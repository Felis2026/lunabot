# === 1. 基础镜像 ===
FROM docker.m.daocloud.io/library/python:3.13-bookworm

WORKDIR /app

# === 2. 软件源：腾讯云 (HTTP) ===
RUN rm -rf /etc/apt/sources.list.d/* /etc/apt/sources.list && \
    echo "deb http://mirrors.cloud.tencent.com/debian/ bookworm main non-free non-free-firmware contrib\n\
deb http://mirrors.cloud.tencent.com/debian-security/ bookworm-security main\n\
deb http://mirrors.cloud.tencent.com/debian/ bookworm-updates main non-free non-free-firmware contrib\n\
deb http://mirrors.cloud.tencent.com/debian/ bookworm-backports main non-free non-free-firmware contrib" > /etc/apt/sources.list

# === 3. 环境变量 ===
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
ENV PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

# === 4. 安装系统依赖 ===
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
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# === 5. 时区 ===
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

# === 6. 脚本 ===
COPY start.sh .
RUN chmod +x start.sh

# === 7. Python 库 ===
COPY requirements.txt .
# 先安装所有依赖 (此时可能会装上最新版 playwright)
RUN pip install --no-cache-dir -r requirements.txt --trusted-host mirrors.cloud.tencent.com

# [关键修复] 强制把 playwright 降级到 1.48.0，以防镜像站找不到最新版导致安装失败
RUN pip install playwright==1.48.0 --trusted-host mirrors.cloud.tencent.com

# === 8. 浏览器 ===
# 这次会下载 1.48.0 对应的旧版浏览器，肯定能下成功
RUN playwright install chromium
RUN playwright install-deps

# === 9. 代码 ===
COPY . .

CMD ["./start.sh"]