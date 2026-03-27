#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/Gemini-API" >&2
  exit 1
fi

SOURCE_ROOT="${1%/}"
SOURCE_DIR="${SOURCE_ROOT}/src/gemini_webapi"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${ROOT_DIR}/vendor/gemini-webapi-upstream/gemini_webapi"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "Missing upstream package dir: ${SOURCE_DIR}" >&2
  exit 1
fi

mkdir -p "$(dirname "${TARGET_DIR}")"
rsync -a --delete --exclude '__pycache__' "${SOURCE_DIR}/" "${TARGET_DIR}/"

echo "Refreshed upstream snapshot into ${TARGET_DIR}"
echo "Next: update vendor/gemini-webapi-upstream/manifest.json with the new commit/date."
