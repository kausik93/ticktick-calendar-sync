#!/usr/bin/env bash
# Run one sync pass. Intended to be invoked from cron / a systemd timer.
# Pass-through args are forwarded to the CLI (e.g. ./run.sh --dry-run).
set -euo pipefail
cd "$(dirname "$0")"
exec uv run sync-ticktick-cal "$@"
