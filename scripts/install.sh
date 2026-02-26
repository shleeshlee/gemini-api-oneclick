#!/usr/bin/env bash
set -euo pipefail

# ── Colors (need early for bootstrap messages) ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
PINK='\033[38;5;205m'
BOLD='\033[1m'
NC='\033[0m'

# ══════════════════════════════════════════════════════════════
# Bootstrap: auto-clone if running via curl pipe
# ══════════════════════════════════════════════════════════════
REPO_URL="https://github.com/shleeshlee/gemini-api-oneclick.git"

_in_repo() {
  [[ -f "app/main.py" ]] && [[ -f "scripts/install.sh" ]]
}

if _in_repo; then
  # Already in the repo root
  ROOT_DIR="$(pwd)"
elif [[ -n "${BASH_SOURCE[0]:-}" ]] && [[ -f "$(dirname "${BASH_SOURCE[0]}")/../app/main.py" ]]; then
  # Running from scripts/ inside the repo
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  # Running from curl pipe or outside the repo — need to clone
  command -v git >/dev/null 2>&1 || { echo -e "${RED}[x] git is required. Install git first.${NC}"; exit 1; }

  DEFAULT_DIR="$HOME/gemini-api-oneclick"
  echo ""
  echo -e "${PINK}Gemini API OneClick — One-Line Installer${NC}"
  echo ""
  read -rp "Install to [$DEFAULT_DIR]: " INSTALL_DIR
  INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo -e "${GREEN}[*]${NC} Existing repo found, updating ..."
    cd "$INSTALL_DIR"
    git pull --ff-only || echo -e "${YELLOW}[!]${NC} git pull failed, continuing with current version"
  else
    echo -e "${GREEN}[*]${NC} Cloning repo ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi
  ROOT_DIR="$(pwd)"
fi

cd "$ROOT_DIR"

