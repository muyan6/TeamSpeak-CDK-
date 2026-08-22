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

echo "[*] 正在启动管理服务，监听端口 12345..."
python app.py
