#!/bin/bash
# ============================================================
# TradingAgents-crypto 一键部署脚本
# 适用于 Ubuntu 22.04 LTS
# 使用方法: chmod +x deploy.sh && ./deploy.sh
# ============================================================

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ============================================================
# 1. 配置变量（请根据实际情况修改）
# ============================================================

# 项目路径（根据实际部署位置修改）
PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/workspace/TradingAgents-crypto}"
REPO_URL="${REPO_URL:-https://github.com/0x0funky/TradingAgents-crypto.git}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-300}"

# API 密钥（从环境变量或 .env 文件读取）
if [ -f .env ]; then
    source .env
fi

# FinnHub API Key（必需，请修改）
FINNHUB_API_KEY="${FINNHUB_API_KEY:-你的_FINNHUB_API_KEY}"

# DeepSeek API Key（可选，可在Web界面配置）
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"

# ============================================================
# 2. 检查运行环境
# ============================================================

print_info "检查运行环境..."

if [ "$EUID" -eq 0 ]; then 
    print_error "请不要使用 root 用户运行此脚本，请使用 ubuntu 用户"
    exit 1
fi

if ! grep -q "Ubuntu 22.04" /etc/os-release; then
    print_warn "当前系统不是 Ubuntu 22.04，可能不兼容"
    read -p "是否继续? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# ============================================================
# 3. 更新系统并安装基础依赖
# ============================================================

print_info "更新系统并安装基础依赖..."
sudo apt update
sudo apt upgrade -y
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    nginx \
    build-essential \
    curl \
    net-tools \
    gnutls-bin

# ============================================================
# 4. 克隆项目代码
# ============================================================

print_info "克隆项目代码..."
mkdir -p "$(dirname $PROJECT_DIR)"
if [ -d "$PROJECT_DIR" ]; then
    print_warn "项目目录已存在: $PROJECT_DIR"
    read -p "是否重新克隆? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$PROJECT_DIR"
        git clone "$REPO_URL" "$PROJECT_DIR"
    fi
else
    git clone "$REPO_URL" "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"

# ============================================================
# 5. 创建虚拟环境并安装依赖
# ============================================================

print_info "创建 Python 虚拟环境..."
python3 -m venv venv
source venv/bin/activate

print_info "升级 pip..."
pip install --upgrade pip

print_info "安装项目依赖..."
pip install -r requirements.txt

print_info "安装生产环境依赖..."
pip install gunicorn

# ============================================================
# 6. 创建日志目录
# ============================================================

print_info "创建日志目录..."
sudo mkdir -p /var/log/gunicorn
sudo chown -R $USER:$USER /var/log/gunicorn

# ============================================================
# 7. 配置 Nginx
# ============================================================

print_info "配置 Nginx..."

sudo tee /etc/nginx/sites-available/crypto-trader > /dev/null << 'NGINX_CONFIG'
server {
    listen 80;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    location /static/ {
        alias /home/ubuntu/workspace/TradingAgents-crypto/static/;
        expires 30d;
    }
}
NGINX_CONFIG

# 启用站点配置
sudo ln -sf /etc/nginx/sites-available/crypto-trader /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo rm -f /etc/nginx/conf.d/default.conf

# 测试并重启 Nginx
sudo nginx -t
sudo systemctl restart nginx

# ============================================================
# 8. 配置 Systemd 服务
# ============================================================

print_info "配置 Systemd 服务..."

sudo tee /etc/systemd/system/crypto-trader.service > /dev/null << SYSTEMD_SERVICE
[Unit]
Description=Gunicorn for TradingAgents-crypto
After=network.target nginx.service
Wants=nginx.service

[Service]
Type=notify
User=$USER
Group=$USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/venv/bin"
Environment="FINNHUB_API_KEY=$FINNHUB_API_KEY"
Environment="DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY"
ExecStart=$PROJECT_DIR/venv/bin/gunicorn \\
    -w $GUNICORN_WORKERS \\
    -k sync \\
    --timeout $GUNICORN_TIMEOUT \\
    --bind 127.0.0.1:8000 \\
    --access-logfile /var/log/gunicorn/access.log \\
    --error-logfile /var/log/gunicorn/error.log \\
    web_app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSTEMD_SERVICE

# ============================================================
# 9. 启动服务
# ============================================================

print_info "启动服务..."
sudo systemctl daemon-reload
sudo systemctl enable crypto-trader
sudo systemctl restart crypto-trader

# ============================================================
# 10. 验证部署
# ============================================================

print_info "验证服务状态..."
sleep 3

if sudo systemctl is-active --quiet crypto-trader; then
    print_info "✅ 服务运行正常!"
else
    print_error "❌ 服务启动失败，查看日志:"
    sudo systemctl status crypto-trader --no-pager
    sudo journalctl -xeu crypto-trader.service -n 20 --no-pager
    exit 1
fi

# 显示访问信息
PUBLIC_IP=$(curl -s ifconfig.me || echo "未知")
print_info "=========================================="
print_info "🎉 部署完成！"
print_info "访问地址: http://$PUBLIC_IP"
print_info ""
print_info "服务管理命令:"
print_info "  启动: sudo systemctl start crypto-trader"
print_info "  停止: sudo systemctl stop crypto-trader"
print_info "  重启: sudo systemctl restart crypto-trader"
print_info "  状态: sudo systemctl status crypto-trader"
print_info "  日志: sudo journalctl -u crypto-trader -f"
print_info "=========================================="
print_warn "⚠️  请配置 API 密钥:"
print_warn "  1. 在 Web 界面中配置 FinnHub 和 LLM API Key"
print_warn "  2. 或编辑 /etc/systemd/system/crypto-trader.service 添加 Environment"
print_warn "  3. 修改后执行: sudo systemctl daemon-reload && sudo systemctl restart crypto-trader"