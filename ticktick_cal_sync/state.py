"""SQLite state store.

Tracks the last-synced state of each task so we can detect new / changed / deleted tasks
and keep the calendar an idempotent mirror.

Phase 2 (two-way) extends each row with a calendar-side baseline so we can tell *which*
side changed since the last sync:
  - content_hash / last_modified  -> TickTick-side baseline (Phase 1)
  - cal_etag / cal_last_modified  -> iCloud-side baseline (Phase 2)
  - project_id                    -> needed to address the TickTick write-back endpoints
A poll compares the live TickTick hash against content_hash and the live CalDAV ETag
against cal_etag; a difference on either side flags a change on that side.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_tasks (
    task_id           TEXT PRIMARY KEY,
    event_uid         TEXT NOT NULL,
    last_modified     TEXT NOT NULL,
    content_hash      TEXT NOT NULL DEFAULT '',
    project_id        TEXT NOT NULL DEFAULT '',
    cal_etag          TEXT NOT NULL DEFAULT '',
    cal_last_modified TEXT NOT NULL DEFAULT '',
    calendar          TEXT NOT NULL DEFAULT '',
    synced_at         TEXT NOT NULL
);
"""

# Columns added after the original Phase 1 deployment, applied via ALTER on existing DBs.
# `calendar` records which iCloud calendar the event lives in (multi-calendar routing); an
# empty string means the default calendar (legacy rows predating routing).
_ADDED_COLUMNS = ("content_hash", "project_id", "cal_etag", "cal_last_modified", "calendar")


@dataclass(frozen=True)
class SyncedRow:
    task_id: str
    event_uid: str
    last_modified: str       # TickTick modifiedTime baseline
    content_hash: str        # TickTick content hash baseline
    project_id: str          # TickTick project id (for write-back endpoints)
    cal_etag: str            # CalDAV ETag baseline
    cal_last_modified: str   # CalDAV LAST-MODIFIED baseline (for conflict tie-break)
    calendar: str            # iCloud calendar name the event lives in ('' = default calendar)
    synced_at: str


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(synced_tasks)")}
        for col in _ADDED_COLUMNS:
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE synced_tasks ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )

    def all_rows(self) -> dict[str, SyncedRow]:
        cur = self._conn.execute(
            "SELECT task_id, event_uid, last_modified, content_hash, project_id, "
            "cal_etag, cal_last_modified, calendar, synced_at FROM synced_tasks"
        )
        return {
            r["task_id"]: SyncedRow(
                r["task_id"], r["event_uid"], r["last_modified"], r["content_hash"],
                r["project_id"], r["cal_etag"], r["cal_last_modified"], r["calendar"],
                r["synced_at"],
            )
            for r in cur.fetchall()
        }

    def upsert(self, task_id: str, event_uid: str, last_modified: str, content_hash: str,
               project_id: str, cal_etag: str, cal_last_modified: str, calendar: str,
               synced_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO synced_tasks
                (task_id, event_uid, last_modified, content_hash, project_id,
                 cal_etag, cal_last_modified, calendar, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                event_uid=excluded.event_uid,
                last_modified=excluded.last_modified,
                content_hash=excluded.content_hash,
                project_id=excluded.project_id,
                cal_etag=excluded.cal_etag,
                cal_last_modified=excluded.cal_last_modified,
                calendar=excluded.calendar,
                synced_at=excluded.synced_at
            """,
            (task_id, event_uid, last_modified, content_hash, project_id,
             cal_etag, cal_last_modified, calendar, synced_at),
        )
        self._conn.commit()

    def delete(self, task_id: str) -> None:
        self._conn.execute("DELETE FROM synced_tasks WHERE task_id = ?", (task_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
