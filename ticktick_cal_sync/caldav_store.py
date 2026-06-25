"""iCloud CalDAV store.

Talks to iCloud CalDAV directly via the `caldav` lib (not the MCP server). Builds VEVENTs
with `icalendar` for full control over fields -- this also makes the Phase 3 all-day
extension (DTSTART;VALUE=DATE) a small change rather than a rewrite.

Phase 1 emits only timed events (DTSTART/DTEND as UTC instants), so the unreliable
is_all_day CalDAV flag (spec issue 4) is never exercised.

Phase 2 also READS events back: `get_event` GETs an object by its deterministic href and
parses it into a CalEvent (no REPORT query -- iCloud rejects those with 412). Change
detection keys off the object's ETag (iCloud does NOT send Last-Modified headers and our
own VEVENTs carry no LAST-MODIFIED, but iCloud always returns a stable ETag that flips on
any edit). LAST-MODIFIED -- which Apple injects into the VEVENT on a user edit -- is parsed
when present and used only for conflict tie-breaking.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import caldav
from caldav.lib.error import DAVError, NotFoundError
from icalendar import Calendar, Event

log = logging.getLogger("ticktick-cal-sync")

UID_PREFIX = "ticktick-"


def uid_for(task_id: str) -> str:
    return f"{UID_PREFIX}{task_id}"


@dataclass(frozen=True)
class CalEvent:
    """Parsed snapshot of a calendar object, as currently stored in iCloud."""
    uid: str
    summary: str
    dtstart: dt.datetime | None  # tz-aware UTC instant; None if the event is all-day
    dtend: dt.datetime | None
    all_day: bool
    all_day_date: dt.date | None  # the VALUE=DATE start when all_day, else None
    last_modified: dt.datetime | None  # from VEVENT LAST-MODIFIED, if Apple stamped one
    etag: str


class CalDavStore:
    """Manages one or more iCloud calendars. Every write/read targets a named calendar so a
    single store can route TickTick tasks to different calendars (e.g. habits/routines vs the
    default). The first name is the default used when a caller omits the calendar."""

    def __init__(self, url: str, username: str, password: str, calendar_names: list[str],
                 default_duration_min: int, allow_create: bool = False):
        if not calendar_names:
            raise ValueError("CalDavStore requires at least one calendar name")
        self._client = caldav.DAVClient(url=url, username=username, password=password)
        self._principal = self._client.principal()
        self._default_calendar_name = calendar_names[0]
        self._default_duration = dt.timedelta(minutes=default_duration_min)
        # Resolve each managed calendar once (no-auto-create unless bootstrapping).
        self._calendars: dict[str, caldav.Calendar] = {
            name: self._resolve_calendar(name, allow_create) for name in calendar_names
        }

    def _resolve_calendar(self, calendar_name: str,
                          allow_create: bool = False) -> caldav.Calendar:
        for cal in self._principal.calendars():
            try:
                name = cal.get_display_name()
            except Exception:
                name = None
            if name == calendar_name:
                return cal
        # SAFETY: do NOT silently create a blank calendar. A missing match here is almost
        # always a transient iCloud listing/auth blip; auto-creating a fresh empty calendar
        # makes every per-UID GET 404 -> the sync reads the calendar as "empty" and (pre-fix)
        # mass-completed tasks. Auto-create is only for first-time bootstrap, behind --init.
        if allow_create:
            return self._principal.make_calendar(name=calendar_name)
        raise RuntimeError(
            f"Calendar {calendar_name!r} not found in the iCloud account. Refusing to "
            f"create or proceed (a transient listing failure would otherwise empty-read the "
            f"calendar). Re-run with --init to bootstrap a new calendar."
        )

    def _calendar(self, calendar_name: str | None) -> caldav.Calendar:
        return self._calendars[calendar_name or self._default_calendar_name]

    def _build_ical(self, uid: str, summary: str, description: str,
                    start: dt.datetime, end: dt.datetime, category: str) -> str:
        cal = Calendar()
        cal.add("prodid", "-//ticktick-cal-sync//Phase1//EN")
        cal.add("version", "2.0")
        ev = Event()
        ev.add("uid", uid)
        ev.add("summary", summary)
        if description:
            ev.add("description", description)
        ev.add("dtstart", start.astimezone(dt.timezone.utc))
        ev.add("dtend", end.astimezone(dt.timezone.utc))
        ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
        if category:
            ev.add("categories", [category])
        cal.add_component(ev)
        return cal.to_ical().decode("utf-8")

    def _build_all_day_ical(self, uid: str, summary: str, description: str,
                            date: dt.date, category: str) -> str:
        """Build an all-day VEVENT: DTSTART/DTEND as VALUE=DATE (end exclusive, next day).

        icalendar emits a `datetime.date` (not datetime) as `;VALUE=DATE`, which is what
        iCloud needs to store isAllDay=true reliably -- the MCP is_all_day flag is not
        trustworthy (Phase 1 issue 4), but a raw VALUE=DATE PUT is.
        """
        cal = Calendar()
        cal.add("prodid", "-//ticktick-cal-sync//Phase3//EN")
        cal.add("version", "2.0")
        ev = Event()
        ev.add("uid", uid)
        ev.add("summary", summary)
        if description:
            ev.add("description", description)
        ev.add("dtstart", date)
        ev.add("dtend", date + dt.timedelta(days=1))
        ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
        if category:
            ev.add("categories", [category])
        cal.add_component(ev)
        return cal.to_ical().decode("utf-8")

    def _resolve_window(self, start: dt.datetime | None,
                        due: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
        if start is not None and start < due:
            return start, due
        # Only a due time (or start >= due): make a default-duration event ending at due.
        return due, due + self._default_duration

    def _event_url(self, uid: str, calendar_name: str | None = None) -> str:
        # Deterministic object path per UID within the target calendar. iCloud handles plain
        # PUT/GET/DELETE on a known href reliably, whereas REPORT-based UID queries
        # (event_by_uid) return 412 Precondition Failed on iCloud -- so we address directly.
        base = str(self._calendar(calendar_name).url)
        if not base.endswith("/"):
            base += "/"
        return f"{base}{uid}.ics"

    def upsert(self, uid: str, summary: str, description: str, category: str,
               start: dt.datetime | None, due: dt.datetime | None,
               all_day: bool = False, all_day_date: dt.date | None = None,
               calendar_name: str | None = None) -> CalEvent | None:
        if all_day:
            if all_day_date is None:
                raise ValueError(f"upsert {uid}: all_day requires all_day_date")
            ical = self._build_all_day_ical(uid, summary, description, all_day_date, category)
        else:
            s, e = self._resolve_window(start, due)
            ical = self._build_ical(uid, summary, description, s, e, category)
        # PUT to the deterministic href overwrites any existing object -> idempotent
        # create-or-update with no REPORT query needed.
        url = self._event_url(uid, calendar_name)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        resp = self._client.put(url, ical, headers)
        if resp.status == 412:
            # iCloud returned 412 Precondition Failed. This is what crash-looped the sync after
            # the 2026-06-24 calendar-replacement incident: PUT into a collection in a stale /
            # just-rebuilt state is rejected. Our events are fully sync-owned at a deterministic
            # href, so clearing any conflicting object and re-PUTting once is safe & idempotent.
            log.warning("PUT %s -> 412 Precondition Failed; clearing object and retrying once", uid)
            try:
                self._client.request(url, "DELETE")
            except NotFoundError:
                pass
            resp = self._client.put(url, ical, headers)
        if resp.status >= 400:
            raise DAVError(f"PUT {uid} failed: HTTP {resp.status}")
        # Re-read so the caller gets the authoritative server-side ETag (and any
        # LAST-MODIFIED) to persist as the new calendar baseline -- this is what keeps
        # the next poll from mistaking our own write for a fresh calendar-side change.
        return self.get_event(uid, calendar_name)

    @staticmethod
    def _coerce_utc(value) -> dt.datetime | None:
        """Normalize an icalendar DTSTART/DTEND value to a tz-aware UTC datetime.

        Returns None for a date-only (all-day) value, which the caller treats specially.
        """
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(dt.timezone.utc)
        return None  # datetime.date -> all-day

    def get_event(self, uid: str, calendar_name: str | None = None) -> CalEvent | None:
        """GET and parse the calendar object for uid, or None if it no longer exists."""
        try:
            resp = self._client.request(self._event_url(uid, calendar_name), "GET")
        except NotFoundError:
            return None
        if resp.status == 404:
            return None
        if resp.status >= 400:
            raise DAVError(f"GET {uid} failed: HTTP {resp.status}")

        etag = (resp.headers.get("ETag") or resp.headers.get("etag") or "").strip('"')
        raw = resp.raw
        body = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        cal = Calendar.from_ical(body)
        vevent = next((c for c in cal.walk("VEVENT")), None)
        if vevent is None:
            return None

        dtstart_raw = vevent.get("dtstart")
        dtend_raw = vevent.get("dtend")
        dtstart = self._coerce_utc(dtstart_raw.dt) if dtstart_raw is not None else None
        dtend = self._coerce_utc(dtend_raw.dt) if dtend_raw is not None else None
        all_day = dtstart_raw is not None and dtstart is None
        # For an all-day event DTSTART is a date-only value; capture it so write-back can
        # map the calendar date back to TickTick (a plain datetime.date, never a datetime).
        all_day_date = (
            dtstart_raw.dt
            if all_day and isinstance(dtstart_raw.dt, dt.date)
            and not isinstance(dtstart_raw.dt, dt.datetime)
            else None
        )
        lm = vevent.get("last-modified")
        last_modified = self._coerce_utc(lm.dt) if lm is not None else None

        return CalEvent(
            uid=uid,
            summary=str(vevent.get("summary") or ""),
            dtstart=dtstart,
            dtend=dtend,
            all_day=all_day,
            all_day_date=all_day_date,
            last_modified=last_modified,
            etag=etag,
        )

    def event_exists(self, uid: str, calendar_name: str | None = None) -> bool:
        try:
            resp = self._client.request(self._event_url(uid, calendar_name), "GET")
        except NotFoundError:
            return False
        return resp.status < 400

    def delete(self, uid: str, calendar_name: str | None = None) -> None:
        try:
            resp = self._client.request(self._event_url(uid, calendar_name), "DELETE")
        except NotFoundError:
            return
        if resp.status >= 400 and resp.status != 404:
            raise DAVError(f"DELETE {uid} failed: HTTP {resp.status}")
