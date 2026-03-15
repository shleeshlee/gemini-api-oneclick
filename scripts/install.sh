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
echo -e "${PINK}║${NC}  ${BOLD}Gemini API OneClick${NC} — 多账号智能网关            ${PINK}║${NC}"
echo -e "${PINK}║${NC}  作者: WanWan                                  ${PINK}║${NC}"
echo -e "${PINK}║${NC}  GitHub: shleeshlee/gemini-api-oneclick         ${PINK}║${NC}"
echo -e "${PINK}║${NC}  ${GREEN}免费开源${NC} | MIT 协议                            ${PINK}║${NC}"
echo -e "${PINK}║${NC}  ${RED}收费 = 被骗!${NC}                                  ${PINK}║${NC}"
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
info "检查依赖 ..."
need_cmd docker
need_cmd python3

if ! docker compose version >/dev/null 2>&1; then
  error "未找到 docker compose（需要 Docker Compose v2）"
fi
info "依赖检查通过"
echo ""

# ══════════════════════════════════════════════════════════════
# Detect existing installation (update mode)
# ══════════════════════════════════════════════════════════════
if [[ -f .env ]]; then
  echo -e "${YELLOW}检测到已有安装！${NC}"
  echo ""
  echo "  [1] 更新（拉取最新代码 + 重建，保留账号配置）"
  echo "  [2] 全新安装（重新配置所有选项）"
  echo "  [q] 取消"
  echo ""
  read -rp "Choose [1/2/q]: " update_choice

  case "$update_choice" in
    1)
      info "正在更新 ..."
      git pull --ff-only 2>/dev/null || warn "git pull 失败（非 git 仓库或有冲突）"

      # shellcheck disable=SC1091
      source .env

      info "重新生成 compose ..."
      python3 scripts/generate_compose.py

      info "重建并重启容器 ..."
      docker compose -f docker-compose.accounts.yml up -d --build

      # 重启 Gateway
      if command -v systemctl >/dev/null 2>&1 && systemctl is-active gemini-gateway >/dev/null 2>&1; then
        sudo systemctl restart gemini-gateway
        info "Gateway 已重启"
      fi

      echo ""
      info "更新完成！"
      ./scripts/healthcheck.sh || true
      exit 0
      ;;
    2)
      info "开始全新安装 ..."
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
echo -e "${BOLD}交互式安装向导${NC}"
echo "════════════════════════════════════════"
echo ""

# [1/5] Container count
step "1/5" "需要多少个容器（账号）？"
read -rp "  数量 [1-50, 默认 5]: " ACCOUNT_COUNT
ACCOUNT_COUNT="${ACCOUNT_COUNT:-5}"
if ! [[ "$ACCOUNT_COUNT" =~ ^[0-9]+$ ]] || (( ACCOUNT_COUNT < 1 || ACCOUNT_COUNT > 50 )); then
  error "数量无效（需要 1-50）"
fi
echo ""

# Port detection: find free range for ACCOUNT_COUNT containers
DEFAULT_START=8001
START_PORT=$(find_free_port_range "$ACCOUNT_COUNT" "$DEFAULT_START") || error "Cannot find $ACCOUNT_COUNT consecutive free ports starting from $DEFAULT_START"

if (( START_PORT != DEFAULT_START )); then
  warn "端口 $DEFAULT_START 已被占用，自动偏移到 ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1))"
else
  info "端口 ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1)) 可用"
fi
echo ""

# [2/5] API key
step "2/5" "API 密钥（回车自动生成）"
read -rp "  API_KEY [自动]: " USER_API_KEY
if [[ -z "$USER_API_KEY" ]]; then
  USER_API_KEY=$(random_key)
  info "已生成 API 密钥: $USER_API_KEY"
fi
echo ""

# [3/5] Proxy
step "3/5" "出站代理（用于访问 Gemini）"
read -rp "  使用代理？[y/N]: " USE_PROXY
HTTP_PROXY=""
HTTPS_PROXY=""
if [[ "$USE_PROXY" =~ ^[Yy]$ ]]; then
  read -rp "  HTTP_PROXY (e.g. http://host:port): " HTTP_PROXY
  HTTPS_PROXY="${HTTP_PROXY}"
fi
echo ""

