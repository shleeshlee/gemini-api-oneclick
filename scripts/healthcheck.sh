#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

START_PORT="${START_PORT:-8001}"

status_ok=0
status_fail=0

for f in envs/account*.env; do
  [[ -f "$f" ]] || continue
  n="${f##*account}"
  n="${n%.env}"
  if ! [[ "$n" =~ ^[0-9]+$ ]]; then
    continue
  fi
  port=$((START_PORT + n - 1))
  if out=$(curl -fsS "http://127.0.0.1:${port}/health" 2>/dev/null); then
    echo "[OK] account${n} port=${port} ${out}"
    status_ok=$((status_ok + 1))
  else
    echo "[FAIL] account${n} port=${port}"
    status_fail=$((status_fail + 1))
  fi
done

echo "summary: ok=${status_ok} fail=${status_fail}"
[[ "$status_fail" -eq 0 ]]
