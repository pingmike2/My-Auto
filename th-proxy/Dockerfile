# Token Harbor Web Session Proxy — amd64 only
# undetected-chromedriver 需要 x86_64
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# 安装 chromium + xvfb + 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    xvfb \
    xauth \
    fonts-liberation \
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
    libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 python 依赖
RUN pip install --no-cache-dir \
    undetected-chromedriver \
    selenium \
    setuptools

# 拷贝服务
COPY th_proxy.py /app/th_proxy.py

# Xvfb 虚拟显示 + 启动服务
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x900x24 & sleep 2 && DISPLAY=:99 python3 /app/th_proxy.py"]