# ══════════════════════════════════════════════════════════════
# ASCII Banner
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${PINK}"
cat << 'BANNER'
   ____                _       _      _    ____ ___
  / ___| ___ _ __ ___ (_)_ __ (_)    / \  |  _ \_ _|
 | |  _ / _ \ '_ ` _ \| | '_ \| |   / _ \ | |_) | |
 | |_| |  __/ | | | | | | | | | |  / ___ \|  __/| |
  \____|\___|_| |_| |_|_|_| |_|_| /_/   \_\_|  |___|

   ___              ____ _ _      _
  / _ \ _ __   ___ / ___| (_) ___| | __
 | | | | '_ \ / _ \ |   | | |/ __| |/ /
 | |_| | | | |  __/ |___| | | (__|   <
  \___/|_| |_|\___|\____|_|_|\___|_|\_\
BANNER
echo -e "${NC}"

echo -e "${PINK}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${PINK}║${NC}  ${BOLD}Gemini API OneClick${NC} — Multi-Account Gateway   ${PINK}║${NC}"
echo -e "${PINK}║${NC}  Author: WanWan                                ${PINK}║${NC}"
echo -e "${PINK}║${NC}  GitHub: shleeshlee/gemini-api-oneclick         ${PINK}║${NC}"
echo -e "${PINK}║${NC}  ${GREEN}Free & Open Source${NC} | MIT License              ${PINK}║${NC}"
echo -e "${PINK}║${NC}  ${RED}Paid = Scammed!${NC}                               ${PINK}║${NC}"
echo -e "${PINK}╚═══════════════════════════════════════════════╝${NC}"
echo ""

# ── Helpers ──
info()  { echo -e "${GREEN}[*]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; exit 1; }
step()  { echo -e "${CYAN}[$1]${NC} $2"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || error "Missing required command: $1"
}

random_key() {
  head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32
}

port_in_use() {
  # Check if a port is occupied (works on Linux and macOS)
  if command -v ss >/dev/null 2>&1; then
    ss -tlnH "sport = :$1" 2>/dev/null | grep -q .
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1
  else
    # Fallback: try connecting
    (echo >/dev/tcp/127.0.0.1/"$1") 2>/dev/null
  fi
}

find_free_port_range() {
  # Find a starting port where $1 consecutive ports are all free
  local need=$1
  local try=${2:-8001}
  local max=65000
  while (( try + need - 1 <= max )); do
    local all_free=true
    for (( p=try; p<try+need; p++ )); do
      if port_in_use "$p"; then
        all_free=false
        try=$((p + 1))
        break
      fi
    done
    if $all_free; then
      echo "$try"
      return 0
    fi
  done
  return 1
}

# ══════════════════════════════════════════════════════════════
# Dependency check
# ══════════════════════════════════════════════════════════════
info "Checking dependencies ..."
need_cmd docker
need_cmd python3

if ! docker compose version >/dev/null 2>&1; then
  error "docker compose not found (need Docker Compose v2)"
fi
info "All dependencies OK"
echo ""

# ══════════════════════════════════════════════════════════════
# Detect existing installation (update mode)
# ══════════════════════════════════════════════════════════════
if [[ -f .env ]]; then
  echo -e "${YELLOW}Existing installation detected!${NC}"
  echo ""
  echo "  [1] Update (git pull + rebuild, keep envs/)"
  echo "  [2] Fresh install (reconfigure everything)"
  echo "  [q] Cancel"
  echo ""
  read -rp "Choose [1/2/q]: " update_choice

  case "$update_choice" in
    1)
      info "Updating ..."
      git pull --ff-only 2>/dev/null || warn "git pull failed (not a git repo or conflicts)"

      # shellcheck disable=SC1091
      source .env

      info "Regenerating compose ..."
      python3 scripts/generate_compose.py

      info "Rebuilding and restarting ..."
      docker compose -f docker-compose.accounts.yml up -d --build

      echo ""
      info "Update complete!"
      ./scripts/healthcheck.sh || true
      exit 0
      ;;
    2)
      info "Starting fresh install ..."
      ;;
    q|Q)
      info "Cancelled"
      exit 0
      ;;
    *)
      error "Invalid choice"
      ;;
  esac
fi

# ══════════════════════════════════════════════════════════════
# Interactive wizard (fresh install)
# ══════════════════════════════════════════════════════════════
echo -e "${BOLD}Interactive Setup${NC}"
echo "════════════════════════════════════════"
echo ""

# [1/5] Container count
step "1/5" "How many containers (accounts)?"
read -rp "  Count [1-50, default 5]: " ACCOUNT_COUNT
ACCOUNT_COUNT="${ACCOUNT_COUNT:-5}"
if ! [[ "$ACCOUNT_COUNT" =~ ^[0-9]+$ ]] || (( ACCOUNT_COUNT < 1 || ACCOUNT_COUNT > 50 )); then
  error "Invalid count (must be 1-50)"
fi
echo ""

# Port detection: find free range for ACCOUNT_COUNT containers
DEFAULT_START=8001
START_PORT=$(find_free_port_range "$ACCOUNT_COUNT" "$DEFAULT_START") || error "Cannot find $ACCOUNT_COUNT consecutive free ports starting from $DEFAULT_START"

if (( START_PORT != DEFAULT_START )); then
  warn "Port $DEFAULT_START already in use, auto-shifted to ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1))"
else
  info "Ports ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1)) are available"
fi
echo ""

# [2/5] API key
step "2/5" "API key for authentication (press Enter to auto-generate)"
read -rp "  API_KEY [auto]: " USER_API_KEY
if [[ -z "$USER_API_KEY" ]]; then
  USER_API_KEY=$(random_key)
  info "Generated API key: $USER_API_KEY"
fi
echo ""

# [3/5] Proxy
step "3/5" "Outbound proxy? (for accessing Gemini)"
read -rp "  Use proxy? [y/N]: " USE_PROXY
HTTP_PROXY=""
HTTPS_PROXY=""
if [[ "$USE_PROXY" =~ ^[Yy]$ ]]; then
  read -rp "  HTTP_PROXY (e.g. http://host:port): " HTTP_PROXY
  HTTPS_PROXY="${HTTP_PROXY}"
