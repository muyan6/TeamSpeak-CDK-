import sqlite3
import secrets
import string
import socket
import ipaddress
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from config import DB_PATH

@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

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
                used_at TEXT NOT NULL               -- 记录时间
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_server_key ON trial_server_records(server_key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_resolved_key ON trial_server_records(resolved_key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trial_server_addr ON trial_server_records(server_address)")

        # 创建系统配置表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
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

        conn.commit()

        conn.commit()

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
    from config import ADMIN_PASSWORD
    pwd = get_setting("admin_password")
    if pwd is None:
        pwd = ADMIN_PASSWORD
        set_setting("admin_password", pwd)
    return pwd

def set_admin_password(new_password: str):
    set_setting("admin_password", new_password.strip())

def get_bot_config() -> Dict[str, str]:
    from config import BOT_PANEL_URL, BOT_PANEL_USER, BOT_PANEL_PASS
    url = get_setting("bot_panel_url", BOT_PANEL_URL) or BOT_PANEL_URL
    user = get_setting("bot_panel_user", BOT_PANEL_USER) or BOT_PANEL_USER
    pwd = get_setting("bot_panel_pass", BOT_PANEL_PASS) or BOT_PANEL_PASS
    return {
        "bot_panel_url": url.rstrip("/"),
        "bot_panel_user": user,
        "bot_panel_pass": pwd
    }

def set_bot_config(url: str, user: str, password: str) -> Dict[str, str]:
    cleaned_url = url.strip().rstrip("/")
    cleaned_user = user.strip()
    cleaned_pass = password.strip()
    set_setting("bot_panel_url", cleaned_url)
    set_setting("bot_panel_user", cleaned_user)
    set_setting("bot_panel_pass", cleaned_pass)
    return {
        "bot_panel_url": cleaned_url,
        "bot_panel_user": cleaned_user,
        "bot_panel_pass": cleaned_pass
    }

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
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cdks WHERE code = ?", (code,))
        conn.commit()
        return cursor.rowcount > 0

def delete_cdks(codes: List[str]) -> int:
    if not codes:
        return 0
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in codes)
        cursor.execute(f"DELETE FROM cdks WHERE code IN ({placeholders})", [c.strip() for c in codes])
        conn.commit()
        return cursor.rowcount

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
    elif duration_months is not None and str(duration_months) != "all":
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

# --- 体验卡与服务器地址指纹检测记录 ---

def normalize_server_target(addr: str, port: Optional[int] = 9987) -> Tuple[str, str, int, Optional[str], Optional[str]]:
    """
    智能解析并规范化目标 TeamSpeak 服务器地址与端口
    返回: (server_key, clean_addr, target_port, resolved_ip, resolved_key)
    例如: ("103.71.69.156:9987", "103.71.69.156", 9987, "103.71.69.156", "103.71.69.156:9987")
    """
    clean_addr = str(addr or "").strip().lower()
    if clean_addr.startswith("http://"):
        clean_addr = clean_addr[7:]
    elif clean_addr.startswith("https://"):
        clean_addr = clean_addr[8:]
    clean_addr = clean_addr.rstrip("/")

    target_port = int(port) if port else 9987
    if ":" in clean_addr:
        parts = clean_addr.split(":", 1)
        clean_addr = parts[0].strip()
        try:
            target_port = int(parts[1].strip())
        except ValueError:
            pass

    server_key = f"{clean_addr}:{target_port}"

    resolved_ip = None
    resolved_key = None
    try:
        try:
            ipaddress.ip_address(clean_addr)
            resolved_ip = clean_addr
        except ValueError:
            # 域名解析真实公网 IP
            resolved_ip = socket.gethostbyname(clean_addr)
        if resolved_ip:
            resolved_key = f"{resolved_ip}:{target_port}"
    except Exception:
        pass

    return server_key, clean_addr, target_port, resolved_ip, resolved_key

def has_server_used_trial(addr: str, port: Optional[int] = 9987) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    检测目标服务器是否已在本地使用过体验卡（同一 IP 不同端口视为独立服务器）
    """
    server_key, clean_addr, target_port, resolved_ip, resolved_key = normalize_server_target(addr, port)
    with get_connection() as conn:
        cursor = conn.cursor()
        # 1. 检查 server_key 匹配
        cursor.execute("SELECT * FROM trial_server_records WHERE server_key = ?", (server_key,))
        row = cursor.fetchone()
        if row:
            return True, dict(row)

        # 2. 如果存在解析后的真实 IP 端口，检查是否匹配
        if resolved_key:
            cursor.execute("""
                SELECT * FROM trial_server_records 
                WHERE server_key = ? OR resolved_key = ?
            """, (resolved_key, resolved_key))
            row = cursor.fetchone()
            if row:
                return True, dict(row)

        return False, None

def record_trial_server(
    addr: str,
    port: int,
    cdk_code: str,
    cdk_type: str = "music_bot",
    target_id: Optional[str] = None,
    raw_input: Optional[str] = None
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
                raw_input, cdk_code, cdk_type, target_id, used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_key) DO UPDATE SET
                cdk_code = excluded.cdk_code,
                used_at = excluded.used_at,
                target_id = excluded.target_id
        """, (
            server_key, clean_addr, target_port, resolved_ip, resolved_key,
            raw_input or f"{addr}:{port}", cdk_code, cdk_type, str(target_id) if target_id else None, now
        ))
        conn.commit()
        record_id = cursor.lastrowid
        cursor.execute("SELECT * FROM trial_server_records WHERE id = ? OR server_key = ?", (record_id, server_key))
        row = cursor.fetchone()
        return dict(row) if row else {}

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

