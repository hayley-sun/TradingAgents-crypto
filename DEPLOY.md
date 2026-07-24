# TradingAgents-crypto 部署文档

## 系统要求

| 项目     | 要求                  |
| -------- | --------------------- |
| 操作系统 | Ubuntu 22.04 LTS      |
| CPU      | 2核 或以上            |
| 内存     | 4GB 或以上 (推荐 8GB) |
| 存储     | 20GB 或以上           |
| 网络     | 公网IP，开放 80 端口  |

---

## 快速部署

### 方式一：一键自动部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/0x0funky/TradingAgents-crypto.git
cd TradingAgents-crypto

# 2. 编辑 .env 文件，填入 API 密钥
nano .env
# 修改 FINNHUB_API_KEY="你的密钥"

# 3. 执行部署脚本
chmod +x deploy.sh
./deploy.sh
```

## 服务管理

操作 命令
启动 sudo systemctl start crypto-trader
停止 sudo systemctl stop crypto-trader
重启 sudo systemctl restart crypto-trader
状态 sudo systemctl status crypto-trader
开机自启 sudo systemctl enable crypto-trader
查看日志 sudo journalctl -u crypto-trader -f
Nginx 日志 sudo tail -f /var/log/nginx/error.log
Gunicorn 日志 sudo tail -f /var/log/gunicorn/error.log

## 故障排查

问题 1: 访问显示 "Welcome to nginx!"

```
# 移除默认站点并重启
sudo rm -f /etc/nginx/sites-enabled/default
sudo rm -f /etc/nginx/conf.d/default.conf
sudo systemctl restart nginx
```

问题 2: Gunicorn 启动失败

```
# 查看详细错误
sudo systemctl status crypto-trader
sudo journalctl -xeu crypto-trader.service -n 50

# 手动测试
cd ~/workspace/TradingAgents-crypto
source venv/bin/activate
gunicorn -w 1 -b 127.0.0.1:8000 web_app:app
```
