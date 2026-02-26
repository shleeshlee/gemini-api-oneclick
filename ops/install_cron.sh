#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

ENABLE_CHANNEL_GUARD="${ENABLE_CHANNEL_GUARD:-true}"
CRON_CMD="flock -n /tmp/channel_guard.lock /usr/bin/env bash -lc 'cd ${ROOT_DIR} && source .env >/dev/null 2>&1 || true; /usr/bin/python3 ${ROOT_DIR}/ops/channel_guard.py >/dev/null 2>&1'"
CRON_LINE="* * * * * ${CRON_CMD}"

if [[ "${1:-}" == "--remove" ]]; then
  (crontab -l 2>/dev/null | grep -v "channel_guard.py" || true) | crontab -
  echo "Removed channel_guard cron"
  exit 0
fi

if [[ "$ENABLE_CHANNEL_GUARD" != "true" ]]; then
  echo "ENABLE_CHANNEL_GUARD=${ENABLE_CHANNEL_GUARD}, skip cron install"
  exit 0
fi

if [[ -z "${NEWAPI_DB_PASS:-}" || "${NEWAPI_DB_PASS:-}" == "change_me" ]]; then
  echo "NEWAPI_DB_PASS is empty/default, skip cron install"
  exit 0
fi

(crontab -l 2>/dev/null | grep -v "channel_guard.py" || true; echo "$CRON_LINE") | crontab -
echo "Installed channel_guard cron"
