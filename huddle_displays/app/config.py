"""Add-on options. Read from /data/options.json under HA OS, or a local
options.json when running the renderer outside the Supervisor for design work.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from typing import Optional
from zoneinfo import ZoneInfo

from .model import Room

DEFAULT_PATHS = ["/data/options.json", "options.json"]


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class Settings:
    timezone: str = "Asia/Kolkata"
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""                       # SUPERVISOR_TOKEN when run as an add-on
    rooms: list[Room] = field(default_factory=list)

    refresh_minutes: list[int] = field(default_factory=lambda: [14, 29, 44, 59])
    active_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    lead_seconds: int = 25
    weekend_nap_seconds: int = 21600

    day_start: time = time(8, 0)
    day_end: time = time(20, 0)

    redact_private: bool = True
    organiser_source: str = "graph"          # graph | ha | none
    image_format: str = "bmp"                # bmp = lowest RAM on ESP32-C3

    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""

    booking_url_template: str = "https://outlook.office.com/book/{mailbox}"
    logo_path: str = "/share/huddle/logo.png"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def room(self, room_id: str) -> Room:
        for r in self.rooms:
            if r.room_id == room_id:
                return r
        raise KeyError(room_id)

    def resolve(self, key: str) -> Room:
        """Look up by panel id first, then room id.

        Panels identify themselves by PANEL id. Reassigning a panel to a
        different door is a one-line config edit, not a reflash -- which matters
        when support takes all ten off the wall every weekend to charge them.
        """
        for r in self.rooms:
            if r.panel_id == key:
                return r
        return self.room(key)

    def assignments(self) -> dict[str, str]:
        return {(r.panel_id or r.room_id): r.room_id for r in self.rooms}

    def booking_url(self, room: Room) -> Optional[str]:
        if room.booking_url:
            return room.booking_url
        if not (self.booking_url_template and room.mailbox):
            return None
        return self.booking_url_template.format(mailbox=room.mailbox, room_id=room.room_id)


def load(path: Optional[str] = None) -> Settings:
    paths = [path] if path else DEFAULT_PATHS
    raw = {}
    for p in paths:
        if p and os.path.exists(p):
            with open(p) as fh:
                raw = json.load(fh)
            break

    s = Settings()
    for key in ("timezone", "ha_url", "ha_token", "lead_seconds", "weekend_nap_seconds",
                "redact_private", "organiser_source", "image_format", "graph_tenant_id",
                "graph_client_id", "graph_client_secret", "booking_url_template", "logo_path"):
        if key in raw:
            setattr(s, key, raw[key])

    if "refresh_minutes" in raw:
        s.refresh_minutes = [int(m) for m in raw["refresh_minutes"]]
    if "active_days" in raw:
        s.active_days = [int(d) for d in raw["active_days"]]
    if "day_start" in raw:
        s.day_start = _parse_hhmm(raw["day_start"])
    if "day_end" in raw:
        s.day_end = _parse_hhmm(raw["day_end"])

    s.rooms = [
        Room(
            room_id=r["room_id"],
            name=r["name"],
            calendar_entity=r["calendar_entity"],
            mailbox=r.get("mailbox"),
            capacity=r.get("capacity"),
            amenities=r.get("amenities", []),
            booking_url=r.get("booking_url"),
            panel_id=r.get("panel_id"),
        )
        for r in raw.get("rooms", [])
    ]

    if not s.ha_token:
        s.ha_token = os.environ.get("SUPERVISOR_TOKEN", "")
    return s
