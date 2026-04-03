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
WORKER_MODE="${WORKER_MODE:-}"
WORKER_URL="${WORKER_URL:-http://127.0.0.1:7860}"

is_worker_mode() {
  [[ "$WORKER_MODE" == "true" || "$WORKER_MODE" == "1" || "$WORKER_MODE" == "yes" ]]
}

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
CYAN='\033[0;36m'
PINK='\033[38;5;205m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[*]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; }

find_max_account() {
  local max=0
  for f in envs/account*.env; do
    [[ -f "$f" ]] || continue
    local n="${f##*account}"; n="${n%.env}"
    if [[ "$n" =~ ^[0-9]+$ ]] && (( n > max )); then max=$n; fi
  done
  echo "$max"
}

count_accounts() {
  local count=0
  for f in envs/account*.env; do [[ -f "$f" ]] && count=$((count + 1)); done
  echo "$count"
}

list_account_nums() {
  for f in envs/account*.env; do
    [[ -f "$f" ]] || continue
    local n="${f##*account}"; n="${n%.env}"
    [[ "$n" =~ ^[0-9]+$ ]] && echo "$n"
  done | sort -n
}

# 重启 Gateway
restart_gateway() {
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active gemini-gateway >/dev/null 2>&1; then
    sudo systemctl restart gemini-gateway
    info "Gateway 已重启"
  fi
}

# ══════════════════════════════════════════════════════════════
# [1] 添加容器
# ══════════════════════════════════════════════════════════════
do_add() {
  local current max
  current=$(count_accounts)
  max=$(find_max_account)

  info "当前账号数: ${current}（最大编号: #${max}）"
  echo ""
  read -rp "要添加几个账号？[1]: " add_count
  add_count="${add_count:-1}"

  if ! [[ "$add_count" =~ ^[0-9]+$ ]] || (( add_count < 1 || add_count > 50 )); then
    error "数量无效（1-50）"
    return
  fi

  local start=$((max + 1))
  local end=$((max + add_count))

  if ! is_worker_mode; then
    local has_conflict=false
    for (( i=start; i<=end; i++ )); do
      local port=$(( START_PORT + i - 1 ))
      if port_in_use "$port"; then
        warn "端口 $port（容器 #$i）已被占用！"
        has_conflict=true
      fi
    done
    if $has_conflict; then
      read -rp "检测到端口冲突，继续吗？[y/N]: " force
      [[ "$force" =~ ^[Yy]$ ]] || { info "已取消"; return; }
    fi
  fi

  info "创建 account${start}.env ~ account${end}.env ..."
  mkdir -p envs cookie-cache

  for (( i=start; i<=end; i++ )); do
    local env_file="envs/account${i}.env"
    if [[ -f "$env_file" ]]; then
      warn "$env_file 已存在，跳过"
      continue
    fi
    cat > "$env_file" <<EOF
API_KEY=
SECURE_1PSID=
SECURE_1PSIDTS=
EOF
    mkdir -p "cookie-cache/account${i}"
    echo "  已创建 $env_file"
  done

  if is_worker_mode; then
    info "重启 Worker 以加载新账号 ..."
    docker restart gemini_worker 2>/dev/null || warn "Worker 重启失败"
  else
    info "重新生成 compose ..."
    python3 scripts/generate_compose.py
    info "启动新容器 ..."
    docker compose -f docker-compose.accounts.yml up -d --build --no-recreate
  fi

  restart_gateway

  echo ""
  info "完成！已添加 ${add_count} 个账号"
  info "下一步: 在 Gateway 面板填入 Cookie"
}

