#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SUDO=""
if [[ ${EUID:-$(id -u)} -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

WORKER_MODE="${WORKER_MODE:-true}"
IMAGE_NAME="${IMAGE_NAME:-gemini-api-oneclick:local}"
CONTAINER_PREFIX="${CONTAINER_PREFIX:-gemini_api_account_}"

is_worker_mode() {
  [[ "$WORKER_MODE" == "true" || "$WORKER_MODE" == "1" || "$WORKER_MODE" == "yes" ]]
}

disable_unit_if_exists() {
  local unit="$1"
  if command -v systemctl >/dev/null 2>&1 && systemctl cat "$unit" >/dev/null 2>&1; then
    $SUDO systemctl stop "$unit" 2>/dev/null || true
    $SUDO systemctl disable "$unit" 2>/dev/null || true
  fi
}

echo "Gemini API OneClick 卸载"
echo "工作目录: $ROOT_DIR"
if is_worker_mode; then
  echo "架构: worker"
else
  echo "架构: accounts"
fi
echo "保留: envs/ state/"
echo ""
read -rp "确认卸载当前架构？输入 yes: " confirm
[[ "$confirm" == "yes" ]] || exit 0

if is_worker_mode; then
  docker compose -f docker-compose.worker.yml down 2>/dev/null || true
  docker rm -f gemini_worker 2>/dev/null || true
else
  docker compose -f docker-compose.accounts.yml down 2>/dev/null || true
  disable_unit_if_exists gemini-containers.service
  disable_unit_if_exists gemini-delayed-start.service
  for env_file in envs/account*.env; do
    [[ -f "$env_file" ]] || continue
    n="${env_file##*account}"
    n="${n%.env}"
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    docker rm -f "${CONTAINER_PREFIX}${n}" 2>/dev/null || true
  done
fi

if command -v systemctl >/dev/null 2>&1; then
  if ! is_worker_mode; then
    $SUDO rm -f /etc/systemd/system/gemini-containers.service
    $SUDO rm -f /etc/systemd/system/gemini-delayed-start.service
  fi
  $SUDO systemctl daemon-reload 2>/dev/null || true
fi

if ! is_worker_mode; then
  rm -f docker-compose.accounts.yml
fi

read -rp "删除镜像 ${IMAGE_NAME}？[y/N]: " remove_image
if [[ "$remove_image" =~ ^[Yy]$ ]]; then
  docker rmi "$IMAGE_NAME" 2>/dev/null || true
fi

echo ""
echo "卸载完成。envs/ 和 state/ 已保留，Gateway 与另一套架构未改动。"
