#!/bin/bash

# 本脚本由容器通过 shebang 直接执行，文件换行符必须保持为 LF。
# 以严格模式运行：任一步骤出错时尽快退出，避免静默失败。
set -euo pipefail

# 无论调用者当前位于哪个目录，都以脚本所在的项目根目录解析相对路径。
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_root"

# 记录后台子进程 PID，便于在退出时统一清理。
pids=()
optional_pids=()

# 退出清理：无论脚本正常退出、收到中断还是某个子进程异常退出，
# 都尽量停止已启动的其他服务，避免残留进程。
cleanup() {
    local exit_code=$?
    local -a all_pids=("${pids[@]}" "${optional_pids[@]}")
    trap - EXIT INT TERM
    if [ ${#all_pids[@]} -gt 0 ]; then
        echo "正在停止子进程..."
        for pid in "${all_pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
        done
        wait "${all_pids[@]}" 2>/dev/null || true
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

# ================================ 可选服务守护 ================================ #
# Preview 退出只能令预演不可用，不能触发关键进程的 wait-n 和全局 cleanup。
# 守护子进程会低频重试；正式组卡、Event Tracker 与 NoneBot 不等待其 READY。
supervise_optional_service() {
    local name="$1"
    shift
    local child_pid=""
    # 后台守护是独立 subshell，不能继承主脚本的全局 EXIT cleanup。
    trap - EXIT

    stop_optional_child() {
        if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
            kill "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
        fi
        exit 0
    }
    trap stop_optional_child INT TERM

    while true; do
        echo "正在启动可选服务 ${name}..."
        "$@" &
        child_pid=$!
        if wait "$child_pid"; then
            status=0
        else
            status=$?
        fi
        child_pid=""
        echo "可选服务 ${name} 已退出（status=${status}），5秒后重试"
        sleep 5
    done
}

start_optional_service() {
    local name="$1"
    shift
    supervise_optional_service "$name" "$@" &
    optional_pids+=("$!")
}

# 统一注册退出清理钩子。
trap cleanup EXIT INT TERM

# ================================ 原生组卡包准备 ================================ #
# 标准镜像会把 StarMoe 的实际 commit 写入元数据；仅在没有临时 wheel 覆盖时
# 读取该构建身份。这样 emergency override 仍必须显式提供自己的 build id。
load_builtin_deckrec_metadata() {
    local metadata_dir="${DECKREC_NATIVE_METADATA_DIR:-/usr/local/share/sekai-deck-recommend-cpp}"
    local build_id_file="${metadata_dir}/build-id"
    local commit_file="${metadata_dir}/commit"
    local builtin_build_id=""
    local builtin_commit=""

    if [ -n "${DECKREC_NATIVE_OVERRIDE_WHEEL:-}" ] \
        || [ -n "${DECKREC_NATIVE_BUILD_ID:-}" ] \
        || [ ! -f "$build_id_file" ]; then
        return 0
    fi

    IFS= read -r builtin_build_id < "$build_id_file"
    if [[ ! "$builtin_build_id" =~ ^starmoe-[0-9a-f]{7,40}$ ]]; then
        echo "错误：镜像内置原生组卡 build id 格式无效：${builtin_build_id}" >&2
        return 1
    fi
    export DECKREC_NATIVE_BUILD_ID="$builtin_build_id"

    if [ -f "$commit_file" ]; then
        IFS= read -r builtin_commit < "$commit_file"
    fi
    echo "使用镜像内置原生组卡包（${builtin_build_id}${builtin_commit:+, commit=${builtin_commit}}）"
}

load_builtin_deckrec_metadata
unset -f load_builtin_deckrec_metadata

# 临时 wheel 覆盖在所有 Python 进程启动前完成；普通重启会跳过重复安装，
# Compose recreate 则从宿主机挂载的 data 目录离线恢复，无需重建镜像。
source src/scripts/prepare_deckrec_native_override.sh

# ================================ ImgTool本地二进制 ================================ #
# 仅在二进制缺失或源码更新时现场编译；编译失败不阻断 Bot，其余图片功能和
# shrink 的 Python 兜底仍可使用，日志会明确提示 cutout 性能组件不可用。
imgtool_source="src/scripts/imgtool.cpp"
imgtool_compile_script="src/scripts/compile_imgtool_cpp.sh"
imgtool_binary="data/imgtool/imgtool-cpp"
if [ ! -x "$imgtool_binary" ] || [ "$imgtool_source" -nt "$imgtool_binary" ] || [ "$imgtool_compile_script" -nt "$imgtool_binary" ]; then
    echo "正在编译 ImgTool 本地处理组件..."
    if ! bash "$imgtool_compile_script"; then
        echo "警告：ImgTool 本地处理组件编译失败，将使用可用的 Python 兜底功能"
    fi
fi

# 1. 启动依赖的后台服务。
start_service "Autochat 服务" python -m src.services.autochat.serve
start_service "Deck Recommender" python src/services/deck_recommender/serve.py

# 默认在现有 Bot 容器/主机内启动隔离的 preview 进程；它使用独立端口和数据目录，
# 但不要求额外 Docker 容器。明确设置 EXTERNAL=1 时交给外部进程托管。
if [ "${DECKREC_PREVIEW_EXTERNAL:-0}" != "1" ] && [ "${DECKREC_PREVIEW_LOCAL_ENABLED:-0}" = "1" ]; then
    start_optional_service "Deck Recommender Preview" env \
        DECKREC_HOST="${DECKREC_PREVIEW_HOST:-127.0.0.1}" \
        DECKREC_PORT="${DECKREC_PREVIEW_PORT:-45557}" \
        DECKREC_WORKER_NUM="${DECKREC_PREVIEW_WORKER_NUM:-1}" \
        DECKREC_DATA_DIR="${DECKREC_PREVIEW_DATA_DIR:-data/sekai/deckrec-preview}" \
        DECKREC_INSTANCE_ROLE=preview \
        DECKREC_DATA_SCOPE=cn_jp_preview_v1 \
        DECKREC_NATIVE_REGION=cn \
        DECKREC_ATOMIC_DATA_PUBLISH=1 \
        python src/services/deck_recommender/serve.py
fi

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

# 只等待关键进程；optional_pids 中的 preview 守护退出不会触发全局收尾。
set +e
wait -n "${pids[@]}"
wait_status=$?
set -e

echo "检测到有进程退出，准备停止其余进程..."
if [ "$wait_status" -eq 0 ]; then
    exit 1
fi
exit "$wait_status"