# ══════════════════════════════════════════════════════════════
# [2] 删除容器
# ══════════════════════════════════════════════════════════════
do_remove() {
  echo ""
  info "当前账号列表:"
  echo ""
  for n in $(list_account_nums); do
    if is_worker_mode; then
      local status="${RED}离线${NC}"
      if curl -fsS --max-time 2 "${WORKER_URL}/slot/${n}/health" 2>/dev/null | grep -q '"client_ready": true'; then
        status="${GREEN}正常${NC}"
      fi
      echo -e "  #${n}  $status"
    else
      local port=$(( START_PORT + n - 1 ))
      local status="${RED}离线${NC}"
      if curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        status="${GREEN}正常${NC}"
      fi
      echo -e "  #${n}  端口 ${port}  $status"
    fi
  done

  echo ""
  read -rp "要删除哪些账号？（逗号分隔，如 5,6,7）: " nums_input
  [[ -z "$nums_input" ]] && { info "已取消"; return; }

  IFS=',' read -ra nums <<< "$nums_input"

  echo ""
  echo -e "  ${YELLOW}即将删除: ${nums[*]}${NC}"
  echo -e "  对应的配置文件会被移除"
  echo ""
  read -rp "确认删除？输入 yes: " confirm
  [[ "$confirm" == "yes" ]] || { info "已取消"; return; }

  for n in "${nums[@]}"; do
    n=$(echo "$n" | tr -d ' ')
    [[ "$n" =~ ^[0-9]+$ ]] || { warn "跳过无效编号: $n"; continue; }

    local env_file="envs/account${n}.env"

    if ! is_worker_mode; then
      local container="${CONTAINER_PREFIX}${n}"
      if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
        info "停止容器 ${container} ..."
        docker stop "$container" 2>/dev/null || true
        docker rm "$container" 2>/dev/null || true
      fi
    fi

    if [[ -f "$env_file" ]]; then
      rm -f "$env_file"
      info "已删除 $env_file"
    fi

    rm -rf "cookie-cache/account${n}" 2>/dev/null || true
  done

  if is_worker_mode; then
    info "重启 Worker 以移除已删账号 ..."
    docker restart gemini_worker 2>/dev/null || warn "Worker 重启失败"
  else
    info "重新生成 compose ..."
    python3 scripts/generate_compose.py
  fi

  restart_gateway

  echo ""
  info "删除完成"
}

# ══════════════════════════════════════════════════════════════
# [3] 查看状态
# ══════════════════════════════════════════════════════════════
do_status() {
  echo ""
  local total
  total=$(count_accounts)

  if is_worker_mode; then
    echo -e "${PINK}Worker 状态（单容器模式）${NC}"
    echo "──────────────────────────────────────────────"
    local worker_status
    worker_status=$(curl -fsS --max-time 3 "${WORKER_URL}/worker/status" 2>/dev/null || echo "")
    if [[ -n "$worker_status" ]]; then
      local w_total w_available
      w_total=$(echo "$worker_status" | python3 -c "import json,sys; print(json.load(sys.stdin)['total'])" 2>/dev/null || echo "?")
      w_available=$(echo "$worker_status" | python3 -c "import json,sys; print(json.load(sys.stdin)['available'])" 2>/dev/null || echo "?")
      echo -e "  Worker    ${GREEN}运行中${NC}  (${w_available}/${w_total} 可用)"
      local mem
      mem=$(docker stats gemini_worker --no-stream --format '{{.MemUsage}}' 2>/dev/null || echo "?")
      echo -e "  内存占用  ${mem}"
    else
      echo -e "  Worker    ${RED}未运行${NC}"
    fi
  else
    local ok=0 fail=0
    echo -e "${PINK}容器状态${NC}"
    echo "──────────────────────────────────────────────"

    for n in $(list_account_nums); do
      local port=$(( START_PORT + n - 1 ))
      if curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        echo -e "  #${n}  端口 ${port}  ${GREEN}正常${NC}"
        ok=$((ok + 1))
      else
        echo -e "  #${n}  端口 ${port}  ${RED}异常${NC}"
        fail=$((fail + 1))
      fi
    done

    echo ""
    info "总计: ${total} 个账号，${GREEN}${ok} 正常${NC}，${RED}${fail} 异常${NC}"
  fi

  # Gateway 状态
  echo ""
  echo -e "${PINK}Gateway 状态${NC}"
  echo "──────────────────────────────────────────────"
  local gw_port="${GATEWAY_PORT:-9880}"
  if curl -fsS --max-time 2 "http://127.0.0.1:${gw_port}/health" >/dev/null 2>&1; then
    echo -e "  端口 ${gw_port}  ${GREEN}运行中${NC}"
    echo -e "  状态面板: http://YOUR_IP:${gw_port}"
  else
    echo -e "  端口 ${gw_port}  ${RED}未运行${NC}"
  fi
}