# [4/5] Cookie manager
step "4/5" "安装 Cookie 管理面板？"
read -rp "  启用？[Y/n]: " USE_COOKIE_MGR
USE_COOKIE_MGR="${USE_COOKIE_MGR:-Y}"
COOKIE_MANAGER_PORT="9880"
COOKIE_MANAGER_PASSWORD=""
if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  read -rp "  面板端口 [9880]: " COOKIE_MANAGER_PORT
  COOKIE_MANAGER_PORT="${COOKIE_MANAGER_PORT:-9880}"
  read -rp "  面板密码 [自动]: " COOKIE_MANAGER_PASSWORD
  if [[ -z "$COOKIE_MANAGER_PASSWORD" ]]; then
    COOKIE_MANAGER_PASSWORD=$(random_key | head -c 16)
    info "已生成面板密码: $COOKIE_MANAGER_PASSWORD"
  fi
fi
echo ""

# [5/5] Channel guard
step "5/5" "启用渠道守卫？（需要 NewAPI 集成）"
read -rp "  启用？[y/N]: " USE_GUARD
ENABLE_CHANNEL_GUARD="false"
NEWAPI_DB_PASS="change_me"
if [[ "$USE_GUARD" =~ ^[Yy]$ ]]; then
  ENABLE_CHANNEL_GUARD="true"
  read -rp "  NewAPI MySQL 密码: " NEWAPI_DB_PASS
  if [[ -z "$NEWAPI_DB_PASS" || "$NEWAPI_DB_PASS" == "change_me" ]]; then
    warn "密码无效，渠道守卫将跳过"
    ENABLE_CHANNEL_GUARD="false"
  fi
fi
echo ""

# ══════════════════════════════════════════════════════════════
# Execute installation
# ══════════════════════════════════════════════════════════════
TOTAL_STEPS=6  # base: env + .env + compose + build + gateway + health
if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  TOTAL_STEPS=$((TOTAL_STEPS + 1))
fi
if [[ "$ENABLE_CHANNEL_GUARD" == "true" ]]; then
  TOTAL_STEPS=$((TOTAL_STEPS + 1))
fi

# Gateway port = START_PORT + ACCOUNT_COUNT (first free port after containers)
GATEWAY_PORT=$((START_PORT + ACCOUNT_COUNT))

CURRENT_STEP=0

# Step: Create env files
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "创建账号配置文件 ..."
mkdir -p envs cookie-cache state

for (( i=1; i<=ACCOUNT_COUNT; i++ )); do
  env_file="envs/account${i}.env"
  if [[ -f "$env_file" ]]; then
    echo "  $env_file 已存在，保留"
  else
    cat > "$env_file" <<EOF
API_KEY=
SECURE_1PSID=
SECURE_1PSIDTS=
EOF
    echo "  已创建 $env_file"
  fi
  mkdir -p "cookie-cache/account${i}"
done

# Step: Write .env
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "写入 .env 配置 ..."

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

# Gateway (智能轮询总入口)
GATEWAY_PORT=${GATEWAY_PORT}

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

info ".env 已写入"

# Step: Generate compose
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "生成 docker-compose ..."
python3 scripts/generate_compose.py

# Step: Build and start
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "构建并启动容器 ..."
docker compose -f docker-compose.accounts.yml up -d --build

# Step: Gateway (智能轮询网关)
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "部署智能轮询网关 (端口 ${GATEWAY_PORT}) ..."

# 安装 gateway 依赖
pip3 install -q fastapi uvicorn httpx 2>/dev/null || pip3 install --break-system-packages -q fastapi uvicorn httpx 2>/dev/null || warn "Gateway 依赖安装失败，请手动安装: pip3 install fastapi uvicorn httpx"

