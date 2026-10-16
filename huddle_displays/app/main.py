"""HTTP service the panels talk to.

Endpoints
---------
GET /plan/<room_id>?battery=4.01&rssi=-58
    The only call a panel *must* make. Returns how long to sleep, whether to
    redraw, and the image URL. Also mirrors panel telemetry into HA.

GET /image/<room_id>.bmp    (or .png)
    The 800x480 1-bit screen. Rendered on demand so the "Updated HH:MM" stamp
    is always truthful.

GET /preview/<room_id>      Browser view, auto-refreshing, for designing.
GET /health                 Liveness + last-error-per-room.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, Response, abort, jsonify, request
from PIL import Image

from . import config
from .model import Event, Room, RoomView
from .render import is_closed, render, to_bytes
from .sleeplan import plan
from .sources import GraphEnricher, HomeAssistantSource, merge_organisers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("huddle")

app = Flask(__name__)
CFG = config.load()
HA = HomeAssistantSource(CFG.ha_url, CFG.ha_token)
GRAPH: Optional[GraphEnricher] = (
    GraphEnricher(CFG.graph_tenant_id, CFG.graph_client_id, CFG.graph_client_secret)
    if CFG.organiser_source == "graph" and CFG.graph_tenant_id else None
)

_LOGO: Optional[Image.Image] = None
_lock = threading.Lock()
_cache: dict[str, tuple[float, list[Event]]] = {}       # room_id -> (fetched_at, events)
_last_error: dict[str, str] = {}
_stay_awake: set[str] = set()
_seen: dict[str, dict] = {}                # panel id -> last check-in (time + wifi signal)
CACHE_TTL = 60.0


def logo() -> Optional[Image.Image]:
    global _LOGO
    if _LOGO is None and CFG.logo_path:
        try:
            _LOGO = Image.open(CFG.logo_path).convert("L")
        except OSError:
            log.info("no logo at %s, falling back to the LWYD wordmark", CFG.logo_path)
            _LOGO = False          # sentinel: don't retry every request
    return _LOGO or None


def fetch_events(room: Room, now: datetime) -> tuple[list[Event], bool, Optional[datetime]]:
    """Returns (events, stale, stale_since). Never raises -- a dead calendar
    must still produce a screen, otherwise the panel shows nothing at all."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    with _lock:
        cached = _cache.get(room.room_id)
    if cached and _time.time() - cached[0] < CACHE_TTL:
        return cached[1], False, None

    try:
        if GRAPH is not None:
            events = GRAPH.events(room, start, end, CFG.timezone)
        else:
            events = merge_organisers(HA.events(room, start, end), CFG.organiser_source)
        with _lock:
            _cache[room.room_id] = (_time.time(), events)
            _last_error.pop(room.room_id, None)
        return events, False, None
    except Exception as exc:                      # noqa: BLE001 - deliberate catch-all
        log.warning("calendar fetch failed for %s: %s", room.room_id, exc)
        with _lock:
            _last_error[room.room_id] = str(exc)
        if cached:
            return cached[1], True, datetime.fromtimestamp(cached[0], CFG.tz)
        return [], True, None


def build_view(key: str, battery_pct: Optional[int] = None) -> RoomView:
    try:
        room = CFG.resolve(key)
    except KeyError:
        abort(404, f"unknown panel or room {key}")
    now = datetime.now(CFG.tz)
    events, stale, since = fetch_events(room, now)
    current = next((e for e in events if e.contains(now)), None)
    return RoomView(room=room, now=now, current=current, events=events,
                    stale=stale, stale_since=since, battery_pct=battery_pct,
                    others=other_free_rooms(room, now))


def other_free_rooms(room: Room, now: datetime) -> list[tuple[str, Optional[datetime]]]:
    """Rooms (other than this one) that are free right now, longest-free first.

    Only rooms with live data count: an unreachable calendar is not an empty
    one, so a room we can't read is never offered as free. Nothing is offered
    outside opening hours, when every room is closed anyway.
    """
    if is_closed(now, CFG.day_start, CFG.day_end, CFG.active_days):
        return []
    found = []
    for other in CFG.rooms:
        if other.room_id == room.room_id:
            continue
        events, stale, _ = fetch_events(other, now)
        if stale and not events:
            continue
        if any(e.contains(now) for e in events):
            continue
        nxt = next((e for e in events if e.start > now), None)
        found.append((other.name, nxt.start if nxt else None))
    # Free for the rest of the day first, then by how long the gap lasts.
    far = now + timedelta(days=1)
    found.sort(key=lambda r: (r[1] or far), reverse=True)
    # A room free for under 15 minutes isn't worth a walk.
    return [r for r in found if r[1] is None or r[1] - now >= timedelta(minutes=15)]


