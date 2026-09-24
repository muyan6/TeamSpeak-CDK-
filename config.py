import os
import secrets
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Web 服务配置
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "12345"))

# 管理员口令。
# 安全策略：优先读取环境变量 ADMIN_PASSWORD；未配置时生成一次性随机口令并在控制台打印，
# 避免像过去那样把 admin123456 这种弱默认口令随代码一起分发。首次登录后请立即在后台修改。
_RAW_ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip()
if _RAW_ADMIN_PASSWORD:
    ADMIN_PASSWORD = _RAW_ADMIN_PASSWORD
else:
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    print(
        "[!] 未配置 ADMIN_PASSWORD 环境变量，本次已生成随机管理员口令: "
        f"{ADMIN_PASSWORD}\n"
        "[!] 该口令仅在本次进程生命周期内有效（会写入数据库 system_settings），请立即在后台修改密码。",
        file=sys.stderr,
    )

# TS3 服务器公网IP/域名（用于展示给用户连接，如未设置则默认当前主机IP/域名）
PUBLIC_SERVER_IP = os.getenv("PUBLIC_SERVER_IP", "")

# 是否信任反向代理下发的 X-Forwarded-For / X-Forwarded-Host。
# 默认关闭：只有确认部署在自建可信反代（Nginx/Traefik）之后才应开启，
# 否则客户端可伪造请求头绕过体验卡 IP 防刷与对外地址判定。
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0").strip().lower() in ("1", "true", "yes", "on")

# TS3 数据与 docker-compose 存储根目录
_DEFAULT_DATA_DIR = os.getenv("TS_DATA_DIR", "").strip()
if not _DEFAULT_DATA_DIR:
    if os.name == "nt":
        # Windows 环境本地测试默认放在当前工程下的 ./data/teamspeak
        _DEFAULT_DATA_DIR = str(Path(__file__).parent.resolve() / "data" / "teamspeak")
    else:
        _DEFAULT_DATA_DIR = "/data/teamspeak"

DATA_BASE_DIR = _DEFAULT_DATA_DIR

# SQLite 数据库文件路径
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent.resolve() / "teamspeak_manager.db"))

# 端口基础分段配置 (分段前缀规律分配: ts1 -> base+1)
BASE_VOICE_PORT = int(os.getenv("BASE_VOICE_PORT", "60000"))      # ts1 -> 60001 (UDP)
BASE_FILE_PORT = int(os.getenv("BASE_FILE_PORT", "20000"))        # ts1 -> 20001 (TCP)
BASE_QUERY_PORT = int(os.getenv("BASE_QUERY_PORT", "30000"))      # ts1 -> 30001 (TCP)
BASE_TSDNS_PORT = int(os.getenv("BASE_TSDNS_PORT", "40000"))      # ts1 -> 40001 (TCP)

# 自动防火墙预放行的端口段宽度（会随实际最大已分配端口动态扩展，见 firewall_service）
FIREWALL_PORT_SPAN = int(os.getenv("FIREWALL_PORT_SPAN", "200"))

# Docker 镜像名称
TS_DOCKER_IMAGE = os.getenv("TS_DOCKER_IMAGE", "teamspeak:latest")

# 音乐机器人后台服务配置 (远程 API 平台对接)
# 注意：账号与密码不再提供硬编码默认值，必须通过 .env / 管理后台配置，避免凭据随代码分发。
BOT_PANEL_URL = os.getenv("BOT_PANEL_URL", "").rstrip("/")
BOT_PANEL_USER = os.getenv("BOT_PANEL_USER", "")
BOT_PANEL_PASS = os.getenv("BOT_PANEL_PASS", "")
BOT_TUTORIAL_URL = os.getenv("BOT_TUTORIAL_URL", "").strip()
