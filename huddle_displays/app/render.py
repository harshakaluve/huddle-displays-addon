"""Renders a RoomView into an 800x480 1-bit image for the XIAO ePaper panel.

Drawing happens in 8-bit greyscale so text is antialiased, then the whole canvas
is hard-thresholded to 1-bit. Hard threshold (not Floyd-Steinberg) because
dithered text on e-paper looks like dirt; genuine tints are built from explicit
dither tiles instead, see shade().
"""
from __future__ import annotations

import io
from datetime import datetime, time, timedelta
from typing import Iterable, Optional, Sequence

import qrcode
from PIL import Image, ImageDraw

from .model import Event, RoomView, humanise, humanise_short
from .theme import (
    BLACK, BODY_BOTTOM, BODY_TOP, CONTENT_L, CONTENT_R, H, HEADER_H,
    TEXT_THRESHOLD, TIMELINE_TOP, W, WHITE, font,
)


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------

def _text_w(draw: ImageDraw.ImageDraw, s: str, f) -> int:
    return int(draw.textlength(s, font=f))


def fit_text(draw, s: str, weight: str, max_w: int, start: int, min_size: int) -> int:
    """Largest size in [min_size, start] at which s fits on one line."""
    size = start
    while size > min_size and _text_w(draw, s, font(weight, size)) > max_w:
        size -= 2
    return size


def wrap(draw, s: str, f, max_w: int, max_lines: int) -> list[str]:
    words = s.split()
    lines, cur, i = [], "", 0
    while i < len(words) and len(lines) < max_lines:
        trial = f"{cur} {words[i]}".strip()
        if not cur or _text_w(draw, trial, f) <= max_w:
            cur, i = trial, i + 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < max_lines:
        lines.append(cur)
        cur = ""
    # Only ellipsise when words were genuinely dropped -- comparing joined
    # length to the source is wrong, because split() collapses runs of spaces.
    if i < len(words) or cur:
        last = lines[-1]
        while last and _text_w(draw, last + "…", f) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def tracked(draw, xy, s: str, f, fill, tracking: int = 0, anchor_right: bool = False):
    """Draw text with extra letter-spacing (Pillow has no tracking)."""
    x, y = xy
    if anchor_right:
        total = sum(_text_w(draw, ch, f) + tracking for ch in s) - tracking
        x -= total
    for ch in s:
        draw.text((x, y), ch, font=f, fill=fill)
        x += _text_w(draw, ch, f) + tracking
    return x


_DITHERS = {
    50: [(0, 0), (1, 1), (2, 2), (3, 3), (0, 2), (1, 3), (2, 0), (3, 1)],
    25: [(0, 0), (2, 2)],
    12: [(0, 0)],          # 1 px in 16 -- reads as a faint wash, never as a block
}


def shade(img: Image.Image, box, level: int = 25, ink: int = BLACK):
    """Fill box with a regular dither so it reads as a tint on a 1-bit panel."""
    x0, y0, x1, y1 = [int(v) for v in box]
    px = img.load()
    pattern = _DITHERS[level]
    for y in range(max(0, y0), min(img.height, y1)):
        for x in range(max(0, x0), min(img.width, x1)):
            if ((x % 4), (y % 4)) in pattern:
                px[x, y] = ink