def battery_pct(volts: float) -> int:
    """Single-cell LiPo, discharge curve flattened to a usable 5-point map."""
    points = [(3.30, 0), (3.60, 10), (3.75, 35), (3.95, 70), (4.20, 100)]
    if volts <= points[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(points, points[1:]):
        if volts <= v1:
            return int(round(p0 + (p1 - p0) * (volts - v0) / (v1 - v0)))
    return 100


@app.get("/plan/<key>")
def plan_endpoint(key: str):
    # `key` is the PANEL id burned into the firmware; which room that panel is
    # currently hanging on is a config lookup, not a firmware constant.
    try:
        room = CFG.resolve(key)
    except KeyError:
        abort(404)
    room_id = room.room_id
    _seen[key] = {"at": datetime.now(CFG.tz), "rssi": request.args.get("rssi", type=int)}
    log.info("plan %s rssi=%s", key, _seen[key]["rssi"])

    # A fuel gauge reports state-of-charge directly (soc); a resistor divider
    # reports volts. -1 means no battery hardware is fitted at all.
    pct: Optional[int] = None
    soc = request.args.get("soc", type=float)
    volts = request.args.get("battery", type=float)
    if soc is not None and soc >= 0:
        pct = int(round(soc))
    elif volts is not None and volts > 0:
        pct = battery_pct(volts)

    if pct is not None:
        HA.push_sensor(
            f"sensor.huddle_{room_id}_battery", pct,
            {"unit_of_measurement": "%", "device_class": "battery",
             "friendly_name": f"{room.name} panel battery", "voltage": volts,
             "rssi": request.args.get("rssi", type=int)},
        )

    p = plan(
        datetime.now(CFG.tz),
        targets=CFG.refresh_minutes,
        active_days=CFG.active_days,
        lead_s=CFG.lead_seconds,
        nap_s=CFG.weekend_nap_seconds,
        stay_awake=key in _stay_awake or room_id in _stay_awake,
    )
    # Image URL is keyed by the panel too, so a reassignment takes effect on the
    # next cycle with nothing cached against the old room.
    stamp = int(_time.time())
    return jsonify(p.as_json(f"/image/{key}.{CFG.image_format}?t={stamp}"))


@app.get("/image/<key>.<fmt>")
def image_endpoint(key: str, fmt: str):
    if fmt not in ("png", "bmp"):
        abort(400)
    view = build_view(key)
    img = render(
        view,
        logo=logo(),
        booking_url=CFG.booking_url(view.room, view.now),
        day_start=CFG.day_start,
        day_end=CFG.day_end,
        redact_private=CFG.redact_private,
        active_days=CFG.active_days,
    )
    body = to_bytes(img, fmt)
    return Response(body, mimetype="image/bmp" if fmt == "bmp" else "image/png",
                    headers={"Cache-Control": "no-store", "Content-Length": str(len(body))})


def _expected_gap_s() -> int:
    """Longest normal silence between two check-ins during opening hours."""
    marks = sorted(set(CFG.refresh_minutes)) or [0]
    gaps = [(b - a) % 60 or 60 for a, b in zip(marks, marks[1:] + marks[:1])]
    return max(gaps) * 60


def panel_status(key: str, now: datetime) -> dict:
    """Is this panel behaving? Judged against the schedule, so a panel that is
    asleep overnight or at the weekend is not reported as broken."""
    seen = _seen.get(key)
    closed = is_closed(now, CFG.day_start, CFG.day_end, CFG.active_days)
    if seen is None:
        return {"state": "idle" if closed else "wait", "label": "Asleep" if closed else "Waiting",
                "detail": "no check-in since the add-on started", "age": None, "rssi": None}
    age = int((now - seen["at"]).total_seconds())
    gap = _expected_gap_s()
    if closed:
        state, label = "idle", "Asleep"
    elif age <= gap + 90:
        state, label = "ok", "Online"
    elif age <= gap * 2 + 120:
        state, label = "late", "Late"
    else:
        state, label = "bad", "Quiet"
    return {"state": state, "label": label, "age": age, "rssi": seen["rssi"], "detail": ""}


def _ago(age: Optional[int]) -> str:
    if age is None:
        return "not seen yet"
    if age < 90:
        return f"{age}s ago"
    if age < 5400:
        return f"{age // 60} min ago"
    return f"{age // 3600} h {age % 3600 // 60} min ago"


def _signal(rssi: Optional[int]) -> tuple[int, str]:
    """(bars 0-4, words) from wifi dBm."""
    if rssi is None or rssi == 0:
        return 0, "no reading"
    for floor, bars, words in ((-55, 4, "excellent"), (-65, 3, "good"), (-75, 2, "fair"), (-85, 1, "weak")):
        if rssi >= floor:
            return bars, f"{words}, {rssi} dBm"
    return 1, f"weak, {rssi} dBm"


@app.get("/")
def index():
    # Home Assistant's "Open Web UI" (ingress) button lands here.
    base = request.headers.get("X-Ingress-Path", "")
    now = datetime.now(CFG.tz)
    cards, counts = [], {"ok": 0, "late": 0, "bad": 0, "idle": 0, "wait": 0}
    for r in CFG.rooms:
        key = r.panel_id or r.room_id
        st = panel_status(key, now)
        counts[st["state"]] += 1
        events, stale, _ = fetch_events(r, now)
        cur = next((e for e in events if e.contains(now)), None)
        if stale and not events:
            room_state = "Calendar unreachable"
        elif cur:
            room_state = f"In use until {cur.end.strftime('%I:%M %p').lstrip('0')}"
        else:
            room_state = "Free"
        bars, words = _signal(st["rssi"])
        bar_html = "".join(f'<i class="{"on" if i < bars else ""}" style="height:{5 + i * 4}px"></i>' for i in range(4))
        cards.append(f"""<a class="card" href="{base}/preview/{key}">
<div class="shot"><img loading="lazy" src="{base}/image/{key}.png?t={int(_time.time())}" alt="{r.name} screen"></div>
<div class="meta"><div class="row"><b>{r.name}</b><span class="chip {st['state']}">{st['label']}</span></div>
<div class="sub">{room_state}</div>
<div class="row small"><span>{key} &middot; {_ago(st['age'])}</span><span class="sig" title="Wi-Fi {words}">{bar_html}</span></div></div></a>""")

    if counts["bad"] or counts["late"]:
        banner = f'<div class="banner warn">{counts["bad"] + counts["late"]} panel(s) have gone quiet. Check they are charged and in range.</div>'
    elif counts["wait"]:
        banner = '<div class="banner">Panels are checking in. Give them a few minutes after an add-on restart.</div>'
    elif counts["idle"] and not counts["ok"]:
        banner = '<div class="banner">Outside opening hours. Panels are asleep until the next working day.</div>'
    else:
        banner = '<div class="banner good">All panels are checking in on schedule.</div>'
    errors = "".join(f"<li>{k}: {v}</li>" for k, v in _last_error.items())
    err_html = f'<div class="banner warn"><b>Calendar errors</b><ul>{errors}</ul></div>' if errors else ""

    return Response(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=60><title>Huddle Room Displays</title>
<style>
:root{{--bg:#16171a;--card:#1f2024;--line:#2e3036;--ink:#ececee;--mute:#9a9ca4;--ok:#4ade80;--late:#fbbf24;--bad:#f87171;--idle:#8b8d96}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:28px 20px 48px}}
h1{{font-size:20px;margin:0 0 4px;letter-spacing:-.01em}}.lede{{color:var(--mute);margin:0 0 20px;font-size:13px}}
.banner{{padding:10px 14px;border-radius:10px;background:var(--card);border:1px solid var(--line);margin-bottom:18px;font-size:14px}}
.banner.good{{border-color:#285a3a;color:var(--ok)}}.banner.warn{{border-color:#6b4a14;color:var(--late)}}
.banner ul{{margin:6px 0 0;padding-left:18px;color:var(--mute)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}
.card{{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;color:inherit;text-decoration:none;transition:border-color .15s}}
.card:hover{{border-color:#4a4d57}}.shot{{background:#d9d5cc;padding:10px}}
.shot img{{display:block;width:100%;height:auto;image-rendering:pixelated;border-radius:2px}}
.meta{{padding:12px 14px 13px}}.row{{display:flex;justify-content:space-between;align-items:center;gap:10px}}
.row b{{font-size:16px}}.sub{{color:var(--ink);margin:2px 0 8px}}.small{{color:var(--mute);font-size:12.5px}}
.chip{{font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;border:1px solid currentColor}}
.chip.ok{{color:var(--ok)}}.chip.late{{color:var(--late)}}.chip.bad{{color:var(--bad)}}.chip.idle,.chip.wait{{color:var(--idle)}}
.sig{{display:inline-flex;align-items:flex-end;gap:2px;height:17px}}.sig i{{width:4px;background:#3a3c44;border-radius:1px}}.sig i.on{{background:var(--ink)}}
footer{{color:var(--mute);font-size:12.5px;margin-top:22px}}
</style>
<main><h1>Huddle Room Displays</h1>
<p class="lede">{now.strftime('%a %d %b, %I:%M %p').replace(' 0',' ')} &middot; refreshes every minute &middot; click a room to see its live screen</p>
{banner}{err_html}<div class="grid">{''.join(cards)}</div>
<footer>Check-in history resets when the add-on restarts. Panels talk to /plan and /image directly; this page is for people.</footer></main>""",
                    mimetype="text/html")


@app.get("/preview/<room_id>")
def preview(room_id: str):
    # Ingress serves this under a per-session path prefix (e.g.
    # /api/hassio_ingress/<token>); Supervisor tells us that prefix via this
    # header so the absolute img/script URLs below still resolve. Direct LAN
    # access (what the physical panels use) never sends the header, so base
    # is "" there and nothing changes for them.
    base = request.headers.get("X-Ingress-Path", "")
    return Response(
        f"""<!doctype html><meta charset=utf-8><title>{room_id}</title>
<style>body{{background:#2a2a2a;display:grid;place-items:center;height:100vh;margin:0;
font:14px/1.5 system-ui,sans-serif;color:#999}}
img{{image-rendering:pixelated;width:800px;border:14px solid #d8d4cc;border-radius:4px;
box-shadow:0 8px 40px #0008}}</style>
<div><img src="{base}/image/{room_id}.png?t=0" id=p>
<p style="text-align:center">{room_id} &middot; reloads every 60 s</p></div>
<script>setInterval(()=>document.getElementById('p').src='{base}/image/{room_id}.png?t='+Date.now(),60000)</script>""",
        mimetype="text/html")


@app.post("/stay-awake/<room_id>")
def stay_awake(room_id: str):
    """Hold a panel awake so it can take an OTA. POST ?on=0 to release."""
    if request.args.get("on", "1") == "1":
        _stay_awake.add(room_id)
    else:
        _stay_awake.discard(room_id)
    return jsonify({"room": room_id, "stay_awake": room_id in _stay_awake})


@app.get("/panels")
def panels():
    """Which panel is on which door. Check this after every charging weekend."""
    return jsonify({
        "assignments": CFG.assignments(),
        "note": "panel_id is the sticker on the back of the unit; room_id is the door",
    })


@app.get("/health")
def health():
    return jsonify({
        "rooms": [r.room_id for r in CFG.rooms],
        "panels": CFG.assignments(),
        "errors": _last_error,
        "stay_awake": sorted(_stay_awake),
        "now": datetime.now(CFG.tz).isoformat(),
        # Last time each panel asked for its plan, and its wifi signal (dBm,
        # closer to 0 is stronger; below about -80 is marginal).
        "panels_seen": {
            k: {"last_seen": v["at"].isoformat(),
                "age_s": int((datetime.now(CFG.tz) - v["at"]).total_seconds()),
                "rssi": v["rssi"]}
            for k, v in sorted(_seen.items())
        },
    })


if __name__ == "__main__":
    from waitress import serve
    log.info("huddle renderer up, %d rooms, tz=%s", len(CFG.rooms), CFG.timezone)
    serve(app, host="0.0.0.0", port=8099, threads=6)
