import shutil
import subprocess
from typing import List, Tuple
from config import (
    SERVER_PORT, BASE_VOICE_PORT, BASE_FILE_PORT, BASE_QUERY_PORT, BASE_TSDNS_PORT,
    FIREWALL_PORT_SPAN,
)


def run_cmd(cmd: List[str]) -> Tuple[bool, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return res.returncode == 0, (res.stdout or "") + (res.stderr or "")
    except Exception as e:
        return False, str(e)


def _normalize_port_arg(port: str) -> str:
    """ufw / iptables 的端口范围必须使用冒号分隔（60000:60200），连字符会被判为 Bad port。"""
    return port.replace("-", ":") if "-" in port else port


def _port_span() -> int:
    """
    计算需要预放行的端口段宽度。
    端口分配允许一路递增到 65535，固定的 200 宽度会漏放后续实例，
    因此这里以「数据库已分配的最大实例号」为准动态扩展（至少保留配置的基础宽度）。
    """
    span = max(1, int(FIREWALL_PORT_SPAN or 200))
    try:
        from database import get_all_instances
        instances = get_all_instances() or []
        if instances:
            max_id = max(int(i.get("id") or 0) for i in instances)
            # 额外预留 20 个空位，避免每次新建实例都要重新放宽规则
            span = max(span, max_id + 20)
    except Exception:
        pass
    return span


def _build_port_rules() -> List[Tuple[str, str]]:
    span = _port_span()
    return [
        (str(SERVER_PORT), "tcp"),                                    # Web 管理平台
        (f"{BASE_VOICE_PORT}-{BASE_VOICE_PORT + span}", "udp"),       # TeamSpeak 语音端口段
        (f"{BASE_FILE_PORT}-{BASE_FILE_PORT + span}", "tcp"),         # 文件传输端口段
        (f"{BASE_QUERY_PORT}-{BASE_QUERY_PORT + span}", "tcp"),       # ServerQuery 查询端口段
        (f"{BASE_TSDNS_PORT}-{BASE_TSDNS_PORT + span}", "tcp"),       # TSDNS 端口段
    ]


def auto_open_firewall_ports():
    """
    自动检测系统内部防火墙（firewalld / ufw / iptables）并放行 TeamSpeak 规划端口段与 Web 端口
    """
    print("[*] 正在自动检测并配置服务器本地防火墙规则...")

    # 动态构建待放行的端口段（根据 config.py 基础配置 + 实际已分配的最大实例号）
    port_rules = _build_port_rules()

    # 1. 优先检测 firewalld (CentOS / RHEL / OpenCloudOS / Fedora)
    if shutil.which("firewall-cmd"):
        ok, out = run_cmd(["firewall-cmd", "--state"])
        if ok and "running" in out:
            print("[*] 检测到 firewalld 正在运行，正在自动放行端口段...")
            failed = 0
            for port, proto in port_rules:
                ok_add, out_add = run_cmd(["firewall-cmd", "--permanent", f"--add-port={port}/{proto}"])
                if not ok_add:
                    failed += 1
                    print(f"[!] firewalld 放行 {port}/{proto} 失败: {out_add.strip()}")
            ok_reload, out_reload = run_cmd(["firewall-cmd", "--reload"])
            if not ok_reload:
                print(f"[!] firewalld 重新加载失败: {out_reload.strip()}")
            if failed:
                print(f"[!] firewalld 规则已尝试添加，其中 {failed} 条失败，请手动检查防火墙配置")
            else:
                print("[+] firewalld 防火墙规则已自动放行并重新加载成功！")
            return

    # 2. 检测 ufw (Ubuntu / Debian)
    if shutil.which("ufw"):
        ok, out = run_cmd(["ufw", "status"])
        if ok and "active" in out:
            print("[*] 检测到 ufw 正在运行，正在自动放行端口段...")
            failed = 0
            for port, proto in port_rules:
                ufw_port = _normalize_port_arg(port)
                ok_allow, out_allow = run_cmd(["ufw", "allow", f"{ufw_port}/{proto}"])
                if not ok_allow:
                    failed += 1
                    print(f"[!] ufw 放行 {ufw_port}/{proto} 失败: {out_allow.strip()}")
            if failed:
                print(f"[!] ufw 规则已尝试添加，其中 {failed} 条失败，请手动检查防火墙配置")
            else:
                print("[+] ufw 防火墙规则已自动放行成功！")
            return

    # 3. 检测 iptables
    if shutil.which("iptables"):
        added = 0
        failed = 0
        for port, proto in port_rules:
            dport = _normalize_port_arg(port)
            # 先 -C 判断规则是否已存在，避免每次启动重复插入导致规则无限累积
            ok_check, _ = run_cmd(["iptables", "-C", "INPUT", "-p", proto, "--dport", dport, "-j", "ACCEPT"])
            if ok_check:
                continue
            ok_add, out_add = run_cmd(["iptables", "-I", "INPUT", "-p", proto, "--dport", dport, "-j", "ACCEPT"])
            if ok_add:
                added += 1
            else:
                failed += 1
                print(f"[!] iptables 放行 {dport}/{proto} 失败: {out_add.strip()}")
        if failed:
            print(f"[!] iptables 共新增 {added} 条规则，{failed} 条失败（iptables 规则不会自动持久化，重启后需重新执行）")
        else:
            print(f"[+] iptables 规则已自动添加放行规则（新增 {added} 条，已存在规则自动跳过）！")
        return

    print("[*] 服务器本地防火墙未开启或已处于放行状态。")


def open_single_instance_ports(voice_port: int, file_port: int, query_port: int, tsdns_port: int):
    """
    针对单个实例创建时进行即时端口放行补充
    """
    rules = [
        (voice_port, "udp"),
        (file_port, "tcp"),
        (query_port, "tcp"),
        (tsdns_port, "tcp"),
    ]

    if shutil.which("firewall-cmd"):
        ok, out = run_cmd(["firewall-cmd", "--state"])
        if ok and "running" in out:
            for _p, _proto in rules:
                ok_add, out_add = run_cmd(["firewall-cmd", "--permanent", f"--add-port={_p}/{_proto}"])
                if not ok_add:
                    print(f"[!] firewalld 放行 {_p}/{_proto} 失败: {out_add.strip()}")
            run_cmd(["firewall-cmd", "--reload"])
            return

    if shutil.which("ufw"):
        ok, out = run_cmd(["ufw", "status"])
        if ok and "active" in out:
            for _p, _proto in rules:
                ok_allow, out_allow = run_cmd(["ufw", "allow", f"{_p}/{_proto}"])
                if not ok_allow:
                    print(f"[!] ufw 放行 {_p}/{_proto} 失败: {out_allow.strip()}")
            return

    if shutil.which("iptables"):
        for _p, _proto in rules:
            ok_check, _ = run_cmd(["iptables", "-C", "INPUT", "-p", _proto, "--dport", str(_p), "-j", "ACCEPT"])
            if ok_check:
                continue
            ok_add, out_add = run_cmd(["iptables", "-I", "INPUT", "-p", _proto, "--dport", str(_p), "-j", "ACCEPT"])
            if not ok_add:
                print(f"[!] iptables 放行 {_p}/{_proto} 失败: {out_add.strip()}")
        return