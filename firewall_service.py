import shutil
import subprocess
from typing import List, Tuple

def run_cmd(cmd: List[str]) -> Tuple[bool, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return res.returncode == 0, res.stdout + res.stderr
    except Exception as e:
        return False, str(e)

def auto_open_firewall_ports():
    """
    自动检测系统内部防火墙（firewalld / ufw / iptables）并放行 TeamSpeak 规划端口段与 Web 端口
    """
    print("[*] 正在自动检测并配置服务器本地防火墙规则...")

    # 待放行的端口段
    port_rules = [
        ("12345", "tcp"),       # Web 管理平台
        ("60000-60100", "udp"), # TeamSpeak 语音端口段
        ("20000-20100", "tcp"), # 文件传输端口段
        ("30000-30100", "tcp"), # ServerQuery 查询端口段
        ("40000-40100", "tcp"), # TSDNS 端口段
    ]

    # 1. 优先检测 firewalld (CentOS / RHEL / OpenCloudOS / Fedora)
    if shutil.which("firewall-cmd"):
        ok, out = run_cmd(["firewall-cmd", "--state"])
        if ok and "running" in out:
            print("[*] 检测到 firewalld 正在运行，正在自动放行端口段...")
            for port, proto in port_rules:
                run_cmd(["firewall-cmd", "--permanent", f"--add-port={port}/{proto}"])
            run_cmd(["firewall-cmd", "--reload"])
            print("[+] firewalld 防火墙规则已自动放行并重新加载成功！")
            return

    # 2. 检测 ufw (Ubuntu / Debian)
    if shutil.which("ufw"):
        ok, out = run_cmd(["ufw", "status"])
        if ok and "active" in out:
            print("[*] 检测到 ufw 正在运行，正在自动放行端口段...")
            for port, proto in port_rules:
                run_cmd(["ufw", "allow", f"{port}/{proto}"])
            print("[+] ufw 防火墙规则已自动放行成功！")
            return

    # 3. 检测 iptables
    if shutil.which("iptables"):
        try:
            for port, proto in port_rules:
                if "-" in port:
                    start_p, end_p = port.split("-")
                    run_cmd(["iptables", "-I", "INPUT", "-p", proto, "--dport", f"{start_p}:{end_p}", "-j", "ACCEPT"])
                else:
                    run_cmd(["iptables", "-I", "INPUT", "-p", proto, "--dport", port, "-j", "ACCEPT"])
            print("[+] iptables 规则已自动添加放行规则！")
        except Exception as e:
            print(f"[!] iptables 放行提示: {e}")
            return

    print("[*] 服务器本地防火墙未开启或已处于放行状态。")

def open_single_instance_ports(voice_port: int, file_port: int, query_port: int, tsdns_port: int):
    """
    针对单个实例创建时进行即时端口放行补充
    """
    if shutil.which("firewall-cmd"):
        ok, out = run_cmd(["firewall-cmd", "--state"])
        if ok and "running" in out:
            run_cmd(["firewall-cmd", "--permanent", f"--add-port={voice_port}/udp"])
            run_cmd(["firewall-cmd", "--permanent", f"--add-port={file_port}/tcp"])
            run_cmd(["firewall-cmd", "--permanent", f"--add-port={query_port}/tcp"])
            run_cmd(["firewall-cmd", "--permanent", f"--add-port={tsdns_port}/tcp"])
            run_cmd(["firewall-cmd", "--reload"])
