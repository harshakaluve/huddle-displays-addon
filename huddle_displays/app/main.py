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
from .render import render, to_bytes
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
                    stale=stale, stale_since=since, battery_pct=battery_pct)


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
        booking_url=CFG.booking_url(view.room),
        day_start=CFG.day_start,
        day_end=CFG.day_end,
        redact_private=CFG.redact_private,
    )
    body = to_bytes(img, fmt)
    return Response(body, mimetype="image/bmp" if fmt == "bmp" else "image/png",
                    headers={"Cache-Control": "no-store", "Content-Length": str(len(body))})


@app.get("/")
def index():
    # Home Assistant's "Open Web UI" (ingress) button lands here.
    base = request.headers.get("X-Ingress-Path", "")
    rows = "".join(
        f'<li><a href="{base}/preview/{r.room_id}">{r.name}</a></li>' for r in CFG.rooms
    )
    return Response(
        f"""<!doctype html><meta charset=utf-8><title>Huddle Room Displays</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;background:#1c1c1c;color:#ddd;
max-width:420px;margin:48px auto;padding:0 16px}}
h1{{font-size:18px}}
a{{color:#8ab4f8;text-decoration:none}} a:hover{{text-decoration:underline}}
ul{{list-style:none;padding:0}} li{{padding:6px 0;border-bottom:1px solid #333}}</style>
<h1>Huddle Room Displays</h1>
<ul>{rows}</ul>
<p style="opacity:.6;font-size:13px">Panels talk to /plan and /image directly; this page is just for humans.</p>""",
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
<p style="text-align:center">{room_id} &middot; reloads every 15 s</p></div>
<script>setInterval(()=>document.getElementById('p').src='{base}/image/{room_id}.png?t='+Date.now(),15000)</script>""",
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
    })


if __name__ == "__main__":
    from waitress import serve
    log.info("huddle renderer up, %d rooms, tz=%s", len(CFG.rooms), CFG.timezone)
    serve(app, host="0.0.0.0", port=8099, threads=6)
