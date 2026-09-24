import json
import sqlite3
import secrets
import string
import socket
import sys
import ipaddress
from urllib.parse import urlsplit
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from config import DB_PATH

import os

@contextmanager
def get_connection():
    db_parent = os.path.dirname(os.path.abspath(DB_PATH))
    if db_parent and not os.path.exists(db_parent):
        os.makedirs(db_parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    try:
        yield conn
    finally:
        conn.close()

# 敏感配置回显掩码：前端看到该值即代表“已配置但未修改”，提交时后端跳过更新
MASKED_SECRET = "******"

# 一律不允许明文回传前端的敏感字段（GET / POST 出参统一走 mask_secrets）
SECRET_FIELD_NAMES = (
    "bot_panel_pass",
    "dns_cf_token",
    "dns_aliyun_sk",
    "dns_tencent_key",
    "web_password",
    "admin_token",
    "query_password",
    "query_apikey",
)


def mask_secrets(payload: Dict[str, Any]) -> Dict[str, Any]:
    """返回脱敏副本：所有敏感字段只要非空就替换为掩码，杜绝密钥进入响应体/DOM/浏览器历史。"""
    safe = dict(payload)
    for key in SECRET_FIELD_NAMES:
        if key in safe and safe[key]:
            safe[key] = MASKED_SECRET
    return safe

# SQLite 单条语句的绑定变量上限为 999，批量 IN 查询按 500 分批，避免 "too many SQL variables"
_SQL_VAR_CHUNK = 500

def _chunked(items, size: int = _SQL_VAR_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]

def _set_settings(cursor, items) -> None:
    """在同一个连接/事务内批量写入系统配置，保证配置整体生效或整体回滚。"""
    for key, value in items:
        cursor.execute(
            "INSERT INTO system_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value))
        )

def _calc_new_expire(current_exp_str: Optional[str], add_months: int, now: Optional[datetime] = None) -> str:
    """统一的续期到期时间计算：永久卡保持 permanent，未过期的在原到期时间上顺延，已过期的从当前时间起算。"""
    if add_months == 0:
        return "permanent"
    now = now or datetime.now()
    if current_exp_str == "permanent":
        return "permanent"
    if current_exp_str:
        try:
            curr_exp = datetime.strptime(current_exp_str, "%Y-%m-%d %H:%M:%S")
            base_time = curr_exp if curr_exp > now else now
            return (base_time + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return (now + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        # 创建 CDK 表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cdks (
                code TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'unused',  -- 'unused', 'used', 'disabled'
                cdk_type TEXT NOT NULL DEFAULT 'teamspeak', -- 'teamspeak', 'music_bot'
                duration_months INTEGER NOT NULL DEFAULT 0, -- 0 为永久, 1 为 1个月, 3 为 3个月...
                is_trial INTEGER NOT NULL DEFAULT 0,    -- 0 为普通卡, 1 为体验卡 (限用一次)
                instance_id INTEGER,
                bot_id TEXT,                            -- 绑定的音乐机器人 ID
                remark TEXT,
                created_at TEXT NOT NULL,
                used_at TEXT
            )
        ''')
        # 创建 TeamSpeak 实例表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS instances (
                id INTEGER PRIMARY KEY,                -- 序号 1, 2, 3...
                name TEXT NOT NULL,                    -- ts1, ts2...
                container_name TEXT NOT NULL,          -- ts-teamspeak-1
                dir_path TEXT NOT NULL,                -- 目录路径
                voice_port INTEGER NOT NULL,
                file_port INTEGER NOT NULL,
                query_port INTEGER NOT NULL,
                tsdns_port INTEGER NOT NULL,
                admin_token TEXT,
                query_password TEXT,                   -- serveradmin 密码
                query_apikey TEXT,                     -- serveradmin apikey
                cdk_code TEXT,                         -- 绑定的初始激活 CDK
                duration_months INTEGER NOT NULL DEFAULT 0, -- 0 为永久, 1 为 1个月, 3 为 3个月...
                expire_at TEXT DEFAULT 'permanent',    -- 'YYYY-MM-DD HH:MM:SS' 或 'permanent'
                status TEXT NOT NULL DEFAULT 'running', -- 'running', 'stopped', 'expired', 'error'
                subdomain TEXT,                        -- 绑定的专属二级域名 (如 play.yourdomain.com)
                domain_record_id TEXT,                 -- DNS 服务商记录 ID (用于自动销毁/删除)
                domain_provider TEXT,                  -- 创建该解析时使用的 DNS 服务商（切服务商后仍能正确删除旧记录）
                created_at TEXT NOT NULL
            )
        ''')
        # 创建音乐机器人实例表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL UNIQUE,           -- 远程音乐机器人平台实例 ID
                name TEXT NOT NULL,
                server_address TEXT NOT NULL,
                server_port INTEGER NOT NULL,
                nickname TEXT NOT NULL,
                default_channel TEXT,
                cdk_code TEXT NOT NULL,
                duration_months INTEGER NOT NULL DEFAULT 1,
                expire_at TEXT,                        -- 'YYYY-MM-DD HH:MM:SS' 或 'permanent'
                status TEXT NOT NULL DEFAULT 'active', -- 'active', 'expired', 'stopped'
                web_username TEXT,                     -- 绑定的 Web 网页点歌用户名
                web_password TEXT,                     -- 绑定的 Web 网页点歌密码
                web_user_id TEXT,                      -- 远程平台用户 ID
                created_at TEXT NOT NULL
            )
        ''')
        
        # 创建体验卡使用记录表（本地防重复白嫖指纹库）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trial_server_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_key TEXT NOT NULL UNIQUE,     -- 规范化唯一键，例如 "103.71.69.156:9987"
                server_address TEXT NOT NULL,       -- 主机地址/域名 (小写)
                server_port INTEGER NOT NULL,       -- 语音端口号
                resolved_ip TEXT,                   -- DNS 解析后的公网真实 IP
                resolved_key TEXT,                  -- DNS 解析后的 "IP:Port"
                raw_input TEXT,                     -- 用户原始输入内容
                cdk_code TEXT NOT NULL,             -- 关联的体验卡 CDK
                cdk_type TEXT NOT NULL,             -- 'music_bot' 或 'teamspeak'
                target_id TEXT,                     -- 绑定的 bot_id 或 instance_id
                client_ip TEXT,                     -- 客户端真实 IP (防白嫖指纹)
                used_at TEXT NOT NULL               -- 记录时间
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_server_key ON trial_server_records(server_key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_resolved_key ON trial_server_records(resolved_key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_server_addr ON trial_server_records(server_address)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_client_ip ON trial_server_records(client_ip)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_voice_port ON instances(voice_port)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_container_name ON instances(container_name)")
        # 端口唯一索引：在数据库层兜底，防止并发开通时实例号/端口被重复分配
        for _col in ("file_port", "query_port", "tsdns_port"):
            try:
                cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_{_col} ON instances({_col})")
            except Exception as _idx_err:
                print(f"[Warning] 创建 {_col} 唯一索引失败（可能存在历史重复端口数据）: {_idx_err}")
        # 查询/回收常用索引
        for _idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_cdks_status_type ON cdks(status, cdk_type)",
            "CREATE INDEX IF NOT EXISTS idx_cdks_trial_duration ON cdks(is_trial, duration_months)",
            "CREATE INDEX IF NOT EXISTS idx_cdks_created_at ON cdks(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_instances_cdk_code ON instances(cdk_code)",
            "CREATE INDEX IF NOT EXISTS idx_instances_status_expire ON instances(status, expire_at)",
            "CREATE INDEX IF NOT EXISTS idx_instances_subdomain ON instances(subdomain)",
            "CREATE INDEX IF NOT EXISTS idx_bots_cdk_code ON bot_instances(cdk_code)",
            "CREATE INDEX IF NOT EXISTS idx_bots_status_expire ON bot_instances(status, expire_at)",
        ):
            try:
                cursor.execute(_idx_sql)
            except Exception as _idx_err:
                print(f"[Warning] 创建索引失败: {_idx_err}")

        # 创建系统配置表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')

        # 创建管理员持久化会话表（避免服务重启导致管理员登录态意外丢失）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_admin_sessions_expire ON admin_sessions(expires_at)")
        
        # 兼容旧表升级：检查并添加列
        cursor.execute("PRAGMA table_info(instances)")
        cols = [col["name"] for col in cursor.fetchall()]
        if "query_password" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN query_password TEXT")
        if "query_apikey" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN query_apikey TEXT")
        if "cdk_code" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN cdk_code TEXT")
        if "duration_months" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN duration_months INTEGER NOT NULL DEFAULT 0")
        if "expire_at" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN expire_at TEXT DEFAULT 'permanent'")
        if "subdomain" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN subdomain TEXT")
        if "domain_record_id" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN domain_record_id TEXT")
        if "domain_provider" not in cols:
            cursor.execute("ALTER TABLE instances ADD COLUMN domain_provider TEXT")

        cursor.execute("PRAGMA table_info(cdks)")
        cdk_cols = [col["name"] for col in cursor.fetchall()]
        if "cdk_type" not in cdk_cols:
            cursor.execute("ALTER TABLE cdks ADD COLUMN cdk_type TEXT NOT NULL DEFAULT 'teamspeak'")
        if "duration_months" not in cdk_cols:
            cursor.execute("ALTER TABLE cdks ADD COLUMN duration_months INTEGER NOT NULL DEFAULT 0")
        if "is_trial" not in cdk_cols:
            cursor.execute("ALTER TABLE cdks ADD COLUMN is_trial INTEGER NOT NULL DEFAULT 0")
        if "bot_id" not in cdk_cols:
            cursor.execute("ALTER TABLE cdks ADD COLUMN bot_id TEXT")

        cursor.execute("PRAGMA table_info(bot_instances)")
        bot_cols = [col["name"] for col in cursor.fetchall()]
        if "web_username" not in bot_cols:
            cursor.execute("ALTER TABLE bot_instances ADD COLUMN web_username TEXT")
        if "web_password" not in bot_cols:
            cursor.execute("ALTER TABLE bot_instances ADD COLUMN web_password TEXT")
        if "web_user_id" not in bot_cols:
            cursor.execute("ALTER TABLE bot_instances ADD COLUMN web_user_id TEXT")

        cursor.execute("PRAGMA table_info(trial_server_records)")
        trial_cols = [col["name"] for col in cursor.fetchall()]
        if "client_ip" not in trial_cols:
            cursor.execute("ALTER TABLE trial_server_records ADD COLUMN client_ip TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_client_ip ON trial_server_records(client_ip)")

        # 进程在外部部署期间异常退出时，释放超过 10 分钟的临时占用。
        stale_claim_before = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "UPDATE cdks SET status = 'unused', used_at = NULL "
            "WHERE status = 'processing' AND used_at < ?",
            (stale_claim_before,)
        )

        conn.commit()

def release_stale_cdk_claims(minutes: int = 10) -> int:
    """释放因进程崩溃/部署中断而长期停留在 processing 状态的 CDK 占用（由后台任务定期调用）。"""
    stale_before = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'unused', used_at = NULL "
            "WHERE status = 'processing' AND used_at < ?",
            (stale_before,)
        )
        conn.commit()
        return cursor.rowcount

# --- 系统配置与密码管理 ---

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO system_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        conn.commit()

def get_admin_password() -> str:
    """
    读取管理员口令，首次调用时完成初始化并持久化。

    优先级：数据库中已保存的口令 > 环境变量 ADMIN_PASSWORD > 新生成的随机口令。
    随机口令只在「首次生成」那一刻打印一次，且立即写入 system_settings，
    因此打印出来的就是真实生效值，后续重启不会变化（不会出现「日志口令与库中口令不一致」）。
    """
    from config import ADMIN_PASSWORD

    pwd = get_setting("admin_password")
    if pwd:
        return pwd

    if ADMIN_PASSWORD:
        set_setting("admin_password", ADMIN_PASSWORD)
        return ADMIN_PASSWORD

    generated = secrets.token_urlsafe(12)
    set_setting("admin_password", generated)
    print(
        "[!] 未配置 ADMIN_PASSWORD 环境变量，已生成随机管理员口令并持久化："
        f"{generated}\n"
        "[!] 该口令已写入数据库 system_settings，重启后保持不变；"
        "请立即登录后台【修改密码】更换为自定义强口令。",
        file=sys.stderr,
    )
    return generated

def set_admin_password(new_password: str):
    set_setting("admin_password", new_password.strip())

# --- 管理员会话持久化 ---

def save_admin_session(token: str, expires_at: float):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO admin_sessions (token, expires_at, created_at) VALUES (?, ?, ?)",
            (token, expires_at, now_str)
        )
        conn.commit()

def delete_admin_session(token: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        conn.commit()

def is_admin_session_valid(token: str) -> bool:
    if not token:
        return False
    now_ts = datetime.now().timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT expires_at FROM admin_sessions WHERE token = ?", (token,))
        row = cursor.fetchone()
        if not row:
            return False
        if row["expires_at"] <= now_ts:
            cursor.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            conn.commit()
            return False
        return True

def prune_admin_sessions() -> int:
    now_ts = datetime.now().timestamp()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now_ts,))
        conn.commit()
        return cursor.rowcount

def delete_all_admin_sessions() -> int:
    """吊销全部管理员会话（修改密码时调用），确保旧 Cookie / 被盗 token 立即失效。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admin_sessions")
        conn.commit()
        return cursor.rowcount

def touch_admin_session(token: str, expires_at: float) -> None:
    """把滑动续期后的过期时间写回持久化表，避免重启后会话有效期回退。"""
    if not token:
        return
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE admin_sessions SET expires_at = ? WHERE token = ?",
            (expires_at, token)
        )
        conn.commit()

def get_bot_config() -> Dict[str, str]:
    from config import BOT_PANEL_URL, BOT_PANEL_USER, BOT_PANEL_PASS, BOT_TUTORIAL_URL
    url = get_setting("bot_panel_url", BOT_PANEL_URL) or BOT_PANEL_URL
    user = get_setting("bot_panel_user", BOT_PANEL_USER) or BOT_PANEL_USER
    pwd = get_setting("bot_panel_pass", BOT_PANEL_PASS) or BOT_PANEL_PASS
    tutorial_url = get_setting("bot_tutorial_url", BOT_TUTORIAL_URL) or BOT_TUTORIAL_URL
    return {
        "bot_panel_url": url.rstrip("/"),
        "bot_panel_user": user,
        "bot_panel_pass": pwd,
        "bot_tutorial_url": tutorial_url.strip()
    }

def set_bot_config(url: str, user: str, password: str, tutorial_url: Optional[str] = None) -> Dict[str, str]:
    from config import BOT_TUTORIAL_URL
    cleaned_url = url.strip().rstrip("/")
    cleaned_user = user.strip()
    cleaned_pass = password.strip()
    # 掩码或空值表示管理员未修改密码，保持数据库中已存的原值不变
    if cleaned_pass == MASKED_SECRET or not cleaned_pass:
        cleaned_pass = get_setting("bot_panel_pass", BOT_PANEL_PASS) or BOT_PANEL_PASS
    if tutorial_url is not None:
        cleaned_tut = tutorial_url.strip() or (get_setting("bot_tutorial_url", BOT_TUTORIAL_URL) or BOT_TUTORIAL_URL)
    else:
        cleaned_tut = get_setting("bot_tutorial_url", BOT_TUTORIAL_URL) or BOT_TUTORIAL_URL
    # 单连接单事务批量写入，避免中途失败留下半套配置
    with get_connection() as conn:
        cursor = conn.cursor()
        _set_settings(cursor, [
            ("bot_panel_url", cleaned_url),
            ("bot_panel_user", cleaned_user),
            ("bot_panel_pass", cleaned_pass),
            ("bot_tutorial_url", cleaned_tut),
        ])
        conn.commit()
    return {
        "bot_panel_url": cleaned_url,
        "bot_panel_user": cleaned_user,
        "bot_panel_pass": cleaned_pass,
        "bot_tutorial_url": cleaned_tut
    }

# --- 音乐机器人用户权限与能力配置 ---

def get_bot_permission_config() -> Dict[str, Any]:
    role = get_setting("bot_user_default_role", "member") or "member"
    caps_raw = get_setting("bot_user_default_capabilities")
    if caps_raw is not None:
        try:
            capabilities = json.loads(caps_raw)
            if not isinstance(capabilities, list):
                capabilities = ["player.control", "player.queue"]
        except Exception:
            capabilities = [c.strip() for c in caps_raw.split(",") if c.strip()]
    else:
        capabilities = ["player.control", "player.queue"]
    
    bot_scope = get_setting("bot_user_bot_scope", "current") or "current"
    notice = get_setting("bot_user_permission_notice", "月卡用户仅有控制功能，年卡用户独享音乐后台")
    return {
        "role": role,
        "capabilities": capabilities,
        "bot_scope": bot_scope,
        "permission_notice": notice
    }

def set_bot_permission_config(
    role: str,
    capabilities: List[str],
    bot_scope: str = "current",
    permission_notice: Optional[str] = None
) -> Dict[str, Any]:
    cleaned_role = role.strip() if role else "member"
    cleaned_caps = [c.strip() for c in capabilities if isinstance(c, str) and c.strip()]
    cleaned_scope = "all" if bot_scope == "all" else "current"
    cleaned_notice = (permission_notice.strip() if permission_notice else "月卡用户仅有控制功能，年卡用户独享音乐后台")

    set_setting("bot_user_default_role", cleaned_role)
    set_setting("bot_user_default_capabilities", json.dumps(cleaned_caps, ensure_ascii=False))
    set_setting("bot_user_bot_scope", cleaned_scope)
    set_setting("bot_user_permission_notice", cleaned_notice)

    return {
        "role": cleaned_role,
        "capabilities": cleaned_caps,
        "bot_scope": cleaned_scope,
        "permission_notice": cleaned_notice
    }

# --- DNS 自动化绑定配置 ---

def get_dns_config() -> Dict[str, Any]:
    return {
        "dns_enabled": get_setting("dns_enabled", "0") == "1",
        "dns_provider": get_setting("dns_provider", "disabled") or "disabled",
        "dns_root_domain": get_setting("dns_root_domain", "") or "",
        "dns_target_host": get_setting("dns_target_host", "") or "",
        "dns_cf_token": get_setting("dns_cf_token", "") or "",
        "dns_cf_zone_id": get_setting("dns_cf_zone_id", "") or "",
        "dns_aliyun_ak": get_setting("dns_aliyun_ak", "") or "",
        "dns_aliyun_sk": get_setting("dns_aliyun_sk", "") or "",
        "dns_tencent_id": get_setting("dns_tencent_id", "") or "",
        "dns_tencent_key": get_setting("dns_tencent_key", "") or ""
    }

def get_dns_config_for_provider(provider: Optional[str]) -> Dict[str, Any]:
    """
    取「指定服务商」的 DNS 配置。

    实例创建时若用的是 Cloudflare，之后管理员把默认服务商切到阿里云，
    销毁实例时若仍按当前默认配置去删记录，会调用错误的 API 并残留 SRV 解析。
    这里把库中该服务商的凭据原样取出，保证删除动作命中正确的服务商。
    """
    cfg = get_dns_config()
    target = (provider or "").strip().lower()
    if target and target in ("cloudflare", "aliyun", "tencent"):
        cfg["dns_provider"] = target
    return cfg


def set_dns_config(data: Dict[str, Any]) -> Dict[str, Any]:
    # 单连接单事务批量写入，避免中途失败留下半套配置
    items = []
    if "dns_enabled" in data:
        items.append(("dns_enabled", "1" if data["dns_enabled"] else "0"))
    if "dns_provider" in data:
        items.append(("dns_provider", str(data["dns_provider"]).strip().lower()))
    if "dns_root_domain" in data:
        items.append(("dns_root_domain", str(data["dns_root_domain"]).strip().lower().rstrip(".")))
    if "dns_target_host" in data:
        items.append(("dns_target_host", str(data["dns_target_host"]).strip()))
    # 掩码 "******" 表示前端未修改该字段，保持数据库中原值不变。
    # 除密钥本身外，AK / ZoneId / SecretId 也纳入保护：
    # 前端加载配置后这些输入框会留空（只在已配置时显示占位提示），若不放行空值就会把已存凭据清空。
    _secret_keys = {
        "dns_cf_token", "dns_cf_zone_id",
        "dns_aliyun_ak", "dns_aliyun_sk",
        "dns_tencent_id", "dns_tencent_key",
    }
    for _key in ("dns_cf_token", "dns_cf_zone_id", "dns_aliyun_ak", "dns_aliyun_sk", "dns_tencent_id", "dns_tencent_key"):
        if _key not in data:
            continue
        _val = str(data[_key]).strip()
        # 密钥字段：掩码或空值表示“保持原值不变”，跳过写入避免误清空已有密钥
        if _key in _secret_keys and (_val == MASKED_SECRET or not _val):
            continue
        items.append((_key, _val))
    if items:
        with get_connection() as conn:
            cursor = conn.cursor()
            _set_settings(cursor, items)
            conn.commit()
    return get_dns_config()

def is_subdomain_available(subdomain_prefix: str) -> Tuple[bool, str, str]:
    from dns_service import validate_subdomain_format, clean_subdomain_prefix
    p = clean_subdomain_prefix(subdomain_prefix)
    valid, err = validate_subdomain_format(p)
    if not valid:
        return False, err, ""
    
    cfg = get_dns_config()
    root_domain = cfg.get("dns_root_domain", "")
    full_subdomain = f"{p}.{root_domain}" if root_domain else p

    with get_connection() as conn:
        cursor = conn.cursor()
        # LIKE 通配符转义，避免用户输入中的 _ / % 越界匹配到其他域名
        like_prefix = p.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cursor.execute(
            "SELECT id, name, status FROM instances "
            "WHERE LOWER(subdomain) = ? OR LOWER(subdomain) LIKE ? ESCAPE '\\'",
            (full_subdomain.lower(), f"{like_prefix}.%")
        )
        row = cursor.fetchone()
        if row:
            return False, f"二级域名 [{full_subdomain}] 已被服务器 ({row['name']}) 占用，请换一个名称", full_subdomain
    return True, "该二级域名可用", full_subdomain

# --- CDK 管理 ---

def generate_random_cdk(prefix: str = "TS-", length: int = 12) -> str:
    chars = string.ascii_uppercase + string.digits
    # 过滤容易混淆的字符如 0, O, 1, I
    clean_chars = [c for c in chars if c not in ('0', 'O', '1', 'I')]
    part1 = "".join(secrets.choice(clean_chars) for _ in range(4))
    part2 = "".join(secrets.choice(clean_chars) for _ in range(4))
    part3 = "".join(secrets.choice(clean_chars) for _ in range(4))
    return f"{prefix}{part1}-{part2}-{part3}"

def create_cdks(
    count: int = 1,
    remark: str = "",
    cdk_type: str = "teamspeak",
    duration_months: int = 0,
    is_trial: int = 0
) -> List[str]:
    if count < 1 or count > 200:
        raise ValueError("生成数量必须在 1 到 200 之间")
    if cdk_type not in ("teamspeak", "music_bot"):
        raise ValueError("不支持的 CDK 类型")
    if duration_months not in (0, 1, 3, 6, 12):
        raise ValueError("CDK 时长必须为 0、1、3、6 或 12 个月")
    if is_trial not in (0, 1):
        raise ValueError("体验卡标记必须为 0 或 1")

    created = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = "BOT-" if cdk_type == "music_bot" else "TS-"
    final_dur = 1 if is_trial else duration_months
    with get_connection() as conn:
        cursor = conn.cursor()
        for _ in range(count):
            while True:
                code = generate_random_cdk(prefix=prefix)
                try:
                    cursor.execute(
                        """INSERT INTO cdks (code, status, cdk_type, duration_months, is_trial, remark, created_at) 
                           VALUES (?, 'unused', ?, ?, ?, ?, ?)""",
                        (code, cdk_type, final_dur, is_trial, remark, now)
                    )
                    created.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
    return created

def get_cdk(code: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM cdks WHERE code = ?", (code.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_all_cdks() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM cdks ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]

def delete_cdk(code: str) -> bool:
    """
    删除 CDK。
    注意：刻意保留 instances/bot_instances 上的 cdk_code 引用，以便 CDK 被误删后
    仍能通过绑定关系自愈恢复（restore_*_cdk 依赖该引用），因此这里不做级联清空。
    """
    clean = (code or "").strip()
    with get_connection() as conn:
        cursor = conn.cursor()
        # 改为「吊销」而非物理删除：
        # 1) 保留审计痕迹，避免卡密在统计中凭空消失；
        # 2) 阻止 restore_*_cdk 自愈逻辑把已删除的卡密重新复活；
        # 3) 前台再次输入该 CDK 时会明确收到「已被系统禁用」而不是「无效」。
        cursor.execute(
            "UPDATE cdks SET status = 'disabled' WHERE code = ? AND status != 'disabled'",
            (clean,)
        )
        conn.commit()
        return cursor.rowcount > 0

def delete_cdks(codes: List[str]) -> int:
    if not codes:
        return 0
    cleaned = [c.strip() for c in codes if c and c.strip()]
    total = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for chunk in _chunked(cleaned):
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"DELETE FROM cdks WHERE code IN ({placeholders})", chunk)
            total += cursor.rowcount
        conn.commit()
    return total

def delete_cdks_by_filter(
    cdk_type: Optional[str] = None,
    duration_months: Optional[int] = None,
    is_trial: Optional[int] = None,
    status: Optional[str] = None
) -> int:
    conditions = []
    params = []
    if cdk_type and cdk_type != "all":
        conditions.append("cdk_type = ?")
        params.append(cdk_type)
    if is_trial is not None and str(is_trial) != "all":
        conditions.append("is_trial = ?")
        params.append(int(is_trial))
    if duration_months is not None and str(duration_months) != "all":
        conditions.append("duration_months = ?")
        params.append(int(duration_months))
    if status and status != "all":
        conditions.append("status = ?")
        params.append(status)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"DELETE FROM cdks{where_clause}"

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return cursor.rowcount

def claim_cdk(code: str, cdk_type: str) -> Optional[Dict[str, Any]]:
    """以数据库条件更新原子占用一个未使用 CDK，防止并发重复兑换。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'processing', used_at = ? "
            "WHERE code = ? AND status = 'unused' AND cdk_type = ?",
            (now, code.strip(), cdk_type)
        )
        if cursor.rowcount != 1:
            return None
        conn.commit()
    return get_cdk(code)

def release_cdk_claim(code: str) -> bool:
    """释放外部部署失败时的临时 CDK 占用。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'unused', used_at = NULL "
            "WHERE code = ? AND status = 'processing'",
            (code.strip(),)
        )
        conn.commit()
        return cursor.rowcount == 1

# --- 体验卡与服务器地址指纹检测记录 ---

def normalize_server_target(addr: str, port: Optional[int] = 9987) -> Tuple[str, str, int, Optional[str], Optional[str]]:
    """
    智能解析并规范化目标 TeamSpeak 服务器地址与端口
    返回: (server_key, clean_addr, target_port, resolved_ip, resolved_key)
    例如: ("103.71.69.156:9987", "103.71.69.156", 9987, "103.71.69.156", "103.71.69.156:9987")
    """
    raw_addr = str(addr or "").strip()
    if not raw_addr:
        raise ValueError("服务器地址不能为空")

    target_port = int(port) if port else 9987
    direct_ip = None
    if "://" not in raw_addr:
        try:
            direct_ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            raw_addr = f"//{raw_addr}"

    if direct_ip is not None:
        clean_addr = str(direct_ip).lower()
    else:
        parsed = urlsplit(raw_addr)
        clean_addr = (parsed.hostname or "").strip().lower().rstrip(".")
        if not clean_addr:
            raise ValueError("服务器地址格式不正确")
        try:
            parsed_port = parsed.port
        except ValueError as e:
            raise ValueError("服务器地址中的端口格式不正确") from e
        if parsed_port is not None:
            target_port = parsed_port
    if target_port < 1 or target_port > 65535:
        raise ValueError("服务器端口必须在 1 到 65535 之间")

    key_addr = f"[{clean_addr}]" if ":" in clean_addr else clean_addr
    server_key = f"{key_addr}:{target_port}"

    resolved_ip = None
    resolved_key = None
    try:
        try:
            ipaddress.ip_address(clean_addr)
            resolved_ip = clean_addr
        except ValueError:
            # 域名解析真实 IP，兼容 IPv4/IPv6
            resolved_ip = socket.getaddrinfo(clean_addr, None, type=socket.SOCK_STREAM)[0][4][0]
        if resolved_ip:
            resolved_addr = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
            resolved_key = f"{resolved_addr}:{target_port}"
    except Exception:
        pass

    return server_key, clean_addr, target_port, resolved_ip, resolved_key

def has_server_used_trial(addr: str, port: Optional[int] = 9987) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    检测目标服务器是否已在本地使用过体验卡（同一 IP 不同端口视为独立服务器）
    """
    server_key, clean_addr, target_port, resolved_ip, resolved_key = normalize_server_target(addr, port)
    stale_pending_before = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        # 1. 检查 server_key 匹配（排除超时的临时预占）
        cursor.execute(
            "SELECT * FROM trial_server_records WHERE server_key = ? "
            "AND (target_id IS NULL OR target_id NOT LIKE 'pending:%' OR used_at >= ?)",
            (server_key, stale_pending_before)
        )
        row = cursor.fetchone()
        if row:
            return True, dict(row)

        # 2. 如果存在解析后的真实 IP 端口，检查是否匹配
        if resolved_key:
            cursor.execute("""
                SELECT * FROM trial_server_records 
                WHERE (server_key = ? OR resolved_key = ?)
                  AND (target_id IS NULL OR target_id NOT LIKE 'pending:%' OR used_at >= ?)
            """, (resolved_key, resolved_key, stale_pending_before))
            row = cursor.fetchone()
            if row:
                return True, dict(row)

        return False, None

def reserve_trial_server(
    addr: str,
    port: int,
    cdk_code: str,
    cdk_type: str,
    raw_input: Optional[str] = None
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """为体验卡目标建立唯一预约，阻止并发请求同时创建多个实例。"""
    server_key, clean_addr, target_port, resolved_ip, resolved_key = normalize_server_target(addr, port)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reservation_id = f"pending:{secrets.token_hex(12)}"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        stale_pending_before = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "DELETE FROM trial_server_records "
            "WHERE target_id LIKE 'pending:%' AND used_at < ?",
            (stale_pending_before,)
        )
        cursor.execute(
            "SELECT * FROM trial_server_records WHERE server_key = ? "
            "OR (? IS NOT NULL AND resolved_key = ?)",
            (server_key, resolved_key, resolved_key)
        )
        existing = cursor.fetchone()
        if existing:
            conn.rollback()
            return False, dict(existing)
        try:
            cursor.execute("""
                INSERT INTO trial_server_records (
                    server_key, server_address, server_port, resolved_ip, resolved_key,
                    raw_input, cdk_code, cdk_type, target_id, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                server_key, clean_addr, target_port, resolved_ip, resolved_key,
                raw_input or f"{addr}:{port}", cdk_code, cdk_type, reservation_id, now
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            cursor.execute("SELECT * FROM trial_server_records WHERE server_key = ?", (server_key,))
            row = cursor.fetchone()
            conn.rollback()
            return False, dict(row) if row else None
    return True, get_trial_record_by_key(server_key)

def release_trial_reservation(addr: str, port: int, cdk_code: str) -> bool:
    server_key, _, _, _, _ = normalize_server_target(addr, port)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM trial_server_records "
            "WHERE server_key = ? AND cdk_code = ? AND target_id LIKE 'pending:%'",
            (server_key, cdk_code)
        )
        conn.commit()
        return cursor.rowcount == 1

def get_trial_record_by_key(server_key: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trial_server_records WHERE server_key = ?", (server_key,))
        row = cursor.fetchone()
        return dict(row) if row else None

def record_trial_server(
    addr: str,
    port: int,
    cdk_code: str,
    cdk_type: str = "music_bot",
    target_id: Optional[str] = None,
    raw_input: Optional[str] = None,
    client_ip: Optional[str] = None
) -> Dict[str, Any]:
    """
    持久化记录已使用体验卡的目标服务器标识与详细指纹
    """
    server_key, clean_addr, target_port, resolved_ip, resolved_key = normalize_server_target(addr, port)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trial_server_records (
                server_key, server_address, server_port, resolved_ip, resolved_key,
                raw_input, cdk_code, cdk_type, target_id, client_ip, used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_key) DO UPDATE SET
                cdk_code = excluded.cdk_code,
                used_at = excluded.used_at,
                target_id = excluded.target_id,
                client_ip = COALESCE(excluded.client_ip, trial_server_records.client_ip)
        """, (
            server_key, clean_addr, target_port, resolved_ip, resolved_key,
            raw_input or f"{addr}:{port}", cdk_code, cdk_type, str(target_id) if target_id else None,
            client_ip.strip() if client_ip else None, now
        ))
        conn.commit()
        # 回读：以 server_key 为准（ON CONFLICT 走 UPDATE 分支时 lastrowid 不可靠）
        cursor.execute(
            "SELECT * FROM trial_server_records WHERE server_key = ? ORDER BY id DESC LIMIT 1",
            (server_key,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {}

def reserve_trial_client_ip(client_ip: str, cdk_code: str, cdk_type: str = "teamspeak") -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    以「客户端 IP」为维度的体验卡原子预占。

    has_ip_used_teamspeak_trial 是「先查后写」，同 IP 并发两发可同时通过；
    这里复用 trial_server_records.server_key 的 UNIQUE 约束做原子占位，
    server_key 形如 "ip:203.0.113.10"，与服务器地址维度互不干扰。
    """
    ip = (client_ip or "").strip()
    if not ip or ip in ("127.0.0.1", "::1", "localhost", "testclient"):
        # 本地/未知来源不做 IP 维度限制，避免开发与反向代理场景误伤
        return True, None

    server_key = f"ip:{ip}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stale_before = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "DELETE FROM trial_server_records "
            "WHERE server_key LIKE 'ip:%' AND target_id LIKE 'pending:%' AND used_at < ?",
            (stale_before,)
        )
        cursor.execute("SELECT * FROM trial_server_records WHERE server_key = ?", (server_key,))
        existing = cursor.fetchone()
        if existing:
            conn.rollback()
            return False, dict(existing)
        try:
            cursor.execute("""
                INSERT INTO trial_server_records (
                    server_key, server_address, server_port, resolved_ip, resolved_key,
                    raw_input, cdk_code, cdk_type, target_id, client_ip, used_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                server_key, ip, ip, f"{ip}:1", ip, cdk_code, cdk_type,
                f"pending:{secrets.token_hex(12)}", ip, now
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, None
        cursor.execute("SELECT * FROM trial_server_records WHERE server_key = ?", (server_key,))
        row = cursor.fetchone()
        return True, (dict(row) if row else None)


def release_trial_client_ip(client_ip: str, cdk_code: str) -> bool:
    """释放 IP 维度的体验预占（仅释放 pending 占位，已落地的正式记录不动）。"""
    ip = (client_ip or "").strip()
    if not ip:
        return False
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM trial_server_records "
            "WHERE server_key = ? AND cdk_code = ? AND target_id LIKE 'pending:%'",
            (f"ip:{ip}", cdk_code)
        )
        conn.commit()
        return cursor.rowcount >= 1


def confirm_trial_client_ip(client_ip: str, cdk_code: str, target_id: Optional[str] = None) -> bool:
    """体验开通成功后，把 IP 维度的 pending 占位转为正式记录。"""
    ip = (client_ip or "").strip()
    if not ip:
        return False
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE trial_server_records SET target_id = ? "
            "WHERE server_key = ? AND cdk_code = ? AND target_id LIKE 'pending:%'",
            (str(target_id) if target_id else None, f"ip:{ip}", cdk_code)
        )
        conn.commit()
        return cursor.rowcount >= 1


def has_ip_used_teamspeak_trial(client_ip: str, days: int = 7) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    检查指定客户端 IP 近期是否已兑换过 TeamSpeak 体验服务器（7天内限1次），防止批量刷取服务器。
    """
    if not client_ip or client_ip in ("127.0.0.1", "::1", "localhost", "testclient"):
        return False, None
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM trial_server_records 
            WHERE cdk_type = 'teamspeak' 
              AND client_ip = ? 
              AND used_at >= ?
            ORDER BY used_at DESC LIMIT 1
        """, (client_ip.strip(), since))
        row = cursor.fetchone()
        if row:
            return True, dict(row)
        return False, None

def get_all_trial_records() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trial_server_records ORDER BY used_at DESC")
        return [dict(row) for row in cursor.fetchall()]

def delete_trial_record(record_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM trial_server_records WHERE id = ?", (record_id,))
        conn.commit()
        return cursor.rowcount > 0

def delete_trial_record_for_target(addr: str, port: int, cdk_code: str, target_id: Optional[str] = None) -> bool:
    server_key, _, _, _, _ = normalize_server_target(addr, port)
    with get_connection() as conn:
        cursor = conn.cursor()
        if target_id is None:
            cursor.execute(
                "DELETE FROM trial_server_records WHERE server_key = ? AND cdk_code = ?",
                (server_key, cdk_code)
            )
        else:
            cursor.execute(
                "DELETE FROM trial_server_records "
                "WHERE server_key = ? AND cdk_code = ? AND target_id = ?",
                (server_key, cdk_code, str(target_id))
            )
        conn.commit()
        return cursor.rowcount > 0

def bind_cdk_instance(code: str, instance_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'used', instance_id = ?, used_at = ? "
            "WHERE code = ? AND status IN ('unused', 'processing') AND cdk_type = 'teamspeak'",
            (instance_id, now, code)
        )
        conn.commit()
        return cursor.rowcount == 1

def bind_cdk_bot(code: str, bot_id: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'used', bot_id = ?, used_at = ? "
            "WHERE code = ? AND status IN ('unused', 'processing') AND cdk_type = 'music_bot'",
            (bot_id, now, code)
        )
        conn.commit()
        return cursor.rowcount == 1

def unbind_cdk_instance(code: str, instance_id: int) -> bool:
    """解绑并回收 CDK（体验卡防重复由 trial_server_records 服务器指纹库负责拦截）。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'unused', instance_id = NULL, used_at = NULL "
            "WHERE code = ? AND instance_id = ? AND status = 'used'",
            (code, instance_id)
        )
        conn.commit()
        return cursor.rowcount == 1

def unbind_cdk_bot(code: str, bot_id: str) -> bool:
    """解绑并回收 CDK（体验卡防重复由 trial_server_records 服务器指纹库负责拦截）。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'unused', bot_id = NULL, used_at = NULL "
            "WHERE code = ? AND bot_id = ? AND status = 'used'",
            (code, bot_id)
        )
        conn.commit()
        return cursor.rowcount == 1

def restore_bot_cdk(cdk_code: str, bot_id: str, duration_months: int = 1, remark: str = "自愈/手动恢复已绑定CDK") -> Optional[Dict[str, Any]]:
    """
    当机器人实例存在但 cdks 表中记录被意外删除时，重新向 cdks 表补全记录
    """
    clean_code = cdk_code.strip().upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO cdks 
            (code, status, cdk_type, duration_months, is_trial, bot_id, remark, created_at, used_at)
            VALUES (?, 'used', 'music_bot', ?, 0, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                status = CASE WHEN cdks.status = 'disabled' THEN 'disabled' ELSE 'used' END,
                bot_id = excluded.bot_id,
                used_at = excluded.used_at
        """, (clean_code, duration_months, bot_id, remark, now_str, now_str))
        conn.commit()
    return get_cdk(clean_code)

def restore_instance_cdk(cdk_code: str, instance_id: int, duration_months: int = 0, remark: str = "自愈/手动恢复已绑定CDK") -> Optional[Dict[str, Any]]:
    """
    当 TeamSpeak 实例存在但 cdks 表中记录被意外删除时，重新向 cdks 表补全记录
    """
    clean_code = cdk_code.strip().upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO cdks 
            (code, status, cdk_type, duration_months, is_trial, instance_id, remark, created_at, used_at)
            VALUES (?, 'used', 'teamspeak', ?, 0, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                status = CASE WHEN cdks.status = 'disabled' THEN 'disabled' ELSE 'used' END,
                instance_id = excluded.instance_id,
                used_at = excluded.used_at
        """, (clean_code, duration_months, instance_id, remark, now_str, now_str))
        conn.commit()
    return get_cdk(clean_code)

# --- 实例管理 ---

def get_next_instance_id() -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(id) FROM instances")
        row = cursor.fetchone()
        max_id = row[0] if row and row[0] is not None else 0
        return max_id + 1

def get_all_used_ports() -> Dict[str, List[int]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT voice_port, file_port, query_port, tsdns_port FROM instances")
        rows = cursor.fetchall()
        voice_ports = [r["voice_port"] for r in rows]
        file_ports = [r["file_port"] for r in rows]
        query_ports = [r["query_port"] for r in rows]
        tsdns_ports = [r["tsdns_port"] for r in rows]
        return {
            "voice": voice_ports,
            "file": file_ports,
            "query": query_ports,
            "tsdns": tsdns_ports,
            "all": voice_ports + file_ports + query_ports + tsdns_ports
        }

def create_instance(
    instance_id: int,
    name: str,
    container_name: str,
    dir_path: str,
    voice_port: int,
    file_port: int,
    query_port: int,
    tsdns_port: int,
    admin_token: str = "",
    query_password: str = "",
    query_apikey: str = "",
    cdk_code: Optional[str] = None,
    duration_months: int = 0,
    expire_at: Optional[str] = None,
    status: str = "running",
    subdomain: Optional[str] = None,
    domain_record_id: Optional[str] = None
) -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO instances (
                id, name, container_name, dir_path, 
                voice_port, file_port, query_port, tsdns_port, 
                admin_token, query_password, query_apikey,
                cdk_code, duration_months, expire_at, status, created_at,
                subdomain, domain_record_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            instance_id, name, container_name, dir_path,
            voice_port, file_port, query_port, tsdns_port,
            admin_token, query_password, query_apikey,
            cdk_code, duration_months, expire_at or "permanent", status, now,
            subdomain, domain_record_id
        ))
        conn.commit()
    return get_instance_by_id(instance_id)

def reserve_instance_slot(
    instance_id: int,
    name: str,
    container_name: str,
    dir_path: str,
    voice_port: int,
    file_port: int,
    query_port: int,
    tsdns_port: int,
    cdk_code: Optional[str] = None,
    duration_months: int = 0,
    expire_at: Optional[str] = None,
    subdomain: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    在分配锁内先落库预占实例编号与四类端口（status='provisioning'）。
    端口/容器名上的 UNIQUE 索引即是并发防线：抢到的请求继续部署，抢不到的返回 None 由调用方换号重试。
    这样可彻底避免两个并发兑换拿到同一 instance_id、以及失败方回滚时误删另一方容器与数据目录。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO instances (
                    id, name, container_name, dir_path,
                    voice_port, file_port, query_port, tsdns_port,
                    admin_token, query_password, query_apikey,
                    cdk_code, duration_months, expire_at, status, created_at,
                    subdomain, domain_record_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, ?, 'provisioning', ?, ?, NULL)
            ''', (
                instance_id, name, container_name, dir_path,
                voice_port, file_port, query_port, tsdns_port,
                cdk_code, duration_months, expire_at or "permanent", now,
                subdomain
            ))
            conn.commit()
    except sqlite3.IntegrityError:
        return None
    except Exception:
        return None
    return get_instance_by_id(instance_id)


