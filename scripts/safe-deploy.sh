#!/usr/bin/env bash
# safe-deploy.sh — Rolling restart in batches to avoid triggering
# datacenter DDoS protection when many containers reconnect at once.
set -euo pipefail

COMPOSE_FILE="docker-compose.accounts.yml"
BATCH_SIZE="${BATCH_SIZE:-6}"
BATCH_DELAY="${BATCH_DELAY:-120}"  # 秒

cd "$(dirname "$0")/.."

# 解析参数
# 默认 --build：lib/ 改动在镜像内，不 build 就吃不到
BUILD=true
for arg in "$@"; do
    case "$arg" in
        --build) BUILD=true ;;
        --no-build) BUILD=false ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

# 获取所有 gemini 服务名（按编号排序）
services=($(docker compose -f "$COMPOSE_FILE" config --services | sort -t- -k3 -n))
total=${#services[@]}

if [ "$total" -eq 0 ]; then
    echo "No services found in $COMPOSE_FILE"
    exit 1
fi

echo "=== Safe Deploy ==="
echo "Total services: $total"
echo "Batch size: $BATCH_SIZE"
echo "Batch delay: ${BATCH_DELAY}s"
echo ""

# 如果需要 build，先统一 build（不启动）
if [ "$BUILD" = true ]; then
    echo ">>> Building image..."
    docker compose -f "$COMPOSE_FILE" build
    echo ""
fi

# 分批启动
batch_num=0
for ((i=0; i<total; i+=BATCH_SIZE)); do
    batch=("${services[@]:i:BATCH_SIZE}")
    batch_num=$((batch_num + 1))
    batch_end=$((i + ${#batch[@]}))

    echo ">>> Batch $batch_num: ${batch[*]} ($((i+1))-${batch_end}/${total})"
    docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "${batch[@]}"

    # 最后一批不等待
    if [ $batch_end -lt $total ]; then
        echo "    Waiting ${BATCH_DELAY}s before next batch..."
        sleep "$BATCH_DELAY"
    fi
done

echo ""
echo "=== Done. Waiting 60s for health checks... ==="
sleep 60

healthy=$(docker ps --filter "name=gemini_api_account" --filter "health=healthy" --format '{{.Names}}' | wc -l)
total_running=$(docker ps --filter "name=gemini_api_account" --format '{{.Names}}' | wc -l)
echo "Result: ${healthy}/${total_running} healthy"