def rounded(draw, box, r, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------

def draw_header(img, draw, view: RoomView, logo: Optional[Image.Image]):
    draw.rectangle([0, 0, W, HEADER_H], fill=BLACK)
    x = CONTENT_L

    if logo is not None:
        lh = 26
        lw = int(logo.width * lh / logo.height)
        img.paste(logo.resize((lw, lh), Image.LANCZOS), (x, (HEADER_H - lh) // 2))
        x += lw + 18
    else:
        f = font("black", 26)
        tracked(draw, (x, 23), "LWYD", f, WHITE, tracking=1)
        x += _text_w(draw, "LWYD", f) + 4 + 18

    draw.line([(x - 9, 18), (x - 9, HEADER_H - 18)], fill=WHITE, width=1)

    # Room name -- second-loudest thing on the screen after the status word.
    right_block_w = 190
    name_max = CONTENT_R - x - right_block_w
    size = fit_text(draw, view.room.name, "semibold", name_max, 32, 20)
    f_name = font("semibold", size)
    draw.text((x, (HEADER_H - size) // 2 - 3), view.room.name, font=f_name, fill=WHITE)

    # Date + last refresh, right aligned, two lines.
    d = view.now
    date_s = f"{d:%a} {d.day} {d:%b}"
    f_date = font("medium", 21)
    draw.text((CONTENT_R, 14), date_s, font=f_date, fill=WHITE, anchor="ra")

    refresh_s = f"Updated {d:%H:%M}"
    if view.stale:
        refresh_s = f"Stale - last sync {view.stale_since:%H:%M}" if view.stale_since else "Stale data"
    f_ref = font("regular", 15)
    draw.text((CONTENT_R, 45), refresh_s, font=f_ref, fill=WHITE, anchor="ra")

    if view.battery_pct is not None and view.battery_pct <= 20:
        draw.text((CONTENT_R - _text_w(draw, refresh_s, f_ref) - 12, 45),
                  "!", font=font("black", 15), fill=WHITE, anchor="ra")


# --------------------------------------------------------------------------
# body - busy
# --------------------------------------------------------------------------

def draw_busy(img, draw, view: RoomView, redact: bool):
    ev = view.current
    y = BODY_TOP + 22

    tracked(draw, (CONTENT_L, y), "IN USE", font("black", 20), BLACK, tracking=3)
    y += 34

    subject = ev.display_subject(redact)
    size = 46 if len(subject) < 34 else 38
    f_sub = font("bold", size)
    lines = wrap(draw, subject, f_sub, CONTENT_R - CONTENT_L, 2)
    if len(lines) == 1 and size == 38:
        f_sub = font("bold", 46)
        lines = wrap(draw, subject, f_sub, CONTENT_R - CONTENT_L, 2)
    for line in lines:
        draw.text((CONTENT_L, y), line, font=f_sub, fill=BLACK)
        y += int(f_sub.size * 1.16)

    y += 6
    organiser = ev.display_organiser(redact)
    if organiser:
        draw.text((CONTENT_L, y), organiser, font=font("medium", 25), fill=BLACK)
    elif ev.is_private:
        draw.text((CONTENT_L, y), "Organiser hidden", font=font("regular", 22), fill=BLACK)
    y += 38

    # time + duration, baseline-aligned
    t_s = f"{ev.start:%H:%M} – {ev.end:%H:%M}"
    f_t = font("semibold", 30)
    draw.text((CONTENT_L, y), t_s, font=f_t, fill=BLACK)
    draw.text((CONTENT_L + _text_w(draw, t_s, f_t) + 14, y + 6),
              humanise(ev.duration), font=font("regular", 23), fill=BLACK)
    y += 46

    # what's after this one -- the question people actually ask at the door
    nxt = view.next_event
    if nxt:
        f_n = font("regular", 21)
        nxt_s = f"Next  {nxt.start:%H:%M}  ·  {nxt.display_subject(redact)}"
        for ln in wrap(draw, nxt_s, f_n, CONTENT_R - CONTENT_L, 1):
            draw.text((CONTENT_L, y), ln, font=f_n, fill=BLACK)

    # progress bar pinned to the bottom of the body band
    bar_y = BODY_BOTTOM - 38
    remaining = ev.end - view.now
    frac = 1.0 - (remaining.total_seconds() / max(1.0, ev.duration.total_seconds()))
    frac = min(1.0, max(0.0, frac))

    draw.text((CONTENT_R, bar_y - 25), f"{humanise_short(remaining)} left",
              font=font("semibold", 21), fill=BLACK, anchor="ra")
    draw.rectangle([CONTENT_L, bar_y, CONTENT_R, bar_y + 12], outline=BLACK, width=2)
    fill_w = int((CONTENT_R - CONTENT_L) * frac)
    if fill_w > 3:
        draw.rectangle([CONTENT_L, bar_y, CONTENT_L + fill_w, bar_y + 12], fill=BLACK)


# --------------------------------------------------------------------------
# body - free
# --------------------------------------------------------------------------

def draw_free(img, draw, view: RoomView, qr: Optional[Image.Image]):
    # optically centred in the body band rather than top-aligned, so the
    # free screen doesn't read as "content fell off the bottom"
    y = BODY_TOP + 40
    qr_w = 152 if qr is not None else 0
    text_r = CONTENT_R - (qr_w + 34 if qr is not None else 0)

    size = fit_text(draw, "AVAILABLE", "black", text_r - CONTENT_L, 78, 48)
    draw.text((CONTENT_L, y), "AVAILABLE", font=font("black", size), fill=BLACK)
    y += int(size * 1.08) + 10

    until = view.free_until()
    if until is None:
        draw.text((CONTENT_L, y), "Free for the rest of the day",
                  font=font("semibold", 30), fill=BLACK)
        y += 42
    else:
        draw.text((CONTENT_L, y), f"Free until {until:%H:%M}",
                  font=font("semibold", 32), fill=BLACK)
        y += 44
        gap = humanise(until - view.now)
        nxt = view.next_event
        line = f"{gap}  ·  then {nxt.display_subject(True)}" if nxt else gap
        f = font("regular", 22)
        lines = wrap(draw, line, f, text_r - CONTENT_L, 2)
        for ln in lines:
            draw.text((CONTENT_L, y), ln, font=f, fill=BLACK)
            y += 28

    if qr is not None:
        qx = CONTENT_R - qr_w
        qy = BODY_TOP + 34
        img.paste(qr.resize((qr_w, qr_w), Image.NEAREST), (qx, qy))
        draw.text((qx + qr_w // 2, qy + qr_w + 10), "Scan to book",
                  font=font("semibold", 17), fill=BLACK, anchor="ma")


# --------------------------------------------------------------------------
# body - unknown
# --------------------------------------------------------------------------

def draw_unknown(img, draw, view: RoomView, qr: Optional[Image.Image]):
    """Calendar unreachable AND nothing cached.

    This state exists because the alternative is worse: with no events in hand
    the free-screen logic would render a confident "AVAILABLE", and someone
    would walk into a room that is actually booked. An unreachable calendar is
    not the same as an empty one, and the screen has to say so.
    """
    y = BODY_TOP + 40
    qr_w = 152 if qr is not None else 0
    text_r = CONTENT_R - (qr_w + 34 if qr is not None else 0)

    size = fit_text(draw, "NO DATA", "black", text_r - CONTENT_L, 78, 48)
    draw.text((CONTENT_L, y), "NO DATA", font=font("black", size), fill=BLACK)
    y += int(size * 1.08) + 10

    draw.text((CONTENT_L, y), "Can't reach the calendar",
              font=font("semibold", 30), fill=BLACK)
    y += 42

    tail = f"Last synced {view.stale_since:%H:%M}" if view.stale_since else "No sync since power-on"
    f_t = font("regular", 21)
    for ln in wrap(draw, f"{tail} · check Outlook before using this room",
                   f_t, text_r - CONTENT_L, 2):
        draw.text((CONTENT_L, y), ln, font=f_t, fill=BLACK)
        y += 27

    if qr is not None:
        qx = CONTENT_R - qr_w
        qy = BODY_TOP + 34
        img.paste(qr.resize((qr_w, qr_w), Image.NEAREST), (qx, qy))
        draw.text((qx + qr_w // 2, qy + qr_w + 10), "Scan to book",
                  font=font("semibold", 17), fill=BLACK, anchor="ma")


# --------------------------------------------------------------------------
# timeline
# --------------------------------------------------------------------------

def draw_timeline(img, draw, view: RoomView, day_start: time, day_end: time, redact: bool):
    x0, x1 = CONTENT_L, CONTENT_R
    span_w = x1 - x0

    d0 = view.now.replace(hour=day_start.hour, minute=day_start.minute, second=0, microsecond=0)
    d1 = view.now.replace(hour=day_end.hour, minute=day_end.minute, second=0, microsecond=0)
    total = (d1 - d0).total_seconds()

    def pos(t: datetime) -> float:
        return x0 + span_w * min(1.0, max(0.0, (t - d0).total_seconds() / total))

    label = f"TODAY   {day_start:%H:%M}–{day_end:%H:%M}"
    tracked(draw, (x0, TIMELINE_TOP + 9), label, font("bold", 13), BLACK, tracking=2)

    if view.data_unknown:
        summary = "schedule unavailable"
    else:
        booked = sum(1 for e in view.events if e.end > d0 and e.start < d1)
        summary = f"{booked} booking{'s' if booked != 1 else ''} today"
    draw.text((x1, TIMELINE_TOP + 8), summary, font=font("medium", 14), fill=BLACK, anchor="ra")

    # bar sits low enough that the now-marker's flag never fouls the label row
    bar_y0 = TIMELINE_TOP + 40
    bar_y1 = bar_y0 + 32

    # Deliberately NO tint on the elapsed part of the bar. Any dither dense
    # enough to see from across a corridor is also dense enough to be mistaken
    # for a booked block, and the now-marker already says where we are.
    # White = free, black = booked. Two states, no ambiguity.
    now_x = pos(view.now)
    draw.rectangle([x0, bar_y0, x1, bar_y1], outline=BLACK, width=2)

    for ev in view.events:
        if ev.end <= d0 or ev.start >= d1:
            continue
        ex0, ex1 = pos(ev.start), pos(ev.end)
        if ex1 - ex0 < 3:
            ex1 = ex0 + 3
        draw.rectangle([ex0, bar_y0, ex1, bar_y1], fill=BLACK)
        # label only if it fits and the now-marker won't sit on top of it
        if ex1 - ex0 >= 58 and not (ex0 - 4 <= now_x <= ex0 + 58):
            draw.text((ex0 + 8, bar_y0 + 9), f"{ev.start:%H:%M}",
                      font=font("semibold", 14), fill=WHITE)

    # now marker: flag above the bar + full-height rule through it
    draw.rectangle([now_x - 1, bar_y0 - 6, now_x + 1, bar_y1 + 6], fill=BLACK)
    draw.polygon([(now_x - 6, bar_y0 - 13), (now_x + 6, bar_y0 - 13), (now_x, bar_y0 - 5)], fill=BLACK)

    # hour ruler
    f_tick = font("medium", 13)
    hour = d0
    step = 2 if total > 8 * 3600 else 1
    while hour <= d1:
        hx = pos(hour)
        draw.line([(hx, bar_y1 + 2), (hx, bar_y1 + 6)], fill=BLACK, width=1)
        anchor = "la" if hour == d0 else ("ra" if hour == d1 else "ma")
        draw.text((hx, bar_y1 + 9), f"{hour:%H}", font=f_tick, fill=BLACK, anchor=anchor)
        hour += timedelta(hours=step)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def make_qr(url: str, box: int = 4) -> Image.Image:
    qr = qrcode.QRCode(version=None, box_size=box, border=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("L")


def render(view: RoomView, *, logo: Optional[Image.Image] = None,
           booking_url: Optional[str] = None,
           day_start: time = time(8, 0), day_end: time = time(20, 0),
           redact_private: bool = True) -> Image.Image:
    img = Image.new("L", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    draw_header(img, draw, view, logo)

    # the QR only appears on screens where booking is the next action
    qr = make_qr(booking_url) if (booking_url and not view.is_busy) else None
    if view.data_unknown:
        draw_unknown(img, draw, view, qr)
    elif view.is_busy:
        draw_busy(img, draw, view, redact_private)
    else:
        draw_free(img, draw, view, qr)

    draw.line([(CONTENT_L, TIMELINE_TOP - 1), (CONTENT_R, TIMELINE_TOP - 1)], fill=BLACK, width=1)
    draw_timeline(img, draw, view, day_start, day_end, redact_private)

    # single hard threshold -> pure 1-bit, no dithering artefacts in the type
    return img.point(lambda p: 255 if p > TEXT_THRESHOLD else 0, mode="1")


def to_bytes(img: Image.Image, fmt: str = "png") -> bytes:
    buf = io.BytesIO()
    if fmt == "bmp":
        img.save(buf, format="BMP")
    else:
        img.save(buf, format="PNG", bits=1, optimize=True)
    return buf.getvalue()
