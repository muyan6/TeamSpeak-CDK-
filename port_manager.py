import os
import socket
import subprocess
from typing import Tuple, Dict, Any
from config import BASE_VOICE_PORT, BASE_FILE_PORT, BASE_QUERY_PORT, BASE_TSDNS_PORT, DATA_BASE_DIR
from database import get_all_used_ports, get_next_instance_id

def is_socket_port_free(port: int, proto: str = "tcp") -> bool:
    """
    检查宿主机端口是否未被任何其他应用占用
    """
    try:
        if proto.lower() == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("0.0.0.0", port))
                return True
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return True
    except (OSError, socket.error):
        return False

def is_container_name_taken(container_name: str) -> bool:
    """
    检查宿主机 Docker 是否已存在同名容器
    """
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        return container_name in res.stdout.strip().split()
    except Exception:
        return False

def allocate_ports_for_instance(desired_id: int = None) -> Tuple[int, Dict[str, int]]:
    """
    为新开通的实例分配唯一编号及端口：
    1. 避让官方已被占用的基础端口 (9987, 30033, 10011, 41144)
    2. 检查数据库已分配端口
    3. 检查宿主机当前实际网络 Socket 占用
    4. 检查宿主机 Docker 是否已有历史同名容器 / 历史目录
    返回: (instance_id, {"voice": 9988, "file": 30034, "query": 10012, "tsdns": 41145})
    """
    used_ports_data = get_all_used_ports()
    used_all = set(used_ports_data["all"])
    
    # 官方基础端口直接加入保留占用集合，确保绝对不分配
    used_all.add(BASE_VOICE_PORT)
    used_all.add(BASE_FILE_PORT)
    used_all.add(BASE_QUERY_PORT)
    used_all.add(BASE_TSDNS_PORT)

    # 从 1 开始寻找最小可用连续编号，保证 ts1 -> ts2 -> ts3 严格规律递增与端口严格对应
    candidate_id = desired_id if (desired_id and desired_id > 0) else 1

    while True:
        container_name = f"ts-teamspeak-{candidate_id}"
        dir_name = os.path.join(DATA_BASE_DIR, f"ts{candidate_id}")

        # 检查是否已有宿主机同名容器冲突
        if is_container_name_taken(container_name):
            candidate_id += 1
            continue

        # 计算对应端口：如 ts1 为 9987 + 1 = 9988
        voice_p = BASE_VOICE_PORT + candidate_id
        file_p = BASE_FILE_PORT + candidate_id
        query_p = BASE_QUERY_PORT + candidate_id
        tsdns_p = BASE_TSDNS_PORT + candidate_id

        # 检查是否已在数据库中
        if (voice_p in used_all or file_p in used_all or 
            query_p in used_all or tsdns_p in used_all):
            candidate_id += 1
            continue

        # 检查宿主机实际端口占用（UDP / TCP）
        if not is_socket_port_free(voice_p, proto="udp"):
            candidate_id += 1
            continue
        if not is_socket_port_free(file_p, proto="tcp"):
            candidate_id += 1
            continue
        if not is_socket_port_free(query_p, proto="tcp"):
            candidate_id += 1
            continue
        if not is_socket_port_free(tsdns_p, proto="tcp"):
            candidate_id += 1
            continue

        # 找到完美可用的端口组与实例ID
        return candidate_id, {
            "voice": voice_p,
            "file": file_p,
            "query": query_p,
            "tsdns": tsdns_p
        }
