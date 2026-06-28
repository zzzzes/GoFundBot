#!/bin/bash
# GoFundBot 容器初始化脚本
# 仅在首次启动时运行一次

set -e

echo "=== GoFundBot 初始化 ==="
echo "开始时间: $(date)"

# 配置时区
echo "${TZ:-Asia/Shanghai}" > /etc/timezone
ln -sf /usr/share/zoneinfo/${TZ:-Asia/Shanghai} /etc/localtime

# 检查是否需要安装依赖
INIT_MARKER="/app/.initialized"

if [ -f "$INIT_MARKER" ]; then
    echo "✅ 已初始化过，跳过依赖安装。"
    echo "如需重新安装依赖，请删除 $INIT_MARKER 后重启容器。"
else
    echo "📦 安装系统依赖..."
    apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        curl ca-certificates gnupg git \
        supervisor tzdata

    # Node.js 22
    echo "📦 安装 Node.js 22..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs

    apt-get clean && rm -rf /var/lib/apt/lists/*

    # Python 依赖
    echo "📦 安装 Python 依赖..."
    cd /app/Backend
    pip3 install --break-system-packages --no-cache-dir -r requirements.txt

    # DataService
    echo "📦 安装 DataService 依赖..."
    cd /app/DataService
    npm install
    npm run build

    # Frontend
    echo "📦 安装 Frontend 依赖并构建..."
    cd /app/Frontend
    npm install
    npm run build

    # 复制前端构建产物
    echo "📦 部署前端到 Flask static..."
    mkdir -p /app/Backend/static
    cp -r /app/Frontend/dist/* /app/Backend/static/

    # 复制 supervisord 配置到系统目录
    echo "📦 复制 supervisord 配置..."
    mkdir -p /etc/supervisor/conf.d
    cp /app/supervisord.conf /etc/supervisor/conf.d/gofundbot.conf

    # 标记初始化完成
    touch "$INIT_MARKER"
    echo "✅ 初始化完成于 $(date)"
fi

# 确保 supervisord 配置存在（每次启动都复制，因为 /etc 不在持久化卷上）
if [ ! -f /etc/supervisor/conf.d/gofundbot.conf ]; then
    mkdir -p /etc/supervisor/conf.d
    cp /app/supervisord.conf /etc/supervisor/conf.d/gofundbot.conf
fi

echo "=== 启动 supervisord ==="
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/gofundbot.conf
