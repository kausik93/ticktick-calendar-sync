"""Configuration loading.

Secrets needed:
  - TickTick V1 OAuth access token  (TICKTICK_ACCESS_TOKEN)
  - iCloud app-specific password     (ICLOUD_USERNAME / ICLOUD_PASSWORD)

They are resolved in order from: the process environment, this project's own `.env`, and
finally two optional external "service" credential files. The external-file path lets you
point at an existing service's `.env` so a token / app-password is stored in exactly one
place and rotation propagates automatically (no secret duplication). Set TICKTICK_ENV_PATH
and ICLOUD_ENV_PATH to use that mode; leave them unset to keep everything in this project's
`.env` or the environment.

Sync-only settings (timezone, calendar name, etc.) live in this project's own `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# This project's own settings file.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ENV = PROJECT_ROOT / ".env"

# Optional external credential files (source of truth for secrets when configured). Point
# these at an existing service's .env to avoid duplicating a token/app-password.
_TICKTICK_ENV_PATH = os.environ.get("TICKTICK_ENV_PATH", "").strip()
_ICLOUD_ENV_PATH = os.environ.get("ICLOUD_ENV_PATH", "").strip()
TICKTICK_ENV = Path(_TICKTICK_ENV_PATH) if _TICKTICK_ENV_PATH else None
ICLOUD_ENV = Path(_ICLOUD_ENV_PATH) if _ICLOUD_ENV_PATH else None


@dataclass(frozen=True)
class Config:
    ticktick_token: str
    icloud_username: str
    icloud_password: str
    timezone: str
    caldav_url: str
    calendar_name: str                 # default calendar for most projects
    habits_calendar_name: str          # calendar for habit/routine (Cadence) projects
    habits_project_ids: frozenset[str] # TickTick project ids routed to habits_calendar_name
    default_duration_min: int
    horizon_days: int | None  # None = sync all active timed tasks
    two_way_enabled: bool      # Phase 2: write calendar-side edits back to TickTick
    cal_deletion_means: str    # "ignore" | "complete" | "delete" -- how a deleted event maps to TickTick
    mass_missing_abort: int    # if >= this many tracked events vanish at once, abort (safety guard)
    db_path: Path

    def calendar_for(self, project_id: str) -> str:
        """Target iCloud calendar for a task, by its TickTick project id."""
        return (self.habits_calendar_name if project_id in self.habits_project_ids
                else self.calendar_name)

    @property
    def all_calendar_names(self) -> list[str]:
        """Every calendar this sync manages (deduped, stable order)."""
        names = [self.calendar_name]
        if self.habits_calendar_name and self.habits_calendar_name not in names:
            names.append(self.habits_calendar_name)
        return names


def _require(sources: list[dict], key: str) -> str:
    """First non-empty value for `key` across the given lookups (highest priority first)."""
    for mapping in sources:
        val = (mapping.get(key) or "").strip()
        if val:
            return val
    raise RuntimeError(
        f"Missing {key}. Set it in the environment, this project's .env, or an external "
        f"credential file referenced by TICKTICK_ENV_PATH / ICLOUD_ENV_PATH."
    )


def load_config() -> Config:
    ticktick = dotenv_values(TICKTICK_ENV) if TICKTICK_ENV and TICKTICK_ENV.exists() else {}
    icloud = dotenv_values(ICLOUD_ENV) if ICLOUD_ENV and ICLOUD_ENV.exists() else {}
    project = dotenv_values(PROJECT_ENV) if PROJECT_ENV.exists() else {}

    # Secret resolution order: process environment, this project's .env, then the optional
    # external service credential file for that secret.
    tt_secrets = [dict(os.environ), project, ticktick]
    icloud_secrets = [dict(os.environ), project, icloud]

    horizon_raw = (project.get("SYNC_HORIZON_DAYS") or "").strip()
    horizon = int(horizon_raw) if horizon_raw else None

    two_way = (project.get("TWO_WAY_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")
    # "ignore" (safe default): a deleted/missing calendar event NEVER writes back to TickTick;
    # because TickTick is the source of truth, a still-active timed task whose event is gone is
    # simply re-created (healed). "complete"/"delete" retained for completeness but discouraged.
    deletion_means = (project.get("CAL_DELETION_MEANS") or "ignore").strip().lower()
    if deletion_means not in ("ignore", "complete", "delete"):
        deletion_means = "ignore"

    mass_missing_raw = (project.get("MASS_MISSING_ABORT") or "5").strip()
    mass_missing_abort = int(mass_missing_raw) if mass_missing_raw else 5

    # Multi-calendar routing: habit/routine projects render to a separate "Habits & Routines"
    # calendar; everything else goes to the default calendar. Set HABITS_PROJECT_IDS to the
    # TickTick project id(s) you want routed to the habits calendar (comma-separated); when
    # unset, all tasks go to the default calendar.
    habits_calendar = (project.get("HABITS_CALENDAR_NAME") or "Habits & Routines").strip()
    habits_ids_raw = (project.get("HABITS_PROJECT_IDS") or "").strip()
    habits_project_ids = frozenset(
        pid.strip() for pid in habits_ids_raw.split(",") if pid.strip()
    )

    return Config(
        ticktick_token=_require(tt_secrets, "TICKTICK_ACCESS_TOKEN"),
        icloud_username=_require(icloud_secrets, "ICLOUD_USERNAME"),
        icloud_password=_require(icloud_secrets, "ICLOUD_PASSWORD"),
        timezone=(project.get("TICKTICK_TIMEZONE") or "America/New_York").strip(),
        caldav_url=(project.get("CALDAV_URL") or "https://caldav.icloud.com").strip(),
        calendar_name=(project.get("CALENDAR_NAME") or "TickTick Tasks").strip(),
        habits_calendar_name=habits_calendar,
        habits_project_ids=habits_project_ids,
        default_duration_min=int((project.get("EVENT_DEFAULT_DURATION_MIN") or "30").strip()),
        horizon_days=horizon,
        two_way_enabled=two_way,
        cal_deletion_means=deletion_means,
        mass_missing_abort=mass_missing_abort,
        db_path=PROJECT_ROOT / "data" / "state.db",
    )
