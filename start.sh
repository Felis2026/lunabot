#!/bin/bash

# 以严格模式运行：任一步骤出错时尽快退出，避免静默失败。
set -euo pipefail

# 记录后台子进程 PID，便于在退出时统一清理。
pids=()

# 退出清理：无论脚本正常退出、收到中断还是某个子进程异常退出，
# 都尽量停止已启动的其他服务，避免残留孤儿进程。
cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    if [ ${#pids[@]} -gt 0 ]; then
        echo "正在停止子进程..."
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
        done
        wait "${pids[@]}" 2>/dev/null || true
    fi
    exit "$exit_code"
}

# 启动一个后台服务并记录 PID。
start_service() {
    local name="$1"
    shift
    echo "正在启动 ${name}..."
    "$@" &
    pids+=("$!")
}

# 统一注册退出清理钩子。
trap cleanup EXIT INT TERM

# 1. 启动依赖的后台服务。
start_service "Autochat 服务" python -m src.services.autochat.serve
start_service "Deck Recommender" python src/services/deck_recommender/serve.py

# 3. 启动 聊天室客户端 (已禁用)
echo "正在启动 Chatroom Client..."
# start_service "Chatroom Client" python -m src.services.chatroom.client

# 2. 启动榜线追踪等常驻后台服务。
start_service "Event Tracker" python -m src.services.event_tracker.main

# === 如果还有其他服务，按上面的格式继续加 ===

# 略等数秒，给后台服务一个启动窗口。
sleep 3

# 启动后先做一次存活检查，提前发现子服务拉起失败。
for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "检测到子服务启动失败，退出"
        exit 1
    fi
done

# 3. 最后启动 NoneBot 主程序。
echo "所有子服务启动完毕，正在启动 NoneBot..."
start_service "NoneBot" nb run

# 任一子进程先退出时，触发统一收尾并退出当前脚本。
set +e
wait -n
wait_status=$?
set -e

echo "检测到有进程退出，准备停止其余进程..."
if [ "$wait_status" -eq 0 ]; then
    exit 1
fi
exit "$wait_status"