def bind_cdk_instance(code: str, instance_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'used', instance_id = ?, used_at = ? WHERE code = ?",
            (instance_id, now, code)
        )
        conn.commit()

def bind_cdk_bot(code: str, bot_id: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'used', bot_id = ?, used_at = ? WHERE code = ?",
            (bot_id, now, code)
        )
        conn.commit()

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
    status: str = "running"
) -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO instances (
                id, name, container_name, dir_path, 
                voice_port, file_port, query_port, tsdns_port, 
                admin_token, query_password, query_apikey,
                cdk_code, duration_months, expire_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            instance_id, name, container_name, dir_path,
            voice_port, file_port, query_port, tsdns_port,
            admin_token, query_password, query_apikey,
            cdk_code, duration_months, expire_at or "permanent", status, now
        ))
        conn.commit()
    return get_instance_by_id(instance_id)

def get_instance_by_id(instance_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM instances WHERE id = ?", (instance_id,))
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
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE instances SET expire_at = ?, status = 'running' WHERE id = ?", (expire_at, instance_id))
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
    为已有 TeamSpeak 实例续期
    """
    inst = get_instance_by_id(instance_id)
    if not inst:
        return None

    if add_months == 0:
        new_expire = "permanent"
    else:
        now = datetime.now()
        current_exp_str = inst.get("expire_at")
        if current_exp_str and current_exp_str != "permanent":
            try:
                curr_exp = datetime.strptime(current_exp_str, "%Y-%m-%d %H:%M:%S")
                # 如果当前还没过期，在原到期时间上累加；如果已过期，从现在开始计算
                base_time = curr_exp if curr_exp > now else now
                new_expire = (base_time + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                new_expire = (now + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            new_expire = (now + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE instances 
            SET expire_at = ?, status = 'running' 
            WHERE id = ?
        """, (new_expire, instance_id))
        conn.commit()

    return get_instance_by_id(instance_id)

def delete_instance(instance_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        conn.commit()
        return cursor.rowcount > 0

def delete_instances(instance_ids: List[int]) -> int:
    if not instance_ids:
        return 0
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in instance_ids)
        cursor.execute(f"DELETE FROM instances WHERE id IN ({placeholders})", instance_ids)
        conn.commit()
        return cursor.rowcount

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
    status: str = "active"
) -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO bot_instances (
                bot_id, name, server_address, server_port, nickname, 
                default_channel, cdk_code, duration_months, expire_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            bot_id, name, server_address, server_port, nickname,
            default_channel, cdk_code, duration_months, expire_at or "permanent", status, now
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
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_instances SET expire_at = ?, status = 'active' WHERE bot_id = ?", (expire_at, bot_id))
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
    为已有机器人实例续期
    """
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        return None

    if add_months == 0:
        new_expire = "permanent"
    else:
        now = datetime.now()
        current_exp_str = bot.get("expire_at")
        if current_exp_str and current_exp_str != "permanent":
            try:
                curr_exp = datetime.strptime(current_exp_str, "%Y-%m-%d %H:%M:%S")
                # 如果当前还没过期，在原到期时间上累加；如果已过期，从现在开始计算
                base_time = curr_exp if curr_exp > now else now
                new_expire = (base_time + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                new_expire = (now + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            new_expire = (now + timedelta(days=30 * add_months)).strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE bot_instances 
            SET expire_at = ?, status = 'active' 
            WHERE bot_id = ?
        """, (new_expire, bot_id))
        conn.commit()

    return get_bot_instance_by_id(bot_id)

def delete_bot_instance(bot_id: str) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bot_instances WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return cursor.rowcount > 0

def delete_bot_instances(bot_ids: List[str]) -> int:
    if not bot_ids:
        return 0
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in bot_ids)
        cursor.execute(f"DELETE FROM bot_instances WHERE bot_id IN ({placeholders})", bot_ids)
        conn.commit()
        return cursor.rowcount
