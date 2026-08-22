#!/usr/bin/env bash
set -e

echo "=== TeamSpeak CDK 自动开通管理系统启动脚本 ==="

# 检查 Python 环境
if command -v python3 &>/dev/null; then
    PYTHON_CMD=python3
elif command -v python &>/dev/null; then
    PYTHON_CMD=python
else
    echo "[错误] 未检测到 Python，请先安装 Python 3.8+"
    exit 1
fi

# 创建虚拟环境（可选）
if [ ! -d "venv" ]; then
    echo "[*] 创建虚拟环境 venv..."
    $PYTHON_CMD -m venv venv
fi

echo "[*] 激活虚拟环境并安装依赖..."
source venv/bin/activate
pip install -r requirements.txt

# 创建数据目录
mkdir -p /data/teamspeak

# 自动放行 Linux 本地防火墙端口（如果已开启 firewalld / ufw）
if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
    echo "[*] 检测到 firewalld，正在放行端口段..."
    firewall-cmd --permanent --add-port=12345/tcp &>/dev/null || true
    firewall-cmd --permanent --add-port=60000-60100/udp &>/dev/null || true
    firewall-cmd --permanent --add-port=20000-20100/tcp &>/dev/null || true
    firewall-cmd --permanent --add-port=30000-30100/tcp &>/dev/null || true
    firewall-cmd --reload &>/dev/null || true
elif command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    echo "[*] 检测到 ufw，正在放行端口段..."
    ufw allow 12345/tcp &>/dev/null || true
    ufw allow 60000:60100/udp &>/dev/null || true
    ufw allow 20000:20100/tcp &>/dev/null || true
    ufw allow 30000:30100/tcp &>/dev/null || true
fi

echo "[*] 正在启动管理服务，监听端口 12345..."
python app.py
