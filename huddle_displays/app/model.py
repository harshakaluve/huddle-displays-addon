"""Domain model shared by the calendar sources and the renderer."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# Sensitivity values as Microsoft Graph reports them.
PRIVATE_SENSITIVITIES = {"private", "confidential"}


@dataclass
class Event:
    start: datetime          # timezone-aware, local
    end: datetime            # timezone-aware, local
    subject: str
    organiser: Optional[str] = None
    sensitivity: str = "normal"
    tentative: bool = False
    all_day: bool = False

    @property
    def is_private(self) -> bool:
        return (self.sensitivity or "normal").lower() in PRIVATE_SENSITIVITIES

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def display_subject(self, redact_private: bool = True) -> str:
        if redact_private and self.is_private:
            return "Private meeting"
        return self.subject or "Untitled meeting"

    def display_organiser(self, redact_private: bool = True) -> Optional[str]:
        if redact_private and self.is_private:
            return None
        return self.organiser

    def contains(self, when: datetime) -> bool:
        return self.start <= when < self.end


@dataclass
class Room:
    room_id: str
    name: str
    calendar_entity: str                 # e.g. calendar.huddle_boardroom
    mailbox: Optional[str] = None        # UPN, used for Graph enrichment + QR
    capacity: Optional[int] = None
    amenities: list[str] = field(default_factory=list)
    booking_url: Optional[str] = None    # overrides the generated OWA deep link

    # Which physical panel currently hangs on this door, e.g. "panel-03".
    # Firmware carries the PANEL id, never the room -- so when the panels come
    # back off the charging dock and two get swapped, you fix it by editing this
    # line instead of reflashing. Leave unset and the panel id is the room id.
    panel_id: Optional[str] = None


@dataclass
class RoomView:
    """Everything the renderer needs for one screen. Computed, not fetched."""
    room: Room
    now: datetime
    current: Optional[Event]
    events: list[Event]                  # today's events, sorted, incl. current
    stale: bool = False
    stale_since: Optional[datetime] = None
    battery_pct: Optional[int] = None

    @property
    def data_unknown(self) -> bool:
        """Stale with nothing cached. Must NOT be rendered as 'available' --
        an unreachable calendar is not an empty one."""
        return self.stale and not self.events

    @property
    def is_busy(self) -> bool:
        return self.current is not None

    @property
    def next_event(self) -> Optional[Event]:
        for ev in self.events:
            if ev.start > self.now:
                return ev
        return None

    def free_until(self) -> Optional[datetime]:
        """When the current free period ends. None means free for the rest of the day."""
        nxt = self.next_event
        return nxt.start if nxt else None


def humanise(delta: timedelta) -> str:
    """90 min -> '1 h 30 min'; 45 min -> '45 min'."""
    mins = max(0, int(round(delta.total_seconds() / 60)))
    if mins < 60:
        return f"{mins} min"
    hours, rem = divmod(mins, 60)
    if rem == 0:
        return f"{hours} h"
    return f"{hours} h {rem} min"


def humanise_short(delta: timedelta) -> str:
    """90 min -> '1h 30m'."""
    mins = max(0, int(round(delta.total_seconds() / 60)))
    if mins < 60:
        return f"{mins}m"
    hours, rem = divmod(mins, 60)
    return f"{hours}h" if rem == 0 else f"{hours}h {rem}m"