fi
echo ""

# [4/5] Cookie manager
step "4/5" "Install Cookie Manager web panel?"
read -rp "  Enable? [Y/n]: " USE_COOKIE_MGR
USE_COOKIE_MGR="${USE_COOKIE_MGR:-Y}"
COOKIE_MANAGER_PORT="9880"
COOKIE_MANAGER_PASSWORD=""
if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  read -rp "  Panel port [9880]: " COOKIE_MANAGER_PORT
  COOKIE_MANAGER_PORT="${COOKIE_MANAGER_PORT:-9880}"
  read -rp "  Panel password [auto]: " COOKIE_MANAGER_PASSWORD
  if [[ -z "$COOKIE_MANAGER_PASSWORD" ]]; then
    COOKIE_MANAGER_PASSWORD=$(random_key | head -c 16)
    info "Generated panel password: $COOKIE_MANAGER_PASSWORD"
  fi
fi
echo ""

# [5/5] Channel guard
step "5/5" "Enable channel_guard? (requires NewAPI integration)"
read -rp "  Enable? [y/N]: " USE_GUARD
ENABLE_CHANNEL_GUARD="false"
NEWAPI_DB_PASS="change_me"
if [[ "$USE_GUARD" =~ ^[Yy]$ ]]; then
  ENABLE_CHANNEL_GUARD="true"
  read -rp "  NewAPI MySQL password: " NEWAPI_DB_PASS
  if [[ -z "$NEWAPI_DB_PASS" || "$NEWAPI_DB_PASS" == "change_me" ]]; then
    warn "Invalid DB password, channel_guard will be skipped"
    ENABLE_CHANNEL_GUARD="false"
  fi
fi
echo ""

# ══════════════════════════════════════════════════════════════
# Execute installation
# ══════════════════════════════════════════════════════════════
TOTAL_STEPS=5
if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  TOTAL_STEPS=6
fi
if [[ "$ENABLE_CHANNEL_GUARD" == "true" ]]; then
  TOTAL_STEPS=$((TOTAL_STEPS + 1))
fi

CURRENT_STEP=0

# Step: Create env files
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "Creating account env files ..."
mkdir -p envs cookie-cache state

for (( i=1; i<=ACCOUNT_COUNT; i++ )); do
  env_file="envs/account${i}.env"
  if [[ -f "$env_file" ]]; then
    echo "  $env_file already exists, keeping"
  else
    cat > "$env_file" <<EOF
API_KEY=
SECURE_1PSID=
SECURE_1PSIDTS=
EOF
    echo "  Created $env_file"
  fi
  mkdir -p "cookie-cache/account${i}"
done

# Step: Write .env
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "Writing .env ..."

cat > .env <<EOF
# Gemini API OneClick - Configuration
# Generated by install.sh

# Basic
TZ=UTC
IMAGE_NAME=gemini-api-oneclick:local
START_PORT=${START_PORT}
CONTAINER_PREFIX=gemini_api_account_
API_KEY=${USER_API_KEY}

# Outbound proxy (optional)
HTTP_PROXY=${HTTP_PROXY}
HTTPS_PROXY=${HTTPS_PROXY}
NO_PROXY=localhost,127.0.0.1

# Cookie Manager
COOKIE_MANAGER_PORT=${COOKIE_MANAGER_PORT}
COOKIE_MANAGER_PASSWORD=${COOKIE_MANAGER_PASSWORD}

# Channel guard (newapi integration)
ENABLE_CHANNEL_GUARD=${ENABLE_CHANNEL_GUARD}
GUARD_ERROR_THRESHOLD=3
GUARD_LOG_WINDOW_SECONDS=300
GUARD_NEWAPI_CONTAINER=newapi-new-api-1
GUARD_NEWAPI_MYSQL_CONTAINER=newapi-mysql-1
NEWAPI_DB_NAME=new-api
NEWAPI_DB_USER=root
NEWAPI_DB_PASS=${NEWAPI_DB_PASS}
EOF

