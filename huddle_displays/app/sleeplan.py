"""Works out how long a panel should sleep.

The panel has no clock of its own -- it never runs SNTP. Every wake it asks this
service "what now?" and is told exactly how many seconds to sleep. That means:

  * alignment to :14/:29/:44/:59 is exact every cycle, because the server knows
    the real time and re-aligns on each wake. ESP32 RTC drift (1-2%) never
    accumulates.
  * no SNTP round-trip on the device -> ~1.5 s less awake time per cycle.
  * the refresh schedule is changed on the server, not by reflashing ten panels.

Weekend sleep is deliberately chunked. A single 60-hour deep sleep would drift by
up to an hour, so the panel takes ~6 h naps instead and is told `render: false`
for all but the last one -- it wakes, asks, and goes straight back to sleep
without touching the e-paper, which is the expensive part of a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence


@dataclass
class Plan:
    sleep_s: int
    render: bool          # False = wake, re-check, sleep again without redrawing
    stay_awake: bool      # True = don't sleep at all, hold the window open for OTA
    wake_at: datetime
    reason: str

    def as_json(self, image_url: str | None = None) -> dict:
        d = {
            "sleep_s": self.sleep_s,
            "render": self.render,
            "stay_awake": self.stay_awake,
            "wake_at": self.wake_at.isoformat(),
            "reason": self.reason,
        }
        if image_url:
            d["image"] = image_url
        return d


def next_boundary(now: datetime, targets: Sequence[int]) -> datetime:
    """Next wall-clock time whose minute is in targets (e.g. 14/29/44/59)."""
    base = now.replace(second=0, microsecond=0)
    for offset in range(0, 120):
        cand = base + timedelta(minutes=offset)
        if cand.minute in targets and cand > now:
            return cand
    raise RuntimeError("no boundary found -- check refresh_minutes")


def _next_active_day(d: date, active_days: Sequence[int]) -> date:
    for i in range(1, 8):
        cand = d + timedelta(days=i)
        if cand.weekday() in active_days:
            return cand
    raise RuntimeError("active_days is empty")


def plan(
    now: datetime,
    *,
    targets: Sequence[int] = (14, 29, 44, 59),
    active_days: Sequence[int] = (0, 1, 2, 3, 4),   # Mon-Fri
    lead_s: int = 25,          # wake this early so the new image lands ON the boundary
    nap_s: int = 6 * 3600,     # max single sleep, keeps RTC drift bounded
    stay_awake: bool = False,
    min_sleep_s: int = 30,
) -> Plan:
    if stay_awake:
        return Plan(0, True, True, now, "ota-window-held-open")

    if now.weekday() in active_days:
        target = next_boundary(now, targets)
        wake = target - timedelta(seconds=lead_s)
        if (wake - now).total_seconds() < min_sleep_s:
            target = next_boundary(target, targets)
            wake = target - timedelta(seconds=lead_s)
        # Late on the last active day the next boundary rolls into the weekend.
        # Don't schedule a Saturday 00:14 redraw -- fall through to weekend logic.
        if target.weekday() in active_days:
            return Plan(int((wake - now).total_seconds()), True, False, wake,
                        f"next-refresh-{target:%H:%M}")

    # --- weekend -----------------------------------------------------------
    resume_day = _next_active_day(now.date(), active_days)
    first = min(targets)
    resume = datetime.combine(resume_day, datetime.min.time(), tzinfo=now.tzinfo)
    resume = resume.replace(minute=first) - timedelta(seconds=lead_s)

    remaining = (resume - now).total_seconds()
    if remaining <= nap_s:
        return Plan(max(min_sleep_s, int(remaining)), True, False, resume,
                    f"weekend-final-hop-to-{resume:%a-%H:%M}")

    wake = now + timedelta(seconds=nap_s)
    return Plan(nap_s, False, False, wake, "weekend-nap-no-redraw")
