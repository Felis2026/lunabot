#!/bin/bash

# ================================ 原生组卡包临时覆盖 ================================ #
# 本脚本由 start.sh source，安装完成后导出的构建身份和 WL 能力会传给所有子进程。
# wheel 必须来自宿主机持久目录；仅 pip 安装到当前容器无法跨 Compose recreate 保留。

deckrec_native_supports_wl3() {
    python -c \
        'from sekai_deck_recommend_cpp import SekaiDeckRecommend; raise SystemExit(0 if hasattr(SekaiDeckRecommend, "get_world_bloom_support_cards") else 1)' \
        >/dev/null 2>&1
}

prepare_deckrec_native_override() {
    local override_wheel="${DECKREC_NATIVE_OVERRIDE_WHEEL:-}"
    local expected_sha256="${DECKREC_NATIVE_OVERRIDE_SHA256:-}"
    local build_id="${DECKREC_NATIVE_BUILD_ID:-upstream}"
    local actual_sha256=""
    local install_stamp=""

    if [ -z "$override_wheel" ]; then
        # 未启用临时覆盖时保持原版行为；未来镜像若原生带有该能力，也可自动开放 WL3。
        export DECKREC_NATIVE_BUILD_ID="$build_id"
        if deckrec_native_supports_wl3; then
            export DECKREC_WL_SIMULATION_MAX_TURN=3
        else
            export DECKREC_WL_SIMULATION_MAX_TURN=2
        fi
        return 0
    fi

    if [ ! -f "$override_wheel" ]; then
        echo "错误：原生组卡覆盖 wheel 不存在：${override_wheel}" >&2
        return 1
    fi
    if [[ ! "$expected_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
        echo "错误：DECKREC_NATIVE_OVERRIDE_SHA256 必须是 64 位 SHA-256" >&2
        return 1
    fi
    if [[ ! "$build_id" =~ ^[0-9A-Za-z._-]+$ ]] || [ "$build_id" = "upstream" ]; then
        echo "错误：临时原生组卡覆盖必须配置独立的 DECKREC_NATIVE_BUILD_ID" >&2
        return 1
    fi

    expected_sha256="${expected_sha256,,}"
    actual_sha256="$(sha256sum "$override_wheel")"
    actual_sha256="${actual_sha256%% *}"
    if [ "$actual_sha256" != "$expected_sha256" ]; then
        echo "错误：原生组卡覆盖 wheel 的 SHA-256 不匹配" >&2
        echo "期望：${expected_sha256}" >&2
        echo "实际：${actual_sha256}" >&2
        return 1
    fi

    # /tmp 随普通 restart 保留、随容器 recreate 消失，正好区分是否需要重新安装。
    install_stamp="/tmp/sekai-deckrec-${build_id}-${expected_sha256}.ready"
    if [ ! -f "$install_stamp" ] || ! deckrec_native_supports_wl3; then
        echo "正在离线加载临时原生组卡包（${build_id}）..."
        python -m pip install \
            --disable-pip-version-check \
            --no-cache-dir \
            --no-deps \
            --no-index \
            --root-user-action=ignore \
            --force-reinstall \
            "$override_wheel"
        if ! deckrec_native_supports_wl3; then
            echo "错误：临时原生组卡包安装后仍未提供 WL3 能力" >&2
            return 1
        fi
        touch "$install_stamp"
    else
        echo "临时原生组卡包已加载，跳过重复安装（${build_id}）"
    fi

    export DECKREC_NATIVE_BUILD_ID="$build_id"
    export DECKREC_WL_SIMULATION_MAX_TURN=3
}

prepare_deckrec_native_override
unset -f prepare_deckrec_native_override deckrec_native_supports_wl3