info ".env written"

# Step: Generate compose
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "Generating docker-compose ..."
python3 scripts/generate_compose.py

# Step: Build and start
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "Building and starting containers ..."
docker compose -f docker-compose.accounts.yml up -d --build

# Step: Cookie manager (optional)
if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  CURRENT_STEP=$((CURRENT_STEP + 1))
  step "${CURRENT_STEP}/${TOTAL_STEPS}" "Setting up Cookie Manager ..."

  # Install as systemd service if systemctl is available
  if command -v systemctl >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v python3)
    SERVICE_FILE="/etc/systemd/system/cookie-manager.service"

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Gemini API Cookie Manager
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
ExecStart=${PYTHON_BIN} ${ROOT_DIR}/web/cookie-manager.py
Restart=on-failure
RestartSec=5
EnvironmentFile=${ROOT_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable cookie-manager
    sudo systemctl start cookie-manager
    info "Cookie Manager installed as systemd service"
  else
    warn "systemctl not found, starting Cookie Manager in background ..."
    nohup python3 web/cookie-manager.py > /tmp/cookie-manager.log 2>&1 &
    info "Cookie Manager started (PID: $!)"
  fi
fi

# Step: Channel guard cron (optional)
if [[ "$ENABLE_CHANNEL_GUARD" == "true" ]]; then
  CURRENT_STEP=$((CURRENT_STEP + 1))
  step "${CURRENT_STEP}/${TOTAL_STEPS}" "Installing channel_guard cron ..."
  ./ops/install_cron.sh
fi

# Step: Health check
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "Running health check ..."
sleep 3
./scripts/healthcheck.sh || true

# ══════════════════════════════════════════════════════════════
# Done!
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation Complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}API Endpoint:${NC}  http://YOUR_IP:${START_PORT}/v1/chat/completions"
echo -e "  ${BOLD}API Key:${NC}       ${USER_API_KEY}"
echo -e "  ${BOLD}Containers:${NC}    ${ACCOUNT_COUNT} (ports ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1)))"

if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  echo ""
  echo -e "  ${BOLD}Cookie Panel:${NC}  http://YOUR_IP:${COOKIE_MANAGER_PORT}"
  echo -e "  ${BOLD}Panel Password:${NC} ${COOKIE_MANAGER_PASSWORD}"
fi

# Detect Docker bridge gateway for NewAPI channel config
DOCKER_GW=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo "172.17.0.1")

echo ""
echo -e "  ${BOLD}${CYAN}NewAPI 渠道配置${NC}"
echo -e "  如果你的 NewAPI 也跑在 Docker 里，添加渠道时填以下地址："
echo ""
for (( i=1; i<=ACCOUNT_COUNT; i++ )); do
  port=$((START_PORT + i - 1))
  echo -e "    Account #${i}: ${BOLD}http://${DOCKER_GW}:${port}/v1/chat/completions${NC}"
done
echo ""
echo -e "  (网关地址 ${DOCKER_GW} 已自动检测，如果 NewAPI 直接跑在宿主机则用 127.0.0.1)"

echo ""
echo -e "  ${YELLOW}Next step:${NC} Open Cookie Manager and fill in your Gemini cookies!"
echo ""
echo -e "${PINK}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${PINK}║${NC}  ${BOLD}Gemini API OneClick${NC} by WanWan                 ${PINK}║${NC}"
echo -e "${PINK}║${NC}  If this helped you, please give us a Star!    ${PINK}║${NC}"
echo -e "${PINK}║${NC}  github.com/shleeshlee/gemini-api-oneclick     ${PINK}║${NC}"
echo -e "${PINK}╚═══════════════════════════════════════════════╝${NC}"
echo ""
