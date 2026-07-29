# === 1. 基础镜像 ===
ARG PYTHON_BASE_IMAGE=python:3.13-bookworm
FROM ${PYTHON_BASE_IMAGE} AS runtime-base

WORKDIR /app

# === 2. 软件源与运行环境 ===
# 使用 HTTPS 镜像，避免构建依赖通过明文 HTTP 下载。
RUN rm -rf /etc/apt/sources.list.d/* /etc/apt/sources.list && \
    echo "deb https://mirrors.cloud.tencent.com/debian/ bookworm main non-free non-free-firmware contrib\n\
deb https://mirrors.cloud.tencent.com/debian-security/ bookworm-security main\n\
deb https://mirrors.cloud.tencent.com/debian/ bookworm-updates main non-free non-free-firmware contrib\n\
deb https://mirrors.cloud.tencent.com/debian/ bookworm-backports main non-free non-free-firmware contrib" > /etc/apt/sources.list

ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    DECKREC_NATIVE_METADATA_DIR=/usr/local/share/sekai-deck-recommend-cpp

# === 3. 系统依赖 ===
RUN apt-get update && apt-get install -y --no-install-recommends --fix-missing \
    git \
    cmake \
    build-essential \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libx11-6 \
    libxcb1 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libfontconfig1 \
    libfreetype6 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# === 4. 时区 ===
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

# === 5. Python 依赖 ===
COPY requirements.txt .
RUN python -m pip install \
    --no-cache-dir \
    --trusted-host mirrors.cloud.tencent.com \
    -r requirements.txt

# ================================ StarMoe原生组卡 ================================ #
# 默认在每次无缓存构建时解析 StarMoe master 的最新提交；也可通过 build arg
# 指定分支、标签或完整 commit。镜像记录实际 commit，并携带 LGPL 原文与对应源码。
ARG DECKREC_NATIVE_REPOSITORY=https://github.com/StarMoe-org/sekai-deck-recommend-cpp.git
ARG DECKREC_NATIVE_REF=master
RUN set -eux; \
    mkdir -p /tmp/deckrec-native/source \
        "${DECKREC_NATIVE_METADATA_DIR}" \
        /usr/local/share/licenses/sekai-deck-recommend-cpp; \
    git -C /tmp/deckrec-native/source init; \
    git -C /tmp/deckrec-native/source remote add origin "${DECKREC_NATIVE_REPOSITORY}"; \
    git -C /tmp/deckrec-native/source fetch --depth 1 origin "${DECKREC_NATIVE_REF}"; \
    git -C /tmp/deckrec-native/source checkout --detach FETCH_HEAD; \
    git -C /tmp/deckrec-native/source submodule update --init --recursive; \
    resolved_commit="$(git -C /tmp/deckrec-native/source rev-parse HEAD)"; \
    short_commit="$(printf '%s' "${resolved_commit}" | cut -c1-7)"; \
    printf '%s\n' "${resolved_commit}" > "${DECKREC_NATIVE_METADATA_DIR}/commit"; \
    printf '%s\n' "${DECKREC_NATIVE_REPOSITORY}" > "${DECKREC_NATIVE_METADATA_DIR}/source-url"; \
    printf '%s\n' "${DECKREC_NATIVE_REF}" > "${DECKREC_NATIVE_METADATA_DIR}/source-ref"; \
    printf 'starmoe-%s\n' "${short_commit}" > "${DECKREC_NATIVE_METADATA_DIR}/build-id"; \
    cp /tmp/deckrec-native/source/LICENSE \
        /usr/local/share/licenses/sekai-deck-recommend-cpp/LICENSE; \
    tar --exclude-vcs --exclude='./build' -czf \
        "${DECKREC_NATIVE_METADATA_DIR}/source.tar.gz" \
        -C /tmp/deckrec-native/source .; \
    python -m pip wheel \
        --no-cache-dir \
        --no-deps \
        --wheel-dir /tmp/deckrec-native/wheels \
        /tmp/deckrec-native/source; \
    wheel_path="$(find /tmp/deckrec-native/wheels -maxdepth 1 -name '*.whl' -print -quit)"; \
    test -n "${wheel_path}"; \
    python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --no-deps \
        --force-reinstall \
        "${wheel_path}"; \
    python -c 'from sekai_deck_recommend_cpp import SekaiDeckRecommend; assert hasattr(SekaiDeckRecommend, "get_world_bloom_support_cards"), "StarMoe原生包缺少WL3能力"'; \
    rm -rf /tmp/deckrec-native

FROM runtime-base AS runtime

# === 6. 浏览器 ===
# 浏览器动态库已在上方明确列出；中文字体由部署者通过 ./fonts 挂载和
# global.yaml 的 font.path 配置，不能混入 Playwright 的通用字体依赖包。
RUN playwright install chromium

# === 7. 项目代码与启动入口 ===
COPY . .
# 最终 COPY 后再处理入口，避免文件权限或换行被构建上下文覆盖。
RUN sed -i 's/\r$//' start.sh src/scripts/*.sh && \
    chmod +x start.sh src/scripts/*.sh

CMD ["bash", "/app/start.sh"]
