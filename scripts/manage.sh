#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Load .env
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

CONTAINER_PREFIX="${CONTAINER_PREFIX:-gemini_api_account_}"
START_PORT="${START_PORT:-8001}"

port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -tlnH "sport = :$1" 2>/dev/null | grep -q .
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1
  else
    (echo >/dev/tcp/127.0.0.1/"$1") 2>/dev/null
  fi
}

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
PINK='\033[38;5;205m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Find max account number ──
find_max_account() {
  local max=0
  for f in envs/account*.env; do
    [[ -f "$f" ]] || continue
    local n="${f##*account}"
    n="${n%.env}"
    if [[ "$n" =~ ^[0-9]+$ ]] && (( n > max )); then
      max=$n
    fi
  done
  echo "$max"
}

# ── Count accounts ──
count_accounts() {
  local count=0
  for f in envs/account*.env; do
    [[ -f "$f" ]] || continue
    count=$((count + 1))
  done
  echo "$count"
}

# ══════════════════════════════════════════════════════════════
# [1] Add containers
# ══════════════════════════════════════════════════════════════
do_add() {
  local current
  current=$(count_accounts)
  local max
  max=$(find_max_account)

  info "Current accounts: $current (max ID: $max)"
  echo ""
  read -rp "How many containers to add? [1]: " add_count
  add_count="${add_count:-1}"

  if ! [[ "$add_count" =~ ^[0-9]+$ ]] || (( add_count < 1 || add_count > 50 )); then
    error "Invalid number (1-50)"
    return
  fi

  local start=$((max + 1))
  local end=$((max + add_count))

  # Check for port conflicts
  local has_conflict=false
  for (( i=start; i<=end; i++ )); do
    local port=$(( START_PORT + i - 1 ))
    if port_in_use "$port"; then
      warn "Port $port (for account #$i) is already in use!"
      has_conflict=true
    fi
  done
  if $has_conflict; then
    read -rp "Ports conflict detected. Continue anyway? [y/N]: " force
    if [[ ! "$force" =~ ^[Yy]$ ]]; then
      info "Cancelled. Change START_PORT in .env or free up the ports first."
      return
    fi
  fi

  info "Creating account${start}.env ~ account${end}.env ..."
  mkdir -p envs cookie-cache

  for (( i=start; i<=end; i++ )); do
    local env_file="envs/account${i}.env"
    if [[ -f "$env_file" ]]; then
      warn "$env_file already exists, skipping"
      continue
    fi
    cat > "$env_file" <<EOF
API_KEY=
SECURE_1PSID=
SECURE_1PSIDTS=
EOF
    mkdir -p "cookie-cache/account${i}"
    echo "  Created $env_file"
  done

  info "Regenerating docker-compose ..."
  python3 scripts/generate_compose.py

  info "Starting new containers (incremental) ..."
  docker compose -f docker-compose.accounts.yml up -d --build --no-recreate

  echo ""
  info "Done! $add_count container(s) added."
  info "Next: fill cookies via Cookie Manager or edit envs/account*.env directly"
}

# ══════════════════════════════════════════════════════════════
# [2] View status
# ══════════════════════════════════════════════════════════════
do_status() {
  echo ""
  echo -e "${PINK}Container Status${NC}"
  echo "──────────────────────────────────────────────"
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' \
    | grep -E "(NAMES|${CONTAINER_PREFIX})" || echo "  No containers found"
  echo ""

  local total
  total=$(count_accounts)
  info "Account env files: $total"

  # Quick health check
  echo ""
  echo -e "${PINK}Health Check${NC}"
  echo "──────────────────────────────────────────────"
  for f in envs/account*.env; do
    [[ -f "$f" ]] || continue
    local n="${f##*account}"
    n="${n%.env}"
    local port=$(( START_PORT + n - 1 ))
    if curl -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      echo -e "  Account #${n} (port ${port}): ${GREEN}OK${NC}"
    else
      echo -e "  Account #${n} (port ${port}): ${RED}FAIL${NC}"
    fi
  done
}

# ══════════════════════════════════════════════════════════════
# [3] Full uninstall
# ══════════════════════════════════════════════════════════════
do_uninstall() {
  echo ""
  warn "This will:"
  echo "  - Stop and remove all Gemini API containers"
  echo "  - Remove generated compose/config files"
  echo "  - Stop cookie-manager systemd service (if installed)"
  echo "  - Remove channel_guard cron job (if installed)"
  echo ""
  echo -e "  ${YELLOW}NOTE: envs/ directory will be PRESERVED (contains your cookies)${NC}"
  echo ""
  read -rp "Are you sure? Type 'yes' to confirm: " confirm
  if [[ "$confirm" != "yes" ]]; then
    info "Cancelled"
    return
  fi

  # Stop containers
  if [[ -f docker-compose.accounts.yml ]]; then
    info "Stopping containers ..."
    docker compose -f docker-compose.accounts.yml down || true
  fi

  # Remove generated files
  info "Removing generated files ..."
  rm -f docker-compose.accounts.yml
  rm -f .env

  # Stop cookie-manager service
  if systemctl is-active --quiet cookie-manager 2>/dev/null; then
    info "Stopping cookie-manager service ..."
    sudo systemctl stop cookie-manager || true
    sudo systemctl disable cookie-manager || true
    sudo rm -f /etc/systemd/system/cookie-manager.service
    sudo systemctl daemon-reload || true
  fi

  # Remove cron
  if crontab -l 2>/dev/null | grep -q "channel_guard"; then
    info "Removing channel_guard cron ..."
    ./ops/install_cron.sh --remove || true
  fi

  # Ask about docker images
  echo ""
  read -rp "Remove docker image ${IMAGE_NAME:-gemini-api-oneclick:local}? [y/N]: " rm_image
  if [[ "$rm_image" =~ ^[Yy]$ ]]; then
    docker rmi "${IMAGE_NAME:-gemini-api-oneclick:local}" 2>/dev/null || true
    info "Image removed"
  fi

  # Clean runtime dirs
  rm -rf cookie-cache state

  echo ""
  info "Uninstall complete."
  info "Your envs/ directory has been preserved."
  info "To fully remove, run: rm -rf $(pwd)"
}

# ══════════════════════════════════════════════════════════════
# Menu
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${PINK}Gemini API OneClick - Container Manager${NC}"
echo "══════════════════════════════════════════"
echo ""
echo "  [1] Add containers"
echo "  [2] View status"
echo "  [3] Full uninstall"
echo "  [q] Exit"
echo ""
read -rp "Choose [1-3/q]: " choice

case "$choice" in
  1) do_add ;;
  2) do_status ;;
  3) do_uninstall ;;
  q|Q) exit 0 ;;
  *) error "Invalid choice"; exit 1 ;;
esac
