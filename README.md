# ticktick-calendar-sync

A bidirectional sync engine between [TickTick](https://ticktick.com) and a CalDAV
calendar (built for and tested against iCloud Calendar). Timed TickTick tasks become
calendar events you can see and reschedule alongside everything else; edits made on the
calendar flow back into TickTick. It runs as a small, stateful, idempotent batch job —
poll both sides, figure out what changed, reconcile, repeat.

The design goal is a correct two-way mirror that is **safe to run unattended on a timer**,
including when the remote side (iCloud) misbehaves. See
[Safety & lessons learned](#safety--lessons-learned) for the data-integrity incident that
shaped that goal.

## What it does

- **TickTick → calendar.** Every active task that carries a real clock time is rendered as
  a calendar event; all-day tasks render as `VALUE=DATE` events. Untimed / date-only tasks
  are deliberately kept off the calendar.
- **Calendar → TickTick** (two-way mode). Retitle, reschedule, or convert an event between
  timed and all-day on the calendar and the change is written back to the task. Deleting an
  event maps to a configurable action (ignore / complete / delete).
- **Stateful change detection.** A local SQLite store remembers a per-task baseline for
  *both* sides, so each pass can tell which side actually changed since the last sync and
  resolve conflicts deterministically.
- **Self-healing & idempotent.** Re-running never duplicates events; a missing event for a
  still-active task is re-created rather than treated as a deletion.
- **Multi-calendar routing.** Chosen TickTick projects (e.g. habits/routines) can be routed
  to a separate calendar.

## How sync state & conflict handling work

TickTick is the historical source of truth; the calendar is an equal editing surface in
two-way mode. For each synced task the state store keeps two independent baselines:

| Baseline | Side | How a change is detected |
|---|---|---|
| `content_hash` | TickTick | Live hash of the task's calendar-relevant fields differs from the stored hash |
| `cal_etag` | Calendar | Live CalDAV ETag differs from the stored ETag (and a baseline exists) |

Change detection on the TickTick side keys off a **content hash, not TickTick's
`modifiedTime`** — `modifiedTime` can lag (editing a due date via the API may leave it
stale), which would silently miss real changes. Hashing the actual synced fields is robust.

Each pass classifies every task and resolves it:

| Situation | Resolution |
|---|---|
| New task | Create the calendar event |
| Only TickTick changed | Push TickTick → calendar |
| Only calendar changed | Write calendar → TickTick (title / time / all-day shape) |
| Both changed | Last-modified-wins; tie or any missing timestamp → **TickTick wins** |
| Event deleted in calendar | `ignore` (heal/re-create), `complete`, or `delete` per config |
| Task gone from TickTick's active set | Delete the calendar event |

**Loop safety:** after *any* action, both sides are brought into agreement and both
baselines are re-stamped from the resulting authoritative state, so the change is never
re-detected as fresh on the next poll (no write-back ping-pong). Two-way write-back is
gated behind a config flag; with it off, a pass is strictly one-way and never mutates
TickTick.

### iCloud/CalDAV specifics

Worth knowing if you adapt this to another CalDAV server:

- Events are addressed at a **deterministic per-UID href** (`<calendar>/ticktick-<id>.ics`)
  and manipulated with plain `PUT`/`GET`/`DELETE`. iCloud rejects REPORT-based UID queries
  (`event_by_uid`) with `412`, so direct addressing is used throughout.
- Change detection uses the **ETag** iCloud returns on every object (iCloud does not send
  `Last-Modified` headers, and our own VEVENTs carry none). A VEVENT `LAST-MODIFIED` that
  Apple injects on a user edit is parsed when present and used only for conflict tie-breaks.
- All-day events are written as raw `VALUE=DATE` VEVENTs, which iCloud stores reliably.

## Architecture

A small Python package, one module per concern:

| Module | Responsibility |
|---|---|
| `config.py` | Loads settings and resolves the three secrets (env → project `.env` → optional external credential files). |
| `ticktick.py` | TickTick V1 Open API client; the `Task` model, timestamp/all-day handling, content hashing, and write-back endpoints. |
| `caldav_store.py` | CalDAV/iCloud layer; builds and parses VEVENTs, deterministic-href upsert/get/delete, the `CalEvent` snapshot, and no-auto-create calendar resolution. |
| `state.py` | SQLite state store; the `synced_tasks` table, schema migrations, and the per-task dual baseline. |
| `sync.py` | Orchestrates one pass: fetch both sides, the safety pre-flight, the three-way reconcile, the deletion sweep, and the CLI entry point. |
| `run.sh` | Thin wrapper to run one pass via `uv` (for cron / a systemd timer). |

### TickTick API notes

The client uses the raw V1 REST endpoint `GET /open/v1/project/{id}/data`, which returns
all active tasks for a project with no server-side date filter, and filters locally. This
sidesteps a convenience-endpoint bug that buckets by UTC date and silently drops
evening-local tasks. Known gap: the V1 project list never returns the Inbox, so Inbox tasks
are not synced.

## Setup

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kausik93/ticktick-calendar-sync.git
cd ticktick-calendar-sync
cp .env.example .env    # then edit
```

Fill in `.env`:

- **`TICKTICK_ACCESS_TOKEN`** — a TickTick V1 Open API OAuth access token.
- **`ICLOUD_USERNAME` / `ICLOUD_PASSWORD`** — your Apple ID and an **app-specific
  password** (not your main password; generate one in your Apple ID security settings).
- `CALENDAR_NAME` — the iCloud calendar to sync into. It must already exist; the sync
  **refuses to create calendars** on a normal run (see below). Bootstrap once with `--init`.
- Optional sync settings (timezone, default event duration, horizon, two-way mode,
  deletion behavior, multi-calendar routing) are documented inline in `.env.example`.

Secrets can also live in the process environment, or in an existing service's `.env` file
referenced via `TICKTICK_ENV_PATH` / `ICLOUD_ENV_PATH` — useful when another service already
holds the token so it's stored in exactly one place and rotation propagates automatically.

## Usage

```bash
# Preview everything; writes to neither side.
uv run sync-ticktick-cal --dry-run

# First-ever run: allow creating the target calendar if it doesn't exist.
uv run sync-ticktick-cal --init

# A normal one-way pass (TickTick -> calendar only).
uv run sync-ticktick-cal --one-way

# A two-way pass (also writes calendar edits back to TickTick).
uv run sync-ticktick-cal --two-way

# Via the wrapper (what cron / a timer calls); args pass through.
./run.sh --dry-run
```

With no `--one-way` / `--two-way` flag, the direction follows `TWO_WAY_ENABLED` in `.env`.
Run it on whatever cadence you like with cron or a systemd timer, e.g. every 15 minutes.

## Safety & lessons learned

This sync once caused a data-integrity incident, and the current safeguards exist because
of it. It's a useful case study in how a naïve two-way sync can be quietly destructive.

**What happened.** A calendar was renamed on the account. On the next pass the sync looked
up the target calendar by display name, didn't find a match, and (in the then-current code)
**auto-created a fresh, empty calendar** with the expected name. Every tracked event now
lived in the old, now-differently-named calendar, so every per-UID lookup against the new
empty calendar returned "not found." The sync interpreted a whole calendar's worth of
"missing" events as the user having deleted those events — and, with deletion mapped to
completion, **mass-completed roughly 49 tasks** in one pass.

**Root cause.** Two compounding bugs:

1. *Silently auto-creating a calendar on a name miss.* A missing calendar is almost always
   a transient listing/auth blip or a rename — not a signal to materialize a new blank one.
2. *Treating a bulk "all events missing" read as a bulk user-deletion.* A single failed
   read of a collection should never be allowed to drive bulk mutation of the other side.

**Safeguards added (and present in this code):**

- **No silent calendar auto-create.** If the named calendar isn't found, the run **aborts**
  with a clear error instead of creating one. Creation only happens behind an explicit
  `--init` bootstrap flag. (`caldav_store.py`)
- **Mass-missing abort pre-flight.** Before touching either side in two-way mode, the sync
  GETs every tracked event once; if a catastrophic fraction have vanished in a single pass
  (all of them, or at least `MASS_MISSING_ABORT` **and** a majority), it logs and **aborts
  with no changes**. That pattern is an infrastructure failure, not real deletions.
  (`sync.py`)
- **Safe deletion default (`ignore`).** A missing event never means "delete the task" by
  default. Because TickTick is the source of truth, a still-active task whose event is gone
  is simply re-created (healed) — so an empty or partially-failed calendar read is
  non-destructive. `complete` / `delete` remain available but opt-in.
- **Deterministic, idempotent writes + dual-baseline re-stamping** so retries and partial
  failures converge instead of amplifying.

The broader lesson: in a two-way sync, **the absence of data is not an instruction.** A
bulk disappearance on one side should raise suspicion, not trigger bulk action on the other.

## License

MIT
