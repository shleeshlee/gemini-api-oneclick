#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="${ROOT_DIR}/vendor/gemini-webapi-upstream/gemini_webapi"
LOCAL_DIR="${ROOT_DIR}/lib/gemini_webapi"
MODE="${1:-}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}

trap cleanup EXIT

if [[ ! -d "${UPSTREAM_DIR}" ]]; then
  echo "Missing upstream snapshot: ${UPSTREAM_DIR}" >&2
  exit 1
fi

if [[ ! -d "${LOCAL_DIR}" ]]; then
  echo "Missing local vendor dir: ${LOCAL_DIR}" >&2
  exit 1
fi

FILTERED_UPSTREAM_DIR="${TMP_DIR}/upstream"
FILTERED_LOCAL_DIR="${TMP_DIR}/local"

rsync -a --exclude '__pycache__' "${UPSTREAM_DIR}/" "${FILTERED_UPSTREAM_DIR}/"
rsync -a --exclude '__pycache__' "${LOCAL_DIR}/" "${FILTERED_LOCAL_DIR}/"

run_git_diff() {
  local status=0
  (
    cd "${TMP_DIR}"
    git --no-pager diff --no-index \
      --no-prefix \
      "$@" -- upstream local
  ) || status=$?
  if [[ "${status}" -gt 1 ]]; then
    exit "${status}"
  fi
}

case "${MODE}" in
  "" )
    run_git_diff --stat
    echo
    run_git_diff
    ;;
  --stat )
    run_git_diff --stat
    ;;
  --name-only )
    run_git_diff --name-only
    ;;
  * )
    echo "Usage: $0 [--stat|--name-only]" >&2
    exit 1
    ;;
esac
