"""Generate sample screens without touching Home Assistant.

    python3 preview.py          -> writes out/*.png
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.model import Event, Room, RoomView
from app.render import render, to_bytes

TZ = ZoneInfo("Asia/Kolkata")


def ev(h, m, dur, subject, organiser=None, sensitivity="normal"):
    start = datetime(2026, 9, 16, h, m, tzinfo=TZ)
    return Event(start, start + timedelta(minutes=dur), subject, organiser, sensitivity)


ROOM = Room(
    room_id="boardroom",
    name="Boardroom",
    calendar_entity="calendar.room_boardroom",
    mailbox="boardroom@lwyd.in",
    capacity=12,
)

DAY = [
    ev(9, 0, 60, "Diageo weekly status", "Ankur Shinghal"),
    ev(10, 30, 45, "Gully Labs x RC sync", "Sanjana Taneja"),
    ev(12, 0, 30, "Appraisal", "Nikita Rao", sensitivity="private"),
    ev(14, 0, 90, "TheBar.in Q4 campaign review", "Sanjana Taneja"),
    ev(16, 30, 60, "Godawan creative presentation", "Ankur Shinghal"),
    ev(18, 0, 30, "Vendor call - Superkicks", "Raghav"),
]


def view(now_h, now_m, **kw):
    now = datetime(2026, 9, 16, now_h, now_m, tzinfo=TZ)
    current = next((e for e in DAY if e.contains(now)), None)
    return RoomView(room=ROOM, now=now, current=current, events=DAY, **kw)


CASES = {
    "01-busy":        (view(14, 29), {}),
    "02-free":        (view(13, 14), {"booking_url": "https://outlook.office.com/bookwithme/user/boardroom@lwyd.in"}),
    "03-private":     (view(12, 14), {}),
    "04-free-long":   (view(19, 14), {"booking_url": "https://outlook.office.com/bookwithme/user/boardroom@lwyd.in"}),
    "05-busy-long":   (view(16, 44), {}),
    "06-stale":       (RoomView(room=ROOM, now=datetime(2026, 9, 16, 15, 44, tzinfo=TZ),
                                current=None, events=DAY, stale=True,
                                stale_since=datetime(2026, 9, 16, 14, 29, tzinfo=TZ)),
                       {"booking_url": "https://outlook.office.com/bookwithme/user/boardroom@lwyd.in"}),
}

if __name__ == "__main__":
    for name, (v, kw) in CASES.items():
        img = render(v, **kw)
        img.save(f"out/{name}.png")
        print(f"out/{name}.png   png={len(to_bytes(img,'png'))}B  bmp={len(to_bytes(img,'bmp'))}B")
