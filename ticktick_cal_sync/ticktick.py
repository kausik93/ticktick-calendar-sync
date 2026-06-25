"""TickTick V1 Open API client.

Phase 1 deliberately uses the raw V1 REST API (NOT the get_tasks_due_today convenience
endpoint, which buckets by UTC date and silently drops evening-ET tasks). The endpoint
GET /open/v1/project/{id}/data returns ALL active (status 0) tasks for a project with no
server-side date filter, so we fetch everything active and filter locally. This sidesteps
the evening-ET / overdue date-window bugs entirely.

Known gap: the V1 /project list never returns the Inbox, so Inbox tasks are not synced.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://api.ticktick.com/open/v1"
TIMEOUT = 30
# TickTick timestamp format, e.g. "2026-06-13T00:00:00.000+0000"
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"


def parse_ts(value: str | None) -> dt.datetime | None:
    """Parse a TickTick timestamp into a tz-aware (UTC) datetime, or None."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, _TS_FMT)
    except (ValueError, TypeError):
        return None


def format_ts(value: dt.datetime) -> str:
    """Format a tz-aware datetime as a TickTick timestamp (UTC, e.g. ...+0000).

    Inverse of parse_ts: used when writing a calendar-side time change back to TickTick.
    """
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def local_all_day_date(instant: dt.datetime, tz_name: str) -> dt.date:
    """Calendar date of an all-day task in its own zone.

    TickTick stores all-day as local-midnight-expressed-as-UTC (e.g. an ET June 22 all-day
    task is `2026-06-22T04:00:00+0000`). Taking the date in the task's OWN zone lands on the
    right day; a naive UTC `.date()` would off-by-one the EDT/EST offset back a day.
    """
    return instant.astimezone(ZoneInfo(tz_name)).date()