def finalize_instance(
    instance_id: int,
    admin_token: str = "",
    query_password: str = "",
    query_apikey: str = "",
    status: str = "running",
    subdomain: Optional[str] = None,
    domain_record_id: Optional[str] = None,
    domain_provider: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """部署成功后把预占的实例补齐凭据并置为可用状态。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE instances SET admin_token = ?, query_password = ?, query_apikey = ?, "
            "status = ?, subdomain = ?, domain_record_id = ?, domain_provider = ? WHERE id = ?",
            (admin_token, query_password, query_apikey, status, subdomain, domain_record_id,
             domain_provider, instance_id)
        )
        conn.commit()
    return get_instance_by_id(instance_id)


def update_instance_credentials_if_empty(
    instance_id: int,
    admin_token: str = "",
    query_password: str = "",
    query_apikey: str = ""
) -> Optional[Dict[str, Any]]:
    """凭据自愈：仅在库中字段为空时写入，避免整组覆盖把已提取到的其它凭据清空。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE instances SET "
            "admin_token = CASE WHEN COALESCE(admin_token,'') = '' THEN ? ELSE admin_token END, "
            "query_password = CASE WHEN COALESCE(query_password,'') = '' THEN ? ELSE query_password END, "
            "query_apikey = CASE WHEN COALESCE(query_apikey,'') = '' THEN ? ELSE query_apikey END "
            "WHERE id = ?",
            (admin_token or "", query_password or "", query_apikey or "", instance_id)
        )
        conn.commit()
    return get_instance_by_id(instance_id)


