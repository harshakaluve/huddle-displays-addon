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
import time
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import Optional, Sequence

import requests

from .model import PRIVATE_SENSITIVITIES, Event, Room

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
        # (organiser address, iCalUId) -> (fetched_at, (subject, sensitivity) | None)
        self._own: dict = {}

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

    OWN_COPY_TTL = 600.0

    def _own_copy(self, address: str, ical_uid: str, tz: str):
        """The organiser's own copy of a meeting: (subject, sensitivity) or None.

        Exchange rewrites the subject on a ROOM mailbox's copy to the organiser's
        name, so the real title only survives on the organiser's calendar. The
        same iCalUId identifies the meeting on both. Failures are not cached, and
        any error just means we fall back to what the room copy says.
        """
        key = (address.lower(), ical_uid)
        hit = self._own.get(key)
        if hit and time.time() - hit[0] < self.OWN_COPY_TTL:
            return hit[1]
        try:
            r = requests.get(
                f"https://graph.microsoft.com/v1.0/users/{quote(address)}/events",
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "Prefer": f'outlook.timezone="{tz}"'},
                params={"$filter": f"iCalUId eq '{ical_uid}'", "$select": "subject,sensitivity"},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return None
            val = r.json().get("value") or []
        except requests.RequestException:
            return None
        result = ((val[0].get("subject") or "", (val[0].get("sensitivity") or "normal").lower())
                  if val else None)
        self._own[key] = (time.time(), result)
        return result

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
                "$select": "subject,start,end,organizer,sensitivity,showAs,isCancelled,iCalUId",
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
            addr = (raw.get("organizer") or {}).get("emailAddress", {})
            organiser = addr.get("name")
            subject = raw.get("subject") or ""
            sensitivity = (raw.get("sensitivity") or "normal").lower()
            ev_start = datetime.fromisoformat(raw["start"]["dateTime"][:26]).replace(tzinfo=start.tzinfo)
            ev_end = datetime.fromisoformat(raw["end"]["dateTime"][:26]).replace(tzinfo=start.tzinfo)

            # The room's copy carries only the organiser's name as its subject
            # (Exchange's AddOrganizerToSubject/DeleteSubject). For meetings that
            # haven't finished, fetch the real title and privacy flag from the
            # organiser's own calendar.
            rewritten = (not subject.strip()) or subject.strip().lower() == (organiser or "").strip().lower()
            if rewritten and addr.get("address") and raw.get("iCalUId") and ev_end > datetime.now(start.tzinfo):
                own = self._own_copy(addr["address"], raw["iCalUId"], tz)
                if own:
                    if own[0].strip():
                        subject = own[0]
                    if own[1] in PRIVATE_SENSITIVITIES:
                        sensitivity = own[1]

            out.append(Event(
                start=ev_start,
                end=ev_end,
                subject=subject,
                organiser=organiser,
                sensitivity=sensitivity,
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
