#!/usr/bin/env bash
set -euo pipefail

# Use sudo only if not root
SUDO=""
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 && SUDO="sudo" || { echo "Not root and no sudo found"; exit 1; }
fi

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

# Auto-install missing system packages
install_pkg() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq "$@"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q "$@"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q "$@"
  else
    error "无法自动安装 $*，请手动安装"
  fi
}

# Docker
if ! command -v docker >/dev/null 2>&1; then
  warn "未找到 Docker，正在安装 ..."
  curl -fsSL https://get.docker.com | sh || error "Docker 安装失败"
  systemctl enable --now docker 2>/dev/null || true
fi
need_cmd docker

if ! docker compose version >/dev/null 2>&1; then
  error "未找到 docker compose（需要 Docker Compose v2）"
fi

# Python3 + pip3
if ! command -v python3 >/dev/null 2>&1; then
  warn "未找到 Python3，正在安装 ..."
  install_pkg python3
fi
need_cmd python3

if ! command -v pip3 >/dev/null 2>&1; then
  warn "未找到 pip3，正在安装 ..."
  install_pkg python3-pip 2>/dev/null || python3 -m ensurepip --upgrade 2>/dev/null || true
  if ! command -v pip3 >/dev/null 2>&1; then
    warn "pip3 安装失败，将尝试使用 python3 -m pip"
  fi
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

      # Read existing config safely (no source, grep only)
      START_PORT=$(grep '^START_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "")
      API_KEY=$(grep '^API_KEY=' .env 2>/dev/null | cut -d= -f2 || echo "")
      GATEWAY_PORT=$(grep '^GATEWAY_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "9880")
      COOKIE_MANAGER_PASSWORD=$(grep '^COOKIE_MANAGER_PASSWORD=' .env 2>/dev/null | cut -d= -f2 || echo "")
      CONTAINER_PREFIX=$(grep '^CONTAINER_PREFIX=' .env 2>/dev/null | cut -d= -f2 || echo "gemini_api_account_")
      HTTP_PROXY=$(grep '^HTTP_PROXY=' .env 2>/dev/null | cut -d= -f2 || echo "")
      HTTPS_PROXY=$(grep '^HTTPS_PROXY=' .env 2>/dev/null | cut -d= -f2 || echo "")
      GATEWAY_PORT="${GATEWAY_PORT:-9880}"
      CONTAINER_PREFIX="${CONTAINER_PREFIX:-gemini_api_account_}"

      # 确保 GATEWAY_PORT 写入 .env（老用户可能没有）
      if ! grep -q '^GATEWAY_PORT=' .env 2>/dev/null; then
        echo "" >> .env
        echo "# Gateway (智能轮询总入口)" >> .env
        echo "GATEWAY_PORT=${GATEWAY_PORT}" >> .env
        info "已添加 GATEWAY_PORT=${GATEWAY_PORT} 到 .env"
      fi

      # 确保 COOKIE_MANAGER_PASSWORD 写入 .env（老用户可能没有）
      if ! grep -q '^COOKIE_MANAGER_PASSWORD=' .env 2>/dev/null; then
        echo ""
        info "首次启用 Gateway 面板，需要设置登录密码"
        read -rp "  面板密码 [回车自动生成]: " USER_GW_PASSWORD
        if [[ -z "$USER_GW_PASSWORD" ]]; then
          USER_GW_PASSWORD=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)
          info "已生成面板密码: ${USER_GW_PASSWORD}"
        fi
        echo "" >> .env
        echo "# Cookie Manager / Gateway 面板密码" >> .env
        echo "COOKIE_MANAGER_PASSWORD=${USER_GW_PASSWORD}" >> .env
      fi

      # 确保 COOKIE_MANAGER_PORT 写入 .env
      if ! grep -q '^COOKIE_MANAGER_PORT=' .env 2>/dev/null; then
        echo "COOKIE_MANAGER_PORT=${GATEWAY_PORT}" >> .env
      fi

      info "重新生成 compose ..."
      python3 scripts/generate_compose.py

      info "重建并重启容器 ..."
      docker build -t gemini-api-oneclick:local .
      docker compose -f docker-compose.accounts.yml up -d

      # 安装 Gateway 依赖
      if ! python3 -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
        info "安装 Gateway 依赖 ..."
        PIP="pip3"
        command -v pip3 >/dev/null 2>&1 || PIP="python3 -m pip"
        $PIP install -q fastapi uvicorn httpx 2>&1 \
          || $PIP install --break-system-packages -q fastapi uvicorn httpx 2>&1 \
          || $SUDO $PIP install -q fastapi uvicorn httpx 2>&1 \
          || $SUDO $PIP install --break-system-packages -q fastapi uvicorn httpx 2>&1 \
          || { error "Gateway 依赖安装失败，请手动运行: pip3 install fastapi uvicorn httpx"; }
      fi

      # 重启或安装 Gateway 服务
      if command -v systemctl >/dev/null 2>&1; then
        PYTHON_BIN=$(command -v python3)
        SERVICE_FILE="/etc/systemd/system/gemini-gateway.service"

        # 始终写入最新的 service 文件（更新配置路径等）
        $SUDO tee "$SERVICE_FILE" > /dev/null <<GWEOF
[Unit]
Description=Gemini API Gateway — 智能轮询网关
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
ExecStart=${PYTHON_BIN} ${ROOT_DIR}/gateway.py
Restart=on-failure
RestartSec=5
EnvironmentFile=${ROOT_DIR}/.env

[Install]
WantedBy=multi-user.target
GWEOF

        $SUDO systemctl daemon-reload
        $SUDO systemctl enable gemini-gateway
        $SUDO systemctl restart gemini-gateway
        info "Gateway 已安装/更新为系统服务（端口 ${GATEWAY_PORT}）"
      fi

      echo ""
      info "更新完成！Gateway 地址: http://YOUR_IP:${GATEWAY_PORT}"
      exit 0
      ;;
    2)
      # 读取旧配置作为默认值
      OLD_START_PORT=$(grep '^START_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "")
      OLD_API_KEY=$(grep '^API_KEY=' .env 2>/dev/null | cut -d= -f2 || echo "")
      OLD_GATEWAY_PORT=$(grep '^GATEWAY_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "")
      OLD_PASSWORD=$(grep '^COOKIE_MANAGER_PASSWORD=' .env 2>/dev/null | cut -d= -f2 || echo "")

      existing_env_count=$(ls envs/account*.env 2>/dev/null | wc -l | tr -d ' ')
      existing_container_count=$(docker ps --format '{{.Names}}' | grep -c "^gemini_api_account_" 2>/dev/null || echo "0")

      if (( existing_env_count > 0 )); then
        echo ""
        warn "当前已有 ${existing_env_count} 个账号配置、${existing_container_count} 个运行中容器"
        warn "全新安装会覆盖 .env 主配置（端口/密钥/密码），但保留已有的 Cookie 配置"
        [[ -n "$OLD_START_PORT" ]] && echo -e "  当前起始端口: ${BOLD}${OLD_START_PORT}${NC}"
        echo ""
        read -rp "  确认全新安装？[y/N]: " confirm_fresh
        [[ "$confirm_fresh" =~ ^[Yy]$ ]] || { info "已取消"; exit 0; }
      fi

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

# 容器端口分配（优先沿用旧配置）
DEFAULT_START="${OLD_START_PORT:-3001}"
END_PORT=$((DEFAULT_START + ACCOUNT_COUNT - 1))
[[ -n "${OLD_START_PORT:-}" ]] && echo -e "  ${GREEN}沿用已有端口配置${NC}"
echo -e "  端口范围: ${BOLD}${DEFAULT_START}-${END_PORT}${NC}"
echo ""
echo "  [1] 使用端口范围 ${DEFAULT_START}-${END_PORT}"
echo "  [2] 自定义起始端口"
echo ""
read -rp "  选择 [1/2, 默认 1]: " port_choice
port_choice="${port_choice:-1}"

if [[ "$port_choice" == "2" ]]; then
  read -rp "  输入起始端口: " DEFAULT_START
  if ! [[ "$DEFAULT_START" =~ ^[0-9]+$ ]] || (( DEFAULT_START < 1024 || DEFAULT_START > 65000 )); then
    error "端口无效（1024-65000）"
  fi
fi

# 检测端口占用
START_PORT="$DEFAULT_START"
occupied_ports=()
for (( i=0; i<ACCOUNT_COUNT; i++ )); do
  p=$((START_PORT + i))
  if port_in_use "$p"; then
    occupied_ports+=("$p")
  fi
done

if (( ${#occupied_ports[@]} > 0 )); then
  warn "以下端口已被占用: ${occupied_ports[*]}"
  echo ""
  echo "  [1] 跳过已占用端口，自动顺延"
  echo "  [2] 重新输入起始端口"
  echo ""
  read -rp "  选择 [1/2]: " conflict_choice

  if [[ "$conflict_choice" == "2" ]]; then
    read -rp "  输入新的起始端口: " START_PORT
    if ! [[ "$START_PORT" =~ ^[0-9]+$ ]] || (( START_PORT < 1024 || START_PORT > 65000 )); then
      error "端口无效（1024-65000）"
    fi
    # 再检一次
    for (( i=0; i<ACCOUNT_COUNT; i++ )); do
      p=$((START_PORT + i))
      if port_in_use "$p"; then
        error "端口 $p 仍被占用，请释放后重试"
      fi
    done
  else
    # 跳过占用端口，找到足够的连续空闲段
    START_PORT=$(find_free_port_range "$ACCOUNT_COUNT" "$START_PORT") || error "找不到 $ACCOUNT_COUNT 个连续空闲端口"
    info "已顺延到 ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1))"
  fi
else
  info "端口 ${START_PORT}-$((START_PORT + ACCOUNT_COUNT - 1)) 全部可用"
fi
echo ""

# [2/5] API key
step "2/5" "API 密钥（回车自动生成）"
if [[ -n "${OLD_API_KEY:-}" ]]; then
  read -rp "  API_KEY [保留当前: ${OLD_API_KEY:0:8}...]: " USER_API_KEY
  USER_API_KEY="${USER_API_KEY:-$OLD_API_KEY}"
else
  read -rp "  API_KEY [自动]: " USER_API_KEY
  if [[ -z "$USER_API_KEY" ]]; then
    USER_API_KEY=$(random_key)
    info "已生成 API 密钥: $USER_API_KEY"
  fi
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

# [4/5] Gateway 面板密码（已集成 Cookie 管理功能）
step "4/5" "Gateway 面板密码（Cookie 管理已集成到 Gateway）"
COOKIE_MANAGER_PORT="9880"
if [[ -n "${OLD_PASSWORD:-}" ]]; then
  read -rp "  面板密码 [保留当前: ${OLD_PASSWORD:0:6}...]: " COOKIE_MANAGER_PASSWORD
  COOKIE_MANAGER_PASSWORD="${COOKIE_MANAGER_PASSWORD:-$OLD_PASSWORD}"
else
  read -rp "  面板密码 [自动]: " COOKIE_MANAGER_PASSWORD
  if [[ -z "$COOKIE_MANAGER_PASSWORD" ]]; then
    COOKIE_MANAGER_PASSWORD=$(random_key | head -c 16)
    info "已生成面板密码: $COOKIE_MANAGER_PASSWORD"
  fi
fi
echo ""

# [5/5] Gateway port
step "5/5" "Gateway 统一入口端口"
DEFAULT_GW="${OLD_GATEWAY_PORT:-9880}"
read -rp "  端口 [${DEFAULT_GW}]: " USER_GW_PORT
GATEWAY_PORT="${USER_GW_PORT:-$DEFAULT_GW}"

if port_in_use "$GATEWAY_PORT"; then
  warn "端口 ${GATEWAY_PORT} 已被占用！"
  read -rp "  换一个端口: " GATEWAY_PORT
  if [[ -z "$GATEWAY_PORT" ]] || port_in_use "$GATEWAY_PORT"; then
    error "端口不可用"
  fi
fi
info "Gateway 端口: ${GATEWAY_PORT}"
echo ""

# ══════════════════════════════════════════════════════════════
# Execute installation
# ══════════════════════════════════════════════════════════════
TOTAL_STEPS=6  # env + .env + compose + build + gateway + health

CURRENT_STEP=0

# Step: Create env files
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "创建账号配置文件 ..."
mkdir -p envs cookie-cache state

new_count=0
kept_count=0
for (( i=1; i<=ACCOUNT_COUNT; i++ )); do
  env_file="envs/account${i}.env"
  if [[ -f "$env_file" ]]; then
    kept_count=$((kept_count + 1))
  else
    cat > "$env_file" <<EOF
API_KEY=
SECURE_1PSID=
SECURE_1PSIDTS=
EOF
    new_count=$((new_count + 1))
  fi
  mkdir -p "cookie-cache/account${i}"
done

# 统计总数（包含超出 ACCOUNT_COUNT 的已有 env）
total_envs=$(ls envs/account*.env 2>/dev/null | wc -l | tr -d ' ')
if (( total_envs > ACCOUNT_COUNT )); then
  extra=$((total_envs - ACCOUNT_COUNT))
  info "新建 ${new_count} 个，保留 ${kept_count} 个，另有 ${extra} 个已有配置（共 ${total_envs} 个容器）"
elif (( kept_count > 0 )); then
  info "新建 ${new_count} 个，保留 ${kept_count} 个（共 ${total_envs} 个容器）"
else
  info "已创建 ${new_count} 个账号配置"
fi

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

EOF

info ".env 已写入"
CONTAINER_PREFIX="gemini_api_account_"

# Step: Generate compose
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "生成 docker-compose ..."
python3 scripts/generate_compose.py

# Step: Build and start
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "构建并启动容器 ..."

# 清理所有残留的同名容器（扫描全部 env 文件，不仅是 1~N）
for env_file in envs/account*.env; do
  [[ -f "$env_file" ]] || continue
  n="${env_file##*account}"; n="${n%.env}"
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  cname="${CONTAINER_PREFIX}${n}"
  if docker ps -a --format '{{.Names}}' | grep -q "^${cname}$"; then
    docker stop "$cname" 2>/dev/null || true
    docker rm "$cname" 2>/dev/null || true
  fi
done

docker build -t gemini-api-oneclick:local .
docker compose -f docker-compose.accounts.yml up -d

# Step: Gateway (智能轮询网关)
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "部署智能轮询网关 (端口 ${GATEWAY_PORT}) ..."

# 安装 gateway 依赖
if ! python3 -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
  info "安装 Gateway 依赖 ..."
  PIP="pip3"
  command -v pip3 >/dev/null 2>&1 || PIP="python3 -m pip"
  $PIP install -q fastapi uvicorn httpx 2>&1 \
    || $PIP install --break-system-packages -q fastapi uvicorn httpx 2>&1 \
    || $SUDO $PIP install -q fastapi uvicorn httpx 2>&1 \
    || $SUDO $PIP install --break-system-packages -q fastapi uvicorn httpx 2>&1 \
    || { error "Gateway 依赖安装失败，请手动运行: pip3 install fastapi uvicorn httpx"; }
fi

if command -v systemctl >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3)
  SERVICE_FILE="/etc/systemd/system/gemini-gateway.service"

  $SUDO tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Gemini API Gateway — 智能轮询网关
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
ExecStart=${PYTHON_BIN} ${ROOT_DIR}/gateway.py
Restart=on-failure
RestartSec=5
EnvironmentFile=${ROOT_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable gemini-gateway
  $SUDO systemctl restart gemini-gateway
  info "Gateway 已安装为系统服务"
else
  warn "未找到 systemctl，后台启动 Gateway ..."
  GATEWAY_PORT=${GATEWAY_PORT} BASE_PORT=${START_PORT} nohup python3 gateway.py > /tmp/gemini-gateway.log 2>&1 &
  info "Gateway 已启动 (PID: $!)"
fi

# Cookie Manager 独立服务已弃用（功能已合并到 Gateway）
# 如果旧版 cookie-manager 服务还在运行，停止它
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active cookie-manager >/dev/null 2>&1; then
    $SUDO systemctl stop cookie-manager
    $SUDO systemctl disable cookie-manager
    info "已停止旧版 Cookie Manager 独立服务（功能已集成到 Gateway）"
  fi
fi

# Step: Health check
CURRENT_STEP=$((CURRENT_STEP + 1))
step "${CURRENT_STEP}/${TOTAL_STEPS}" "健康检查 ..."
sleep 5
if curl -fsS --max-time 5 "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
  info "Gateway 运行正常"
else
  warn "Gateway 暂未响应，可能还在启动中，请稍后访问面板确认"
fi

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
FINAL_COUNT=$(ls envs/account*.env 2>/dev/null | wc -l | tr -d ' ')
echo -e "  智能轮询 ${FINAL_COUNT} 个容器，自动跳过故障节点"
echo -e "  支持 OpenAI 兼容格式，可直接接入酒馆/Kelivo/NewAPI 等"

echo -e "  ${BOLD}面板密码:${NC}   ${COOKIE_MANAGER_PASSWORD}"
echo ""
echo -e "  ${YELLOW}下一步:${NC} 打开 Gateway 面板 (http://YOUR_IP:${GATEWAY_PORT})，登录后填入 Gemini Cookie！"
echo ""
echo -e "${PINK}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${PINK}║${NC}  ${BOLD}Gemini API OneClick${NC} by WanWan                 ${PINK}║${NC}"
echo -e "${PINK}║${NC}  觉得好用的话，给个 Star 吧！                 ${PINK}║${NC}"
echo -e "${PINK}║${NC}  github.com/shleeshlee/gemini-api-oneclick     ${PINK}║${NC}"
echo -e "${PINK}╚═══════════════════════════════════════════════╝${NC}"
echo ""
