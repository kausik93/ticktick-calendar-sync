"""Orchestrates one sync pass between TickTick and the iCloud calendar.

Phase 1 (one-way): TickTick is the source of truth, the calendar is a mirror.
Phase 2 (two-way): the calendar is an equal editing surface. Each synced task carries
two baselines in the state store -- a TickTick content hash and a CalDAV ETag -- so a
poll can tell which side changed since the last sync:

    TickTick changed  = live content_hash != stored content_hash
    Calendar changed  = live ETag        != stored cal_etag   (and a baseline exists)

Resolution:
    only TickTick changed -> push TickTick -> calendar      (Phase 1 behaviour)
    only calendar changed -> write back calendar -> TickTick (title / time / deletion)
    both changed          -> last-modified-wins; tie or missing timestamp -> TickTick wins
    event deleted in cal  -> map to TickTick complete (default) or delete

Loop safety: after ANY action both sides are brought into agreement and BOTH baselines are
re-stamped from the resulting authoritative state, so the change is not re-detected next poll.

Two-way write-back is gated behind cfg.two_way_enabled (or the --two-way CLI override). When
off, the pass is byte-for-byte Phase 1 behaviour and never mutates TickTick.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .caldav_store import CalDavStore, CalEvent, uid_for
from .config import load_config
from .state import StateStore
from .ticktick import TickTickClient, Task, parse_ts

log = logging.getLogger("ticktick-cal-sync")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
        stream=sys.stdout,
    )


def _iso(value: dt.datetime | None) -> str:
    return value.isoformat() if value else ""


def _tt_wins(tt_modified: str, cal_last_modified: dt.datetime | None) -> bool:
    """Last-modified-wins tie-break for a simultaneous edit on both sides.

    TickTick wins on a tie or whenever a timestamp is missing -- it is the historical
    source of truth, and TickTick's modifiedTime is known to occasionally lag (a due-date
    edit can leave it stale), so we only let the calendar win when it is *strictly* newer.
    """
    tt_dt = parse_ts(tt_modified)
    if cal_last_modified is None or tt_dt is None:
        return True
    return tt_dt >= cal_last_modified


def run(dry_run: bool = False, two_way_override: bool | None = None,
        allow_create: bool = False) -> int:
    cfg = load_config()
    two_way = cfg.two_way_enabled if two_way_override is None else two_way_override
    log.info(
        "Starting sync (dry_run=%s, two_way=%s, tz=%s, calendar=%r, horizon_days=%s, "
        "cal_deletion_means=%s, mass_missing_abort=%d)",
        dry_run, two_way, cfg.timezone, cfg.calendar_name, cfg.horizon_days,
        cfg.cal_deletion_means, cfg.mass_missing_abort,
    )

    tt = TickTickClient(cfg.ticktick_token)
    tasks: dict[str, Task] = tt.fetch_active_tasks(cfg.timezone, cfg.horizon_days)
    log.info("Fetched %d active task(s) from TickTick", len(tasks))

    state = StateStore(cfg.db_path)
    rows = state.all_rows()

    # The calendar already exists; constructing the store is read-only safe, so we build it
    # even for a dry-run to give an accurate two-way preview. All WRITES below are guarded.
    # allow_create=False means a missing calendar raises instead of being silently recreated.
    store = CalDavStore(
        cfg.caldav_url, cfg.icloud_username, cfg.icloud_password,
        cfg.all_calendar_names, cfg.default_duration_min, allow_create=allow_create,
    )
    # Each tracked event remembers which calendar it lives in; '' (legacy) -> default calendar.
    def row_cal(task_id: str) -> str:
        r = rows.get(task_id)
        return (r.calendar if r and r.calendar else cfg.calendar_name)

    # ---- SAFETY PRE-FLIGHT: detect a wholesale calendar disappearance --------------------
    # The mass-completion incident happened because every tracked event read as missing in a
    # single pass (an external calendar-collection failure), and each missing event was acted
    # on independently. Before touching either side, GET every tracked event once and bail if
    # a catastrophic fraction has vanished -- that is an iCloud/collection failure, not the
    # user deleting tasks. We cache the lookups so the main loop reuses them (no double GET).
    cal_cache: dict[str, CalEvent | None] = {}
    if two_way and rows:
        cal_cache = {tid: store.get_event(uid_for(tid), row_cal(tid)) for tid in rows}
        missing = [tid for tid, ev in cal_cache.items() if ev is None]
        all_gone = len(missing) == len(rows)
        over_threshold = len(missing) >= cfg.mass_missing_abort
        majority_gone = len(missing) > len(rows) // 2
        if missing and (all_gone or (over_threshold and majority_gone)):
            log.error(
                "ABORT: %d of %d tracked calendar events are missing in one pass. This is "
                "almost certainly an iCloud/calendar-collection failure, not real deletions. "
                "No changes made to TickTick or the calendar. (threshold=%d)",
                len(missing), len(rows), cfg.mass_missing_abort,
            )
            state.close()
            return 1

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    c = dict(created=0, updated=0, deleted=0, healed=0, unchanged=0,
             wb_edit=0, wb_done=0, conflict=0, refreshed=0)

    def push_to_cal(task: Task, uid: str) -> None:
        """TickTick -> calendar, then re-stamp both baselines from the stored event. The
        target calendar is chosen by the task's project (habit/routine vs default)."""
        if dry_run:
            return
        cal_name = cfg.calendar_for(task.project_id)
        if task.is_all_day:
            ev = store.upsert(uid, task.title, task.content, task.project_name,
                              None, None, all_day=True,
                              all_day_date=task.all_day_local_date, calendar_name=cal_name)
        else:
            ev = store.upsert(uid, task.title, task.content, task.project_name,
                              task.start, task.due, calendar_name=cal_name)
        etag = ev.etag if ev else ""
        lm = _iso(ev.last_modified) if ev else ""
        state.upsert(task.id, uid, task.modified_time, task.content_hash,
                     task.project_id, etag, lm, cal_name, now_iso)

    def stamp_unchanged(task: Task, uid: str, cal: CalEvent, cal_name: str) -> None:
        """No action needed, but refresh the calendar baseline (e.g. first-ever Phase 2
        poll backfilling cal_etag onto a Phase 1 row)."""
        if dry_run:
            return
        state.upsert(task.id, uid, task.modified_time, task.content_hash,
                     task.project_id, cal.etag, _iso(cal.last_modified), cal_name, now_iso)

    def write_back(task: Task, uid: str, cal: CalEvent, cal_name: str) -> None:
        """Calendar -> TickTick (title, time, and all-day shape), then re-stamp baselines.

        Routing is driven by the CALENDAR event's shape, so timed<->all-day conversions made
        in Apple Calendar flow back automatically:
          - all-day event  -> write an all-day task (isAllDay=true, date as local-midnight-UTC)
          - timed event    -> write a timed task; mapping mirrors Phase 1's _resolve_window in
            reverse using the task's CURRENT shape: a real start (start < due) maps
            DTSTART->start / DTEND->due; a due-only task maps DTSTART->due (its DTEND is a
            synthetic duration we ignore).
        """
        new_title = cal.summary or task.title
        if dry_run:
            if cal.all_day:
                log.info("WRITE-BACK %s %r (all-day date=%s title=%r)", task.id, task.title,
                         cal.all_day_date, new_title)
            else:
                log.info("WRITE-BACK %s %r (timed title=%r start=%s)", task.id, task.title,
                         new_title, _iso(cal.dtstart))
            return

        if cal.all_day and cal.all_day_date is not None:
            tz = task.timezone or cfg.timezone
            log.info("WRITE-BACK %s %r -> all-day %s (title=%r)", task.id, task.title,
                     cal.all_day_date, new_title)
            tt.set_task_all_day(task.project_id, task.id, new_title, cal.all_day_date, tz)
        else:
            new_start: dt.datetime | None = None
            new_due: dt.datetime | None = None
            if cal.dtstart is not None:
                has_real_start = task.start is not None and task.start < task.due
                if has_real_start:
                    new_start = cal.dtstart
                    new_due = cal.dtend or task.due
                else:
                    new_due = cal.dtstart  # block start == due time in our mapping
            log.info("WRITE-BACK %s %r -> timed (title=%r start=%s due=%s)", task.id,
                     task.title, new_title, _iso(new_start), _iso(new_due))
            tt.update_task_times_title(task.project_id, task.id, new_title, new_start, new_due)

        # Re-read the authoritative TickTick state and re-stamp BOTH baselines so the
        # write-back is not re-detected as a fresh TickTick-side change next poll.
        raw = tt.get_raw_task(task.project_id, task.id)
        fresh = tt.make_task(raw, task.project_name, cfg.timezone)
        state.upsert(task.id, uid, fresh.modified_time, fresh.content_hash,
                     task.project_id, cal.etag, _iso(cal.last_modified), cal_name, now_iso)

    def write_back_deletion(task: Task, uid: str) -> None:
        """Calendar event was deleted -> complete (default) or delete the TickTick task."""
        action = cfg.cal_deletion_means
        log.info("WRITE-BACK-DELETE %s %r -> %s task in TickTick", task.id, task.title, action)
        if dry_run:
            return
        if action == "delete":
            tt.delete_task(task.project_id, task.id)
        else:
            tt.complete_task(task.project_id, task.id)
        state.delete(task.id)  # task leaves the active set; drop the row

    # ---- main pass: every active timed task -------------------------------------------
    for task_id, task in tasks.items():
        uid = uid_for(task_id)
        row = rows.get(task_id)
        task_hash = task.content_hash

        # New task -> create the calendar event (Phase 1).
        if row is None:
            log.info("CREATE %s %r (due %s) -> calendar %r", task_id, task.title,
                     task.due.isoformat(), cfg.calendar_for(task.project_id))
            push_to_cal(task, uid)
            c["created"] += 1
            continue

        tt_changed = row.content_hash != task_hash

        # Routing: where the event currently lives vs where this task now belongs. If the task
        # changed project (e.g. moved into/out of Cadence), move the event to the right calendar
        # before any other reconcile -- delete from the old calendar, recreate in the new one.
        cur_cal = row.calendar or cfg.calendar_name
        tgt_cal = cfg.calendar_for(task.project_id)
        if cur_cal != tgt_cal:
            log.info("MOVE   %s %r (%s -> %s)", task_id, task.title, cur_cal, tgt_cal)
            if not dry_run:
                store.delete(row.event_uid, cur_cal)
            push_to_cal(task, uid)  # recreates in tgt_cal and re-stamps state
            c["updated"] += 1
            continue

        # --- Phase 1 path (two-way disabled): cheap, never touches TickTick ---
        if not two_way:
            if tt_changed:
                log.info("UPDATE %s %r", task_id, task.title)
                push_to_cal(task, uid)
                c["updated"] += 1
            elif not store.event_exists(uid, cur_cal):
                log.info("HEAL   %s %r (event missing in calendar)", task_id, task.title)
                push_to_cal(task, uid)
                c["healed"] += 1
            else:
                c["unchanged"] += 1
            continue

        # --- Phase 2 path (two-way enabled): three-way reconcile ---
        # Reuse the pre-flight lookup (already GET-ed once above); no second round-trip.
        cal = cal_cache.get(task_id)

        if cal is None:
            # Event no longer in the calendar.
            if not tt_changed:
                if cfg.cal_deletion_means == "ignore":
                    # SAFE default: a missing event is never a "delete" signal. TickTick is
                    # the source of truth, so re-create (heal) the event for this still-active
                    # timed task. This makes an empty/partial calendar non-destructive.
                    log.info("HEAL   %s %r (event missing in calendar; deletion=ignore)",
                             task_id, task.title)
                    push_to_cal(task, uid)
                    c["healed"] += 1
                else:
                    write_back_deletion(task, uid)
                    c["wb_done"] += 1
            else:
                # Conflict: TickTick edited while the calendar event was deleted.
                # TickTick wins -> re-create the event.
                log.info("CONFLICT %s %r: TT edited vs cal deleted -> TT wins, re-create",
                         task_id, task.title)
                push_to_cal(task, uid)
                c["conflict"] += 1
            continue

        # A baseline of "" means this Phase 1 row has never recorded an ETag; the first
        # Phase 2 poll only backfills it -- it is NOT a calendar-side change.
        cal_changed = bool(row.cal_etag) and cal.etag != row.cal_etag

        if not tt_changed and not cal_changed:
            if row.cal_etag != cal.etag:  # backfill / refresh baseline
                stamp_unchanged(task, uid, cal, cur_cal)
                c["refreshed"] += 1
            else:
                c["unchanged"] += 1
        elif tt_changed and not cal_changed:
            log.info("UPDATE %s %r (TickTick edit -> calendar)", task_id, task.title)
            push_to_cal(task, uid)
            c["updated"] += 1
        elif cal_changed and not tt_changed:
            write_back(task, uid, cal, cur_cal)
            c["wb_edit"] += 1
        else:
            # Both sides changed since last sync -> last-modified-wins.
            if _tt_wins(task.modified_time, cal.last_modified):
                log.info("CONFLICT %s %r: both edited -> TickTick wins", task_id, task.title)
                push_to_cal(task, uid)
            else:
                log.info("CONFLICT %s %r: both edited -> calendar wins", task_id, task.title)
                write_back(task, uid, cal, cur_cal)
            c["conflict"] += 1

    # ---- deletion sweep: rows whose task is no longer an active timed task -------------
    for task_id, row in rows.items():
        if task_id in tasks:
            continue
        log.info("DELETE %s (gone from TickTick active timed set)", task_id)
        if not dry_run:
            store.delete(row.event_uid, row.calendar or cfg.calendar_name)
            state.delete(task_id)
        c["deleted"] += 1

    state.close()
    log.info(
        "Done. created=%d updated=%d deleted=%d healed=%d unchanged=%d refreshed=%d "
        "| writeback_edit=%d writeback_done=%d conflict=%d",
        c["created"], c["updated"], c["deleted"], c["healed"], c["unchanged"],
        c["refreshed"], c["wb_edit"], c["wb_done"], c["conflict"],
    )
    return 0


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Sync timed TickTick tasks <-> iCloud calendar")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report planned changes without writing to either side")
    parser.add_argument("--two-way", dest="two_way", action="store_true", default=None,
                        help="Force two-way write-back on for this run (overrides .env)")
    parser.add_argument("--one-way", dest="two_way", action="store_false",
                        help="Force two-way write-back off for this run (Phase 1 behaviour)")
    parser.add_argument("--init", action="store_true",
                        help="Allow bootstrapping a new iCloud calendar if it does not exist "
                             "(normal runs refuse, to avoid recreating a blank calendar)")
    args = parser.parse_args()
    try:
        sys.exit(run(dry_run=args.dry_run, two_way_override=args.two_way,
                     allow_create=args.init))
    except Exception:
        log.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