def all_day_ts(date: dt.date, tz_name: str) -> str:
    """Inverse of local_all_day_date: local-midnight-of-date as a TickTick UTC timestamp.

    Used on write-back to store an all-day task's date the way TickTick expects.
    """
    midnight = dt.datetime.combine(date, dt.time(), ZoneInfo(tz_name))
    return format_ts(midnight)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    content: str
    project_id: str            # TickTick project id (needed for write-back endpoints)
    project_name: str
    start: dt.datetime | None  # tz-aware UTC instant
    due: dt.datetime | None    # tz-aware UTC instant
    modified_time: str         # raw TickTick modifiedTime, used as change key
    is_all_day: bool = False   # all-day task (no timeslot); rendered as a VALUE=DATE VEVENT
    timezone: str = "America/New_York"  # resolved task zone (raw timeZone or sync default)

    @property
    def effective_start(self) -> dt.datetime:
        return self.start or self.due  # due is guaranteed present for synced tasks

    @property
    def all_day_local_date(self) -> dt.date:
        """The calendar date this all-day task belongs on, in its own zone."""
        return local_all_day_date(self.effective_start, self.timezone)

    @property
    def has_time_of_day(self) -> bool:
        """True only if this task carries a real clock time (not date-only).

        Untimed tasks are either correctly flagged is_all_day, OR stored corrupted as
        is_all_day=false at local midnight (a malformed "no time"). Both count as
        untimed and must stay off the calendar; only a non-midnight local time is timed.
        """
        if self.is_all_day:
            return False
        tz = ZoneInfo(self.timezone)
        for inst in (self.start, self.due):
            if inst is not None:
                loc = inst.astimezone(tz)
                if loc.hour or loc.minute or loc.second:
                    return True
        return False

    @property
    def content_hash(self) -> str:
        """Stable hash of the calendar-relevant fields.

        Change detection keys off this, NOT TickTick's modifiedTime: modifiedTime is
        unreliable -- editing a task's due date via the API can leave modifiedTime stale,
        which would silently miss the change. Hashing the actual synced fields is robust.

        All-day tasks hash on their local DATE (not the raw instant) so the digest is stable
        regardless of the midnight-as-UTC offset; timed tasks keep their original formula
        byte-for-byte so existing synced rows don't churn.
        """
        if self.is_all_day:
            parts = [
                self.title,
                self.content,
                self.project_name,
                "ALLDAY",
                self.all_day_local_date.isoformat(),
            ]
        else:
            parts = [
                self.title,
                self.content,
                self.project_name,
                self.start.isoformat() if self.start else "",
                self.due.isoformat(),
            ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class TickTickClient:
    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _get(self, path: str) -> object:
        resp = self._session.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json_body: dict | None = None) -> object:
        resp = self._session.post(f"{BASE_URL}{path}", json=json_body, timeout=TIMEOUT)
        resp.raise_for_status()
        # complete/update may return an empty body; tolerate that.
        return resp.json() if resp.content and resp.text.strip() else {}

    def _delete(self, path: str) -> None:
        resp = self._session.delete(f"{BASE_URL}{path}", timeout=TIMEOUT)
        resp.raise_for_status()

    def list_projects(self) -> list[dict]:
        return self._get("/project") or []

    def project_tasks(self, project_id: str) -> tuple[str, list[dict]]:
        data = self._get(f"/project/{project_id}/data") or {}
        name = (data.get("project") or {}).get("name", "")
        return name, data.get("tasks") or []

    # ----- Phase 2 write-back -------------------------------------------------

    def get_raw_task(self, project_id: str, task_id: str) -> dict:
        """Fetch a single task's full raw body (GET /project/{pid}/task/{tid}).

        Used before an update (so we patch rather than clobber unrelated fields) and
        after a write-back (to re-read the authoritative state for loop-safe restamping).
        """
        return self._get(f"/project/{project_id}/task/{task_id}") or {}

    def make_task(self, raw: dict, project_name: str,
                  default_tz: str = "America/New_York") -> Task:
        """Build a Task from a raw V1 task dict (mirrors fetch_active_tasks)."""
        return Task(
            id=raw["id"],
            title=raw.get("title", "") or "(untitled)",
            content=raw.get("content", "") or "",
            project_id=raw.get("projectId", "") or "",
            project_name=project_name,
            start=parse_ts(raw.get("startDate")),
            due=parse_ts(raw.get("dueDate")),
            modified_time=raw.get("modifiedTime", "") or "",
            is_all_day=bool(raw.get("isAllDay")),
            timezone=(raw.get("timeZone") or "").strip() or default_tz,
        )

    def update_task_times_title(
        self, project_id: str, task_id: str,
        title: str | None, start: dt.datetime | None, due: dt.datetime | None,
    ) -> dict:
        """Write a calendar-side title and/or time change back to TickTick.

        Patches the existing task body (preserving content, tags, timeZone, etc.) and
        re-POSTs it. Only title / startDate / dueDate are touched. Returns the raw
        updated task (TickTick echoes the new state).
        """
        body = self.get_raw_task(project_id, task_id)
        if not body:
            raise RuntimeError(f"Cannot update {task_id}: task not found")
        body["id"] = task_id
        body["projectId"] = project_id
        if title is not None:
            body["title"] = title
        if due is not None:
            body["dueDate"] = format_ts(due)
            body["isAllDay"] = False
        if start is not None:
            body["startDate"] = format_ts(start)
            body["isAllDay"] = False
        return self._post(f"/task/{task_id}", body) or body

    def set_task_all_day(
        self, project_id: str, task_id: str, title: str | None,
        local_date: dt.date, tz_name: str,
    ) -> dict:
        """Write a calendar-side change back as an ALL-DAY task in TickTick.

        Sets isAllDay=true and stores startDate/dueDate as local-midnight-of-date expressed
        as UTC (TickTick's all-day convention). Used both for editing an existing all-day
        task's date and for a timed->all-day conversion made in Apple Calendar.
        """
        body = self.get_raw_task(project_id, task_id)
        if not body:
            raise RuntimeError(f"Cannot update {task_id}: task not found")
        body["id"] = task_id
        body["projectId"] = project_id
        if title is not None:
            body["title"] = title
        ts = all_day_ts(local_date, tz_name)
        body["isAllDay"] = True
        body["startDate"] = ts
        body["dueDate"] = ts
        body["timeZone"] = tz_name
        return self._post(f"/task/{task_id}", body) or body

    def complete_task(self, project_id: str, task_id: str) -> None:
        self._post(f"/project/{project_id}/task/{task_id}/complete")

    def delete_task(self, project_id: str, task_id: str) -> None:
        self._delete(f"/project/{project_id}/task/{task_id}")

    def fetch_active_tasks(
        self, default_tz: str = "America/New_York", horizon_days: int | None = None
    ) -> dict[str, Task]:
        """Return {task_id: Task} for active tasks (timed AND all-day) across all projects.

        Included = a dueDate is present (all-day tasks carry one). All-day tasks render as
        VALUE=DATE VEVENTs; timed tasks as instants. default_tz is the fallback zone when a
        task has no timeZone of its own. If horizon_days is set, tasks whose effective date
        is more than that many days in the FUTURE are skipped (overdue/past kept).
        """
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now + dt.timedelta(days=horizon_days) if horizon_days is not None else None

        out: dict[str, Task] = {}
        for project in self.list_projects():
            project_id = project.get("id")
            if not project_id:
                continue
            project_name, raw_tasks = self.project_tasks(project_id)
            # /project/{id} also carries the name; prefer the /data name, fall back to list.
            project_name = project_name or project.get("name", "")
            for t in raw_tasks:
                if t.get("status", 0) != 0:
                    continue  # not active
                if parse_ts(t.get("dueDate")) is None:
                    continue  # undated -> excluded (known gap)
                # /data tasks may omit projectId; fill it from the loop before building.
                t.setdefault("projectId", project_id)
                if not t.get("projectId"):
                    t["projectId"] = project_id
                task = self.make_task(t, project_name, default_tz)
                if not task.has_time_of_day:
                    continue  # untimed (date-only / corrupted-midnight) -> calendar is timed-only
                if cutoff is not None and task.effective_start > cutoff:
                    continue
                out[task.id] = task
        return out
