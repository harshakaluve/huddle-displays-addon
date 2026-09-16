"""Where the events come from.

Primary source is Home Assistant's calendar REST API, fed by the MS365-Calendar
HACS integration. That endpoint returns summary / start / end / description and
nothing else -- in particular it does NOT carry the organiser or the Outlook
sensitivity flag.

So there are two ways to get "who booked it" and the Private flag:

  A. Exchange-side (no extra credentials). Leave AddOrganizerToSubject $True and
     DeleteSubject $False on the room mailbox; Exchange writes the organiser into
     the subject and ORGANISER_IN_SUBJECT_RE pulls it back out. Zero setup, but
     the exact format varies by tenant -- verify against a real booking.

  B. Graph enrichment (recommended). An app-only registration with
     Calendars.Read reads the same events directly and returns organiser and
     sensitivity as first-class fields. 40 lines, one extra secret, exact.

Set organiser_source: ha | graph | none in the add-on options.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Sequence

import requests

from .model import Event, Room

log = logging.getLogger(__name__)

# Matches the two shapes Exchange writes when AddOrganizerToSubject is on.
ORGANISER_IN_SUBJECT_RE = re.compile(
    r"^\s*(?:(?P<org1>[^;]{2,60});\s*(?P<subj1>.+)"
    r"|(?P<subj2>.+?)\s*[-–]\s*(?P<org2>[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){1,3}))\s*$"
)


def split_organiser(subject: str) -> tuple[str, Optional[str]]:
    m = ORGANISER_IN_SUBJECT_RE.match(subject or "")
    if not m:
        return subject, None
    if m.group("org1"):
        return m.group("subj1").strip(), m.group("org1").strip()
    return m.group("subj2").strip(), m.group("org2").strip()


class HomeAssistantSource:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def events(self, room: Room, start: datetime, end: datetime) -> list[Event]:
        url = f"{self.base_url}/api/calendars/{room.calendar_entity}"
        r = self.session.get(
            url,
            params={"start": start.isoformat(), "end": end.isoformat()},
            timeout=self.timeout,
        )
        r.raise_for_status()
        out: list[Event] = []
        for raw in r.json():
            s, e = raw.get("start", {}), raw.get("end", {})
            if "dateTime" not in s:
                continue  # all-day entries never represent a room booking
            subject = raw.get("summary") or ""
            out.append(Event(
                start=datetime.fromisoformat(s["dateTime"]).astimezone(start.tzinfo),
                end=datetime.fromisoformat(e["dateTime"]).astimezone(start.tzinfo),
                subject=subject,
            ))
        out.sort(key=lambda ev: ev.start)
        return out

    def push_sensor(self, entity_id: str, state, attributes: dict) -> None:
        """Mirror panel telemetry (battery, RSSI) back into HA."""
        try:
            self.session.post(
                f"{self.base_url}/api/states/{entity_id}",
                json={"state": state, "attributes": attributes},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.warning("telemetry push failed for %s: %s", entity_id, exc)


class GraphEnricher:
    """App-only Microsoft Graph lookup for organiser + sensitivity."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, timeout: float = 8.0):
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry = datetime.min

    def _access_token(self) -> str:
        if self._token and datetime.utcnow() < self._token_expiry:
            return self._token
        r = requests.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        payload = r.json()
        self._token = payload["access_token"]
        self._token_expiry = datetime.utcnow() + timedelta(seconds=payload.get("expires_in", 3600) - 120)
        return self._token

    def events(self, room: Room, start: datetime, end: datetime, tz: str) -> list[Event]:
        if not room.mailbox:
            raise ValueError(f"room {room.room_id} has no mailbox set")
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{room.mailbox}/calendarView",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Prefer": f'outlook.timezone="{tz}"',
            },
            params={
                "startDateTime": start.replace(tzinfo=None).isoformat(),
                "endDateTime": end.replace(tzinfo=None).isoformat(),
                "$select": "subject,start,end,organizer,sensitivity,showAs,isCancelled",
                "$orderby": "start/dateTime",
                "$top": "50",
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        out: list[Event] = []
        for raw in r.json().get("value", []):
            if raw.get("isCancelled"):
                continue
            organiser = (raw.get("organizer") or {}).get("emailAddress", {}).get("name")
            out.append(Event(
                start=datetime.fromisoformat(raw["start"]["dateTime"][:26]).replace(tzinfo=start.tzinfo),
                end=datetime.fromisoformat(raw["end"]["dateTime"][:26]).replace(tzinfo=start.tzinfo),
                subject=raw.get("subject") or "",
                organiser=organiser,
                sensitivity=(raw.get("sensitivity") or "normal").lower(),
                tentative=(raw.get("showAs") == "tentative"),
            ))
        return out


def merge_organisers(events: Sequence[Event], mode: str) -> list[Event]:
    """Apply the subject-parsing fallback when Graph isn't in play."""
    if mode != "ha":
        return list(events)
    out = []
    for ev in events:
        subject, organiser = split_organiser(ev.subject)
        out.append(Event(ev.start, ev.end, subject, organiser or ev.organiser,
                         ev.sensitivity, ev.tentative, ev.all_day))
    return out
