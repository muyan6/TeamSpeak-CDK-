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

source venv/bin/activate

# 仅在依赖清单发生变化时重新安装，避免每次启动都联网拉取
REQ_HASH_FILE="venv/.requirements.sha256"
CURRENT_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE")" != "$CURRENT_HASH" ]; then
    echo "[*] 安装/更新依赖..."
    pip install -r requirements.txt
    echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
else
    echo "[*] 依赖无变化，跳过安装"
fi

# 加载 .env（若存在），以便下面读取 TS_DATA_DIR
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

if [ -z "${ADMIN_PASSWORD:-}" ]; then
    echo "[!] 未在 .env 中设置 ADMIN_PASSWORD，服务将生成一次性随机口令并打印到启动日志。"
    echo "[!] 建议复制 .env.example 为 .env 并设置强口令后再启动。"
fi

# 创建配置指定的数据目录并赋予权限，确保 UID 9987 的容器进程可正常读写 SQLite
DATA_DIR="${TS_DATA_DIR:-/data/teamspeak}"
mkdir -p "$DATA_DIR"
chmod -R 777 "$DATA_DIR" 2>/dev/null || true

# 自动放行 Linux 本地防火墙端口（如果已开启 firewalld / ufw）
# 端口段宽度取自 FIREWALL_PORT_SPAN，运行期还会随实际最大实例号动态扩展
SPAN="${FIREWALL_PORT_SPAN:-200}"
VOICE_END=$((60000 + SPAN))
FILE_END=$((20000 + SPAN))
QUERY_END=$((30000 + SPAN))

if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
    echo "[*] 检测到 firewalld，正在放行端口段..."
    firewall-cmd --permanent --add-port=12345/tcp &>/dev/null || true
    firewall-cmd --permanent --add-port=60000-${VOICE_END}/udp &>/dev/null || true
    firewall-cmd --permanent --add-port=20000-${FILE_END}/tcp &>/dev/null || true
    firewall-cmd --permanent --add-port=30000-${QUERY_END}/tcp &>/dev/null || true
    firewall-cmd --reload &>/dev/null || true
elif command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    echo "[*] 检测到 ufw，正在放行端口段..."
    ufw allow 12345/tcp &>/dev/null || true
    ufw allow 60000:${VOICE_END}/udp &>/dev/null || true
    ufw allow 20000:${FILE_END}/tcp &>/dev/null || true
    ufw allow 30000:${QUERY_END}/tcp &>/dev/null || true
fi

echo "[*] 正在启动管理服务，监听端口 ${SERVER_PORT:-12345}..."
"$PYTHON_CMD" app.py
