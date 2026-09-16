"""End-to-end check: fake Home Assistant calendar -> service -> 1-bit image.

Runs the real Flask app, the real renderer and the real sleep planner. The only
thing stubbed is Home Assistant itself.

    python3 test_e2e.py
"""
import json
import os
import pathlib
import sys
import threading
from datetime import datetime, timedelta
from wsgiref.simple_server import make_server
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

TZ = ZoneInfo("Asia/Kolkata")
FAKE_HA_PORT = 8765

# ---- stand up a fake HA before the app imports its config -----------------
fake = Flask("fake-ha")
PUSHED = []


@fake.get("/api/calendars/<entity>")
def cal(entity):
    base = datetime.now(TZ).replace(second=0, microsecond=0)
    day = base.replace(hour=0, minute=0)

    def blk(h, m, dur, summary):
        s = day.replace(hour=h, minute=m)
        return {"summary": summary,
                "start": {"dateTime": s.isoformat()},
                "end": {"dateTime": (s + timedelta(minutes=dur)).isoformat()}}

    now_h, now_m = base.hour, (base.minute // 30) * 30
    return jsonify([
        blk(9, 0, 60, "Ankur Shinghal; Diageo weekly status"),
        blk(10, 30, 45, "Sanjana Taneja; Gully Labs x RC sync"),
        blk(now_h, now_m, 90, "Sanjana Taneja; TheBar.in Q4 campaign review"),
        blk(18, 0, 30, "Raghav; Vendor call - Superkicks"),
        {"summary": "All hands offsite", "start": {"date": "2026-09-16"},
         "end": {"date": "2026-09-17"}},          # all-day: must be ignored
    ])


@fake.post("/api/states/<entity>")
def push(entity):
    PUSHED.append((entity, request.json))
    return jsonify({}), 200


opts = {
    "timezone": "Asia/Kolkata",
    "ha_url": f"http://127.0.0.1:{FAKE_HA_PORT}",
    "ha_token": "test",
    "organiser_source": "ha",          # exercise the subject-parsing fallback
    "image_format": "bmp",
    "logo_path": "/nonexistent.png",
    "rooms": [{"room_id": "boardroom", "name": "Boardroom",
               "calendar_entity": "calendar.room_boardroom",
               "mailbox": "boardroom@lwyd.in", "capacity": 12,
               "panel_id": "panel-01"}],
}
pathlib.Path("options.json").write_text(json.dumps(opts))

srv = make_server("127.0.0.1", FAKE_HA_PORT, fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()

from app.main import app as huddle_app       # noqa: E402  (must follow options.json)
from app.sources import split_organiser      # noqa: E402

client = huddle_app.test_client()
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(label)


print("\n[1] organiser parsing out of the Exchange subject")
for raw, want in [
    ("Ankur Shinghal; Diageo weekly status", ("Diageo weekly status", "Ankur Shinghal")),
    ("Quarterly review", ("Quarterly review", None)),
]:
    got = split_organiser(raw)
    check(repr(raw), got == want, f"-> {got}")

print("\n[2] /plan")
r = client.get("/plan/panel-01?soc=68.0&rssi=-57")
p = r.get_json()
check("200", r.status_code == 200)
check("sleep_s is a sane gap", 0 < p["sleep_s"] <= 900, f"{p['sleep_s']}s")
check("wake lands on a refresh minute",
      datetime.fromisoformat(p["wake_at"]).minute in (13, 28, 43, 58),
      p["wake_at"])
check("image url present", p["image"].startswith("/image/panel-01.bmp"))
check("battery pushed to HA", any(e[0] == "sensor.huddle_boardroom_battery" for e in PUSHED),
      str(PUSHED[-1][1]["state"]) + "%" if PUSHED else "none")

print("\n[3] panel identity")
check("404 for unknown key", client.get("/plan/nope").status_code == 404)
check("room id still works as a key", client.get("/plan/boardroom").status_code == 200)
check("/panels maps panel -> door",
      client.get("/panels").get_json()["assignments"] == {"panel-01": "boardroom"})

print("\n[4] /image")
for fmt, magic in (("bmp", b"BM"), ("png", b"\x89PNG")):
    r = client.get(f"/image/panel-01.{fmt}")
    ok = r.status_code == 200 and r.data.startswith(magic)
    check(f"{fmt} renders", ok, f"{len(r.data)} bytes")
    pathlib.Path(f"out/e2e.{fmt}").write_bytes(r.data)

from PIL import Image                         # noqa: E402
im = Image.open("out/e2e.bmp")
check("800x480 1-bit", im.size == (800, 480) and im.mode == "1", f"{im.size} {im.mode}")
check("BMP fits the C3 image buffer", len(pathlib.Path('out/e2e.bmp').read_bytes()) < 60_000,
      f"{len(pathlib.Path('out/e2e.bmp').read_bytes())} bytes vs 48 000 B decoded")

print("\n[5] calendar outage still produces a screen")
srv.shutdown()
import app.main as m                          # noqa: E402
m._cache.clear()
r = client.get("/image/panel-01.png")
check("renders with HA down", r.status_code == 200, f"{len(r.data)} bytes")
pathlib.Path("out/e2e-ha-down.png").write_bytes(r.data)
check("/health reports the error", "boardroom" in client.get("/health").get_json()["errors"])

# The important one: an unreachable calendar must never render as "AVAILABLE",
# or people walk into rooms that are actually booked.
v = m.build_view("panel-01")
check("unknown state, not 'free'", v.data_unknown and not v.is_busy,
      f"stale={v.stale} events={len(v.events)}")

print("\n[6] OTA hold")
client.post("/stay-awake/panel-01?on=1")
check("stay_awake honoured", client.get("/plan/panel-01").get_json()["stay_awake"] is True)
client.post("/stay-awake/panel-01?on=0")
check("released", client.get("/plan/panel-01").get_json()["stay_awake"] is False)

os.remove("options.json")
print(f"\n{'ALL PASS' if not failures else str(len(failures)) + ' FAILURES: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
