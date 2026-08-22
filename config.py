import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Web 服务配置
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "12345"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")

# TS3 服务器公网IP/域名（用于展示给用户连接，如未设置则默认当前主机IP/域名）
PUBLIC_SERVER_IP = os.getenv("PUBLIC_SERVER_IP", "")

# TS3 数据与 docker-compose 存储根目录
# 默认按用户要求设置为 /data/teamspeak，若在 Windows 环境下测试可由环境变量覆盖或自适应
_DEFAULT_DATA_DIR = "/data/teamspeak"
if os.name == "nt" and not os.path.exists("C:\\data\\teamspeak"):
    # Windows 环境本地测试默认放在当前工程下的 ./data/teamspeak
    _DEFAULT_DATA_DIR = str(Path(__file__).parent.resolve() / "data" / "teamspeak")

DATA_BASE_DIR = os.getenv("TS_DATA_DIR", _DEFAULT_DATA_DIR)

# SQLite 数据库文件路径
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent.resolve() / "teamspeak_manager.db"))

# 端口基础偏移配置 (官方默认被占用的端口，分配时基础偏移从 +1 开始，即 ts1 -> base+1)
BASE_VOICE_PORT = int(os.getenv("BASE_VOICE_PORT", "9987"))      # ts1 -> 9988
BASE_FILE_PORT = int(os.getenv("BASE_FILE_PORT", "30033"))        # ts1 -> 30034
BASE_QUERY_PORT = int(os.getenv("BASE_QUERY_PORT", "10011"))      # ts1 -> 10012
BASE_TSDNS_PORT = int(os.getenv("BASE_TSDNS_PORT", "41144"))      # ts1 -> 41145

# Docker 镜像名称
TS_DOCKER_IMAGE = os.getenv("TS_DOCKER_IMAGE", "teamspeak:latest")
