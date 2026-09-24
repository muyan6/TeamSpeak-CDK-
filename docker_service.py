import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from config import DATA_BASE_DIR, TS_DOCKER_IMAGE

_COMPOSE_CMD_CACHE: Optional[list] = None

def get_compose_cmd() -> list:
    """
    检查系统支持的 docker compose 命令（优先使用 docker compose，其次 docker-compose）
    结果缓存，避免每次启停都重新探测子进程（最长 20s）。
    """
    global _COMPOSE_CMD_CACHE
    if _COMPOSE_CMD_CACHE is not None:
        return list(_COMPOSE_CMD_CACHE)
    try:
        res = subprocess.run(["docker", "compose", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if res.returncode == 0:
            _COMPOSE_CMD_CACHE = ["docker", "compose"]
            return list(_COMPOSE_CMD_CACHE)
    except Exception:
        pass

    try:
        res = subprocess.run(["docker-compose", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if res.returncode == 0:
            _COMPOSE_CMD_CACHE = ["docker-compose"]
            return list(_COMPOSE_CMD_CACHE)
    except Exception:
        pass

    # 默认返回 docker compose
    _COMPOSE_CMD_CACHE = ["docker", "compose"]
    return list(_COMPOSE_CMD_CACHE)

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
            # compose up 可能已在后台拉起容器，失败时按容器名兜底清理，避免孤儿容器长期占用端口
            _force_remove_container(instance_id)
            return False, {}, f"Docker 启动命令失败: {res.stderr or res.stdout}"
    except Exception as e:
        _force_remove_container(instance_id)
        return False, {}, f"执行 Docker 命令异常: {str(e)}"

    # 轮询捕获 Token 与密码：TS3 首次生成 privilege key 常需 20-40 秒，最多等待约 90 秒
    container_name = f"ts-teamspeak-{instance_id}"
    creds = {
        "admin_token": "",
        "query_user": "serveradmin",
        "query_password": "",
        "query_apikey": ""
    }
    for _ in range(45):
        time.sleep(2)
        try:
            log_res = subprocess.run(
                ["docker", "logs", "--tail", "300", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            logs = log_res.stdout + "\n" + log_res.stderr
            c = extract_credentials_from_logs(logs)
            if c["admin_token"] and (c["query_password"] or c["query_apikey"]):
                creds = c
                break
            if c["admin_token"] or c["query_password"]:
                creds = c
        except Exception:
            pass

    live_status = get_container_status(instance_id)
    if live_status != "running":
        return False, creds, f"容器启动后状态异常: {live_status}"

    if creds["admin_token"] or creds["query_password"] or creds["query_apikey"]:
        return True, creds, "部署成功，首次启动凭据已提取"
    # 容器已确认 running，仅凭据尚未落盘：返回成功但提示稍后重新查询 CDK 自动补齐（app.py 有凭据自愈逻辑）
    return True, creds, "容器已启动，但首次启动凭据尚未生成，请稍后重新输入 CDK 查询以自动补齐凭据"

def _force_remove_container(instance_id: int) -> None:
    """按容器名强制删除残留容器（忽略失败），用于 compose up 失败后的兜底清理。"""
    container_name = f"ts-teamspeak-{instance_id}"
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20
        )
    except Exception:
        pass

def get_container_status(instance_id: int) -> str:
    """
    获取容器当前运行状态: running, exited, stopped, not_found, error
    docker 不可用/命令异常时返回 error，调用方不应视为成功。
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
        if "No such" in (res.stderr or ""):
            return "not_found"
        return "error"
    except Exception:
        return "error"

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

def _robust_rmtree(path: str):
    import stat
    def on_err(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except Exception:
            pass
    try:
        shutil.rmtree(path, onerror=on_err)
    except Exception:
        pass
    if os.path.exists(path):
        time.sleep(0.5)
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

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
        if delete_files and os.path.exists(instance_dir):
            _robust_rmtree(instance_dir)
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

def extract_credentials_from_container(instance_id: int) -> Dict[str, str]:
    container_name = f"ts-teamspeak-{instance_id}"
    try:
        log_res = subprocess.run(
            ["docker", "logs", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )
        logs = log_res.stdout + "\n" + log_res.stderr
        return extract_credentials_from_logs(logs)
    except Exception:
        return {"admin_token": "", "query_user": "serveradmin", "query_password": "", "query_apikey": ""}