# ══════════════════════════════════════════════════════════════
# [4] 完整卸载
# ══════════════════════════════════════════════════════════════
do_uninstall() {
  echo ""
  warn "即将执行:"
  echo "  - 停止所有 Gemini API 服务"
  echo "  - 停止 Gateway 服务"
  echo "  - 删除配置文件"
  echo ""
  echo -e "  ${YELLOW}注意: envs/ 目录会保留（包含你的 Cookie）${NC}"
  echo ""
  read -rp "确认卸载？输入 yes: " confirm
  [[ "$confirm" == "yes" ]] || { info "已取消"; return; }

  # 停止容器/worker
  if is_worker_mode; then
    info "停止 Worker ..."
    docker rm -f gemini_worker 2>/dev/null || true
  elif [[ -f docker-compose.accounts.yml ]]; then
    info "停止容器 ..."
    docker compose -f docker-compose.accounts.yml down || true
  fi

  # 停止 Gateway
  if systemctl is-active --quiet gemini-gateway 2>/dev/null; then
    info "停止 Gateway ..."
    sudo systemctl stop gemini-gateway || true
    sudo systemctl disable gemini-gateway || true
    sudo rm -f /etc/systemd/system/gemini-gateway.service
  fi

  # 停止 Cookie Manager
  if systemctl is-active --quiet cookie-manager 2>/dev/null; then
    info "停止 Cookie Manager ..."
    sudo systemctl stop cookie-manager || true
    sudo systemctl disable cookie-manager || true
    sudo rm -f /etc/systemd/system/cookie-manager.service
  fi

  sudo systemctl daemon-reload 2>/dev/null || true

  # 删除生成的文件
  info "清理文件 ..."
  rm -f docker-compose.accounts.yml .env

  # Docker 镜像
  echo ""
  read -rp "删除 Docker 镜像 ${IMAGE_NAME:-gemini-api-oneclick:local}？[y/N]: " rm_image
  if [[ "$rm_image" =~ ^[Yy]$ ]]; then
    docker rmi "${IMAGE_NAME:-gemini-api-oneclick:local}" 2>/dev/null || true
    info "镜像已删除"
  fi

  rm -rf cookie-cache state

  echo ""
  info "卸载完成"
  info "envs/ 目录已保留"
  info "完全删除请执行: rm -rf $(pwd)"
}

# ══════════════════════════════════════════════════════════════
# 菜单
# ══════════════════════════════════════════════════════════════
echo ""
if is_worker_mode; then
  MODE_LABEL="单容器模式"
else
  MODE_LABEL="多容器模式"
fi
echo -e "${PINK}Gemini API OneClick — 管理菜单${NC} (${MODE_LABEL})"
echo "══════════════════════════════════════════"
echo ""
echo "  [1] 添加账号"
echo "  [2] 删除账号"
echo "  [3] 查看状态"
echo "  [4] 完整卸载"
echo "  [q] 退出"
echo ""
read -rp "选择 [1-4/q]: " choice

case "$choice" in
  1) do_add ;;
  2) do_remove ;;
  3) do_status ;;
  4) do_uninstall ;;
  q|Q) exit 0 ;;
  *) error "无效选项"; exit 1 ;;
esac