if command -v systemctl >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3)
  SERVICE_FILE="/etc/systemd/system/gemini-gateway.service"

  sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Gemini API Gateway — 智能轮询网关
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
ExecStart=${PYTHON_BIN} ${ROOT_DIR}/gateway.py
Restart=on-failure
RestartSec=5
Environment=GATEWAY_PORT=${GATEWAY_PORT}
Environment=BASE_PORT=${START_PORT}
EnvironmentFile=${ROOT_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable gemini-gateway
  sudo systemctl start gemini-gateway
  info "Gateway 已安装为系统服务"
else
  warn "未找到 systemctl，后台启动 Gateway ..."
  GATEWAY_PORT=${GATEWAY_PORT} BASE_PORT=${START_PORT} nohup python3 gateway.py > /tmp/gemini-gateway.log 2>&1 &
  info "Gateway 已启动 (PID: $!)"
fi

# Step: Cookie manager (optional)
if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  CURRENT_STEP=$((CURRENT_STEP + 1))
  step "${CURRENT_STEP}/${TOTAL_STEPS}" "部署 Cookie 管理面板 ..."

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
    info "Cookie 管理面板已安装为系统服务"
  else
    warn "未找到 systemctl，后台启动 Cookie 管理面板 ..."
    nohup python3 web/cookie-manager.py > /tmp/cookie-manager.log 2>&1 &
    info "Cookie 管理面板已启动 (PID: $!)"
  fi
fi

# Step: Channel guard cron (optional)
if [[ "$ENABLE_CHANNEL_GUARD" == "true" ]]; then
  CURRENT_STEP=$((CURRENT_STEP + 1))
  step "${CURRENT_STEP}/${TOTAL_STEPS}" "安装渠道守卫定时任务 ..."
  ./ops/install_cron.sh
fi

# Step: Health check
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "健康检查 ..."
sleep 3
./scripts/healthcheck.sh || true

# ══════════════════════════════════════════════════════════════
# Done!
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  安装完成！${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}${CYAN}▸ 统一 API 入口（推荐使用）${NC}"
echo -e "  ${BOLD}地址:${NC}       http://YOUR_IP:${GATEWAY_PORT}"
echo -e "  ${BOLD}聊天:${NC}       http://YOUR_IP:${GATEWAY_PORT}/v1/chat/completions"
echo -e "  ${BOLD}生图:${NC}       http://YOUR_IP:${GATEWAY_PORT}/v1/images/generations"
echo -e "  ${BOLD}模型列表:${NC}   http://YOUR_IP:${GATEWAY_PORT}/v1/models"
echo -e "  ${BOLD}状态面板:${NC}   http://YOUR_IP:${GATEWAY_PORT}"
echo -e "  ${BOLD}API 密钥:${NC}   ${USER_API_KEY}"
echo ""
echo -e "  智能轮询 ${ACCOUNT_COUNT} 个容器，自动跳过故障节点"
echo -e "  支持 OpenAI 兼容格式，可直接接入酒馆/Kelivo/NewAPI 等"

if [[ ! "$USE_COOKIE_MGR" =~ ^[Nn]$ ]]; then
  echo ""
  echo -e "  ${BOLD}${CYAN}▸ Cookie 管理面板${NC}"
  echo -e "  ${BOLD}地址:${NC}       http://YOUR_IP:${COOKIE_MANAGER_PORT}"
  echo -e "  ${BOLD}密码:${NC}       ${COOKIE_MANAGER_PASSWORD}"
fi

# Detect Docker bridge gateway for NewAPI channel config
DOCKER_GW=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo "172.17.0.1")

echo ""
echo -e "  ${BOLD}${CYAN}▸ NewAPI 渠道配置（可选）${NC}"
echo -e "  如果你的 NewAPI 也跑在 Docker 里，可以把 Gateway 整体接入："
echo -e "    ${BOLD}http://${DOCKER_GW}:${GATEWAY_PORT}${NC}"
echo -e "  或者单独接入每个容器："
for (( i=1; i<=ACCOUNT_COUNT; i++ )); do
  port=$((START_PORT + i - 1))
  echo -e "    容器 #${i}: ${BOLD}http://${DOCKER_GW}:${port}${NC}"
done

echo ""
echo -e "  ${YELLOW}下一步:${NC} 打开 Cookie 管理面板，填入你的 Gemini Cookie！"
echo ""
echo -e "${PINK}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${PINK}║${NC}  ${BOLD}Gemini API OneClick${NC} by WanWan                 ${PINK}║${NC}"
echo -e "${PINK}║${NC}  觉得好用的话，给个 Star 吧！                 ${PINK}║${NC}"
echo -e "${PINK}║${NC}  github.com/shleeshlee/gemini-api-oneclick     ${PINK}║${NC}"
echo -e "${PINK}╚═══════════════════════════════════════════════╝${NC}"
echo ""
