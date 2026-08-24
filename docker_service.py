import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from config import DATA_BASE_DIR, TS_DOCKER_IMAGE

def get_compose_cmd() -> list:
    """
    检查系统支持的 docker compose 命令（优先使用 docker compose，其次 docker-compose）
    """
    try:
        res = subprocess.run(["docker", "compose", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if res.returncode == 0:
            return ["docker", "compose"]
    except Exception:
        pass

    try:
        res = subprocess.run(["docker-compose", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if res.returncode == 0:
            return ["docker-compose"]
    except Exception:
        pass

    # 默认返回 docker compose
    return ["docker", "compose"]

def get_instance_dir(instance_id: int) -> str:
    return os.path.join(DATA_BASE_DIR, f"ts{instance_id}")

def generate_compose_yaml_content(instance_id: int, ports: Dict[str, int]) -> str:
    """
    根据用户需求生成 docker-compose.yml 内容
    """
    content = f"""services:
  teamspeak{instance_id}:
    image: {TS_DOCKER_IMAGE}
    container_name: ts-teamspeak-{instance_id}
    restart: always
    environment:
      - TS3SERVER_LICENSE=accept
    ports:
      - "{ports['voice']}:9987/udp"    # 语音服务 (已避开 9987)
      - "{ports['file']}:30033"      # 文件传输 (已避开 30033)
      - "{ports['query']}:10011"      # 服务器查询 raw (已避开 10011)
      - "{ports['tsdns']}:41144"      # DNS域名解析（可选，已避开 41144）
    volumes:
      - ./data:/var/ts3server
"""
    return content

def extract_credentials_from_logs(logs_text: str) -> Dict[str, str]:
    """
    从 TeamSpeak 首次启动日志中提取管理员密钥 Token 与 ServerQuery 账号密码
    """
    creds = {
        "admin_token": "",
        "query_user": "serveradmin",
        "query_password": "",
        "query_apikey": ""
    }
    
    # 1. 提取客户端管理员 Token (Privilege Key)
    token_match = re.search(r'token=([a-zA-Z0-9+/=_-]+)', logs_text)
    if token_match:
        creds["admin_token"] = token_match.group(1).strip()
    else:
        token_match2 = re.search(r'privilege key created.*?token=([^\s\r\n]+)', logs_text, re.IGNORECASE | re.DOTALL)
        if token_match2:
            creds["admin_token"] = token_match2.group(1).strip()

    # 2. 提取 ServerQuery 密码 (password= "xxx" 或 password=xxx)
    pwd_match = re.search(r'password=\s*"([^"]+)"', logs_text)
    if pwd_match:
        creds["query_password"] = pwd_match.group(1).strip()
    else:
        pwd_match2 = re.search(r'password=\s*([^\s,]+)', logs_text)
        if pwd_match2:
            creds["query_password"] = pwd_match2.group(1).strip().strip('"')

    # 3. 提取 ServerQuery apikey (apikey= "xxx" 或 apikey=xxx)
    api_match = re.search(r'apikey=\s*"([^"]+)"', logs_text)
    if api_match:
        creds["query_apikey"] = api_match.group(1).strip()
    else:
        api_match2 = re.search(r'apikey=\s*([^\s,]+)', logs_text)
        if api_match2:
            creds["query_apikey"] = api_match2.group(1).strip().strip('"')

    return creds

# 保持对旧接口的兼容
def extract_admin_token_from_logs(logs_text: str) -> Optional[str]:
    return extract_credentials_from_logs(logs_text).get("admin_token") or None

def deploy_teamspeak_instance(instance_id: int, ports: Dict[str, int]) -> Tuple[bool, Dict[str, str], str]:
    """
    全流程部署 TS 实例：
    1. 创建文件夹 /data/teamspeak/ts{N}
    2. 生成 docker-compose.yml
    3. 执行 docker compose up -d
    4. 尝试获取管理员 Token 与 ServerQuery 账号密码
    返回: (success: bool, creds: Dict[str, str], message: str)
    """
    instance_dir = get_instance_dir(instance_id)
    try:
        os.makedirs(instance_dir, exist_ok=True)
        # 创建 ./data 目录用于挂载卷
        os.makedirs(os.path.join(instance_dir, "data"), exist_ok=True)
    except Exception as e:
        return False, {}, f"创建目录失败: {str(e)}"

    compose_file_path = os.path.join(instance_dir, "docker-compose.yml")
    compose_content = generate_compose_yaml_content(instance_id, ports)
    
    try:
        with open(compose_file_path, "w", encoding="utf-8") as f:
            f.write(compose_content)
    except Exception as e:
        return False, {}, f"写入 docker-compose.yml 失败: {str(e)}"

    compose_cmd = get_compose_cmd()
    cmd = compose_cmd + ["up", "-d"]

    try:
        res = subprocess.run(
            cmd,
            cwd=instance_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60
        )
        if res.returncode != 0:
            return False, {}, f"Docker 启动命令失败: {res.stderr or res.stdout}"
    except Exception as e:
        return False, {}, f"执行 Docker 命令异常: {str(e)}"

    # 异步轮询捕获 Token 与密码（最多等待 15 秒）
    container_name = f"ts-teamspeak-{instance_id}"
    creds = {
        "admin_token": "",
        "query_user": "serveradmin",
        "query_password": "",
        "query_apikey": ""
    }
    for _ in range(8):
        time.sleep(2)
        try:
            log_res = subprocess.run(
                ["docker", "logs", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            logs = log_res.stdout + "\n" + log_res.stderr
            c = extract_credentials_from_logs(logs)
            if c["admin_token"] or c["query_password"]:
                creds = c
                break
        except Exception:
            pass

    live_status = get_container_status(instance_id)
    if live_status != "running":
        return False, creds, f"容器启动后状态异常: {live_status}"

    if creds["admin_token"] or creds["query_password"] or creds["query_apikey"]:
        return True, creds, "部署成功，首次启动凭据已提取"
    return True, creds, "部署成功，但首次启动凭据尚未从日志提取"

def get_container_status(instance_id: int) -> str:
    """
    获取容器当前运行状态: running, exited, stopped, not_found
    """
    container_name = f"ts-teamspeak-{instance_id}"
    try:
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"

def start_instance_container(instance_id: int) -> bool:
    instance_dir = get_instance_dir(instance_id)
    cmd = get_compose_cmd() + ["start"]
    try:
        res = subprocess.run(cmd, cwd=instance_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return res.returncode == 0
    except Exception:
        return False

def stop_instance_container(instance_id: int) -> bool:
    instance_dir = get_instance_dir(instance_id)
    cmd = get_compose_cmd() + ["stop"]
    try:
        res = subprocess.run(cmd, cwd=instance_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return res.returncode == 0
    except Exception:
        return False

def restart_instance_container(instance_id: int) -> bool:
    instance_dir = get_instance_dir(instance_id)
    cmd = get_compose_cmd() + ["restart"]
    try:
        res = subprocess.run(cmd, cwd=instance_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return res.returncode == 0
    except Exception:
        return False

def destroy_instance_container(instance_id: int, delete_files: bool = True) -> bool:
    instance_dir = get_instance_dir(instance_id)
    cmd = get_compose_cmd() + ["down", "-v"]
    try:
        if not os.path.exists(instance_dir):
            return True

        res = subprocess.run(
            cmd,
            cwd=instance_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30
        )
        if res.returncode != 0:
            return False
        if delete_files:
            shutil.rmtree(instance_dir, ignore_errors=False)
            if os.path.exists(instance_dir):
                return False
        return True
    except Exception:
        return False

def fetch_container_logs(instance_id: int, tail_lines: int = 150) -> str:
    container_name = f"ts-teamspeak-{instance_id}"
    try:
        res = subprocess.run(
            ["docker", "logs", "--tail", str(tail_lines), container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )
        return res.stdout + (("\n[STDERR]\n" + res.stderr) if res.stderr else "")
    except Exception as e:
        return f"获取日志出错: {str(e)}"