def update_instance_domain(
    instance_id: int,
    subdomain: Optional[str],
    domain_record_id: Optional[str],
    domain_provider: Optional[str] = None
):
    with get_connection() as conn:
        cursor = conn.cursor()
        if domain_provider is None:
            cursor.execute(
                "UPDATE instances SET subdomain = ?, domain_record_id = ? WHERE id = ?",
                (subdomain, domain_record_id, instance_id)
            )
        else:
            cursor.execute(
                "UPDATE instances SET subdomain = ?, domain_record_id = ?, domain_provider = ? WHERE id = ?",
                (subdomain, domain_record_id, domain_provider, instance_id)
            )
        conn.commit()

def get_instance_by_id(instance_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM instances WHERE id = ?", (instance_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_instance_by_cdk(cdk_code: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM instances WHERE cdk_code = ?", (cdk_code.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_all_instances() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM instances ORDER BY id ASC")
        return [dict(row) for row in cursor.fetchall()]

def update_instance_credentials(instance_id: int, admin_token: str = "", query_password: str = "", query_apikey: str = ""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE instances SET admin_token = ?, query_password = ?, query_apikey = ? WHERE id = ?",
            (admin_token, query_password, query_apikey, instance_id)
        )
        conn.commit()

def update_instance_token(instance_id: int, admin_token: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE instances SET admin_token = ? WHERE id = ?", (admin_token, instance_id))
        conn.commit()

def update_instance_status(instance_id: int, status: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE instances SET status = ? WHERE id = ?", (status, instance_id))
        conn.commit()

def update_instance_expiry(instance_id: int, expire_at: str):
    """仅回滚到期时间，不改动运行状态（避免把 stopped/expired 实例强行复活）。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE instances SET expire_at = ? WHERE id = ?", (expire_at, instance_id))
        conn.commit()

def get_expired_active_instances() -> List[Dict[str, Any]]:
    """
    获取所有已超过有效时间但仍处于 running 状态的 TeamSpeak 服务器实例
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM instances 
            WHERE status = 'running' 
              AND expire_at != 'permanent' 
              AND expire_at < ?
        """, (now_str,))
        return [dict(row) for row in cursor.fetchall()]

def renew_instance(instance_id: int, add_months: int) -> Optional[Dict[str, Any]]:
    """
    为已有 TeamSpeak 实例续期。
    使用 BEGIN IMMEDIATE 在同一事务内读取并写回，避免并发续费互相覆盖丢失更新。
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM instances WHERE id = ?", (instance_id,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return None
        inst = dict(row)

        new_expire = _calc_new_expire(inst.get("expire_at"), add_months)
        # 已停止/过期的实例不因续期被强行复活为 running
        if inst.get("status") in ("stopped", "expired", "error"):
            cursor.execute("UPDATE instances SET expire_at = ? WHERE id = ?", (new_expire, instance_id))
        else:
            cursor.execute("UPDATE instances SET expire_at = ?, status = 'running' WHERE id = ?", (new_expire, instance_id))
        conn.commit()

    return get_instance_by_id(instance_id)

def delete_instance(instance_id: int) -> bool:
    """删除实例并清理关联引用：解绑 CDK、清理体验卡指纹记录，避免悬空引用。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            cursor.execute(
                "UPDATE cdks SET instance_id = NULL WHERE instance_id = ?",
                (instance_id,)
            )
            cursor.execute(
                "DELETE FROM trial_server_records WHERE target_id = ? AND cdk_type = 'teamspeak'",
                (str(instance_id),)
            )
        conn.commit()
        return deleted

def delete_instances(instance_ids: List[int]) -> int:
    if not instance_ids:
        return 0
    total = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for chunk in _chunked(list(instance_ids)):
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"DELETE FROM instances WHERE id IN ({placeholders})", chunk)
            total += cursor.rowcount
        conn.commit()
    return total

# --- 音乐机器人实例管理 ---

def create_bot_instance(
    bot_id: str,
    name: str,
    server_address: str,
    server_port: int,
    nickname: str,
    cdk_code: str,
    duration_months: int = 1,
    expire_at: Optional[str] = None,
    default_channel: Optional[str] = None,
    status: str = "active",
    web_username: Optional[str] = None,
    web_password: Optional[str] = None,
    web_user_id: Optional[str] = None
) -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO bot_instances (
                bot_id, name, server_address, server_port, nickname, 
                default_channel, cdk_code, duration_months, expire_at, status,
                web_username, web_password, web_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            bot_id, name, server_address, server_port, nickname,
            default_channel, cdk_code, duration_months, expire_at or "permanent", status,
            web_username, web_password, str(web_user_id) if web_user_id else None, now
        ))
        conn.commit()
    return get_bot_instance_by_id(bot_id)

def get_bot_instance_by_id(bot_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bot_instances WHERE bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_bot_instance_by_cdk(cdk_code: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bot_instances WHERE cdk_code = ?", (cdk_code.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_all_bot_instances() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bot_instances ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]

def update_bot_instance_status(bot_id: str, status: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_instances SET status = ? WHERE bot_id = ?", (status, bot_id))
        conn.commit()

def update_bot_instance_expiry(bot_id: str, expire_at: str):
    """仅回滚到期时间，不改动运行状态（避免把 stopped/expired 机器人强行复活）。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_instances SET expire_at = ? WHERE bot_id = ?", (expire_at, bot_id))
        conn.commit()

def update_bot_instance_web_user_id(bot_id: str, web_user_id: str):
    """更新机器人实例绑定的远程 Web 用户 ID，便于后续同步无需全量扫描"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_instances SET web_user_id = ? WHERE bot_id = ?", (str(web_user_id), bot_id))
        conn.commit()

def get_expired_active_bots() -> List[Dict[str, Any]]:
    """
    获取所有已超过有效时间但仍处于 active 状态的机器人实例
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM bot_instances 
            WHERE status = 'active' 
              AND expire_at != 'permanent' 
              AND expire_at < ?
        """, (now_str,))
        return [dict(row) for row in cursor.fetchall()]

def renew_bot_instance(bot_id: str, add_months: int) -> Optional[Dict[str, Any]]:
    """
    为已有机器人实例续期。
    使用 BEGIN IMMEDIATE 在同一事务内读取并写回，避免并发续费互相覆盖丢失更新。
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM bot_instances WHERE bot_id = ?", (bot_id,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return None
        bot = dict(row)

        new_expire = _calc_new_expire(bot.get("expire_at"), add_months)
        # 已停止/过期的机器人不因续期被强行复活
        if bot.get("status") in ("stopped", "expired", "error"):
            cursor.execute("UPDATE bot_instances SET expire_at = ? WHERE bot_id = ?", (new_expire, bot_id))
        else:
            cursor.execute("UPDATE bot_instances SET expire_at = ?, status = 'active' WHERE bot_id = ?", (new_expire, bot_id))
        conn.commit()

    return get_bot_instance_by_id(bot_id)

def delete_bot_instance(bot_id: str) -> bool:
    """删除机器人实例并清理关联引用：解绑 CDK、清理体验卡指纹记录，避免悬空引用。"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("DELETE FROM bot_instances WHERE bot_id = ?", (bot_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            cursor.execute(
                "UPDATE cdks SET bot_id = NULL WHERE bot_id = ?",
                (bot_id,)
            )
            cursor.execute(
                "DELETE FROM trial_server_records WHERE target_id = ? AND cdk_type = 'music_bot'",
                (str(bot_id),)
            )
        conn.commit()
        return deleted

def delete_bot_instances(bot_ids: List[str]) -> int:
    if not bot_ids:
        return 0
    total = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for chunk in _chunked(list(bot_ids)):
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"DELETE FROM bot_instances WHERE bot_id IN ({placeholders})", chunk)
            total += cursor.rowcount
        conn.commit()
    return total
