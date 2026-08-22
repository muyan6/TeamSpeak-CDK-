import sqlite3
import secrets
import string
from datetime import datetime
from typing import Optional, List, Dict, Any
from config import DB_PATH

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        # 创建 CDK 表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cdks (
                code TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'unused',  -- 'unused', 'used', 'disabled'
                instance_id INTEGER,
                remark TEXT,
                created_at TEXT NOT NULL,
                used_at TEXT
            )
        ''')
        # 创建实例表
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
                status TEXT NOT NULL DEFAULT 'running', -- 'running', 'stopped', 'error'
                created_at TEXT NOT NULL
            )
        ''')
        conn.commit()

# --- CDK 管理 ---

def generate_random_cdk(prefix: str = "TS-", length: int = 12) -> str:
    chars = string.ascii_uppercase + string.digits
    # 过滤容易混淆的字符如 0, O, 1, I
    clean_chars = [c for c in chars if c not in ('0', 'O', '1', 'I')]
    part1 = "".join(secrets.choice(clean_chars) for _ in range(4))
    part2 = "".join(secrets.choice(clean_chars) for _ in range(4))
    part3 = "".join(secrets.choice(clean_chars) for _ in range(4))
    return f"{prefix}{part1}-{part2}-{part3}"

def create_cdks(count: int = 1, remark: str = "") -> List[str]:
    created = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        for _ in range(count):
            while True:
                code = generate_random_cdk()
                try:
                    cursor.execute(
                        "INSERT INTO cdks (code, status, remark, created_at) VALUES (?, 'unused', ?, ?)",
                        (code, remark, now)
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

def bind_cdk_instance(code: str, instance_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cdks SET status = 'used', instance_id = ?, used_at = ? WHERE code = ?",
            (instance_id, now, code)
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
    status: str = "running"
) -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO instances (
                id, name, container_name, dir_path, 
                voice_port, file_port, query_port, tsdns_port, 
                admin_token, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            instance_id, name, container_name, dir_path,
            voice_port, file_port, query_port, tsdns_port,
            admin_token, status, now
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

def delete_instance(instance_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        conn.commit()
        return cursor.rowcount > 0
