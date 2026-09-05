import os
import re
import socket
import subprocess
import ipaddress
import urllib.request
import json
from pathlib import Path
from typing import Optional, Literal, Dict, Any, Tuple, List
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import asyncio
from datetime import datetime, timedelta
import config
from database import (
    init_db,
    get_cdk,
    create_cdks,
    get_all_cdks,
    delete_cdk,
    delete_cdks,
    delete_cdks_by_filter,
    bind_cdk_instance,
    bind_cdk_bot,
    get_instance_by_id,
    get_instance_by_cdk,
    get_all_instances,
    create_instance,
    update_instance_token,
    update_instance_status,
    update_instance_expiry,
    get_expired_active_instances,
    renew_instance,
    delete_instance,
    delete_instances,
    get_all_used_ports,
    create_bot_instance,
    get_bot_instance_by_id,
    get_bot_instance_by_cdk,
    get_all_bot_instances,
    restore_bot_cdk,
    restore_instance_cdk,
    update_bot_instance_status,
    delete_bot_instance,
    delete_bot_instances,
    get_expired_active_bots,
    renew_bot_instance,
    update_bot_instance_expiry,
    get_admin_password,
    set_admin_password,
    get_bot_config,
    set_bot_config,
    get_bot_permission_config,
    set_bot_permission_config,
    has_server_used_trial,
    record_trial_server,
    get_all_trial_records,
    delete_trial_record,
    delete_trial_record_for_target,
    claim_cdk,
    release_cdk_claim,
    unbind_cdk_instance,
    unbind_cdk_bot,
    reserve_trial_server,
    release_trial_reservation,
    normalize_server_target,
    get_dns_config,
    set_dns_config,
    is_subdomain_available,
    update_instance_domain
)
from port_manager import allocate_ports_for_instance
from docker_service import (
    deploy_teamspeak_instance,
    get_container_status,
    start_instance_container,
    stop_instance_container,
    restart_instance_container,
    destroy_instance_container,
    fetch_container_logs
)
from music_bot_service import music_bot_client
from firewall_service import auto_open_firewall_ports, open_single_instance_ports
from dns_service import dns_service, validate_subdomain_format, clean_subdomain_prefix

from contextlib import asynccontextmanager

async def system_expiry_checker():
    """
    后台守护任务：定期扫描所有已到期的音乐机器人和 TeamSpeak 服务器实例，自动停机下线并标记状态为 expired
    """
    while True:
        try:
            # 1. 扫描已到期的音乐机器人
            expired_bots = get_expired_active_bots()
            for b in expired_bots:
                try:
                    print(f"[*] ⏰ 监测到机器人 [{b['name']}] (ID: {b['bot_id']}) 已到达有效期限 ({b['expire_at']})，正在执行自动停机下线...")
                    stop_ok, stop_res = await asyncio.to_thread(music_bot_client.stop_bot, b["bot_id"])
                    if not stop_ok:
                        print(f"[Warning] 机器人 [{b['bot_id']}] 停机响应: {stop_res}，依然标记状态为 expired")
                except Exception as b_err:
                    print(f"[Warning] 停止机器人 [{b['bot_id']}] 发生异常: {b_err}")
                finally:
                    update_bot_instance_status(b["bot_id"], "expired")

            # 2. 扫描已到期的 TeamSpeak 语音服务器
            expired_instances = get_expired_active_instances()
            for inst in expired_instances:
                try:
                    print(f"[*] ⏰ 监测到 TeamSpeak 服务器 [{inst['name']}] (ID: {inst['id']}) 已到达有效期限 ({inst['expire_at']})，正在执行自动停机下线...")
                    stop_ok = await asyncio.to_thread(stop_instance_container, inst["id"])
                    if not stop_ok:
                        print(f"[Warning] TeamSpeak 容器 [{inst['id']}] 停止未成功，依然标记状态为 expired")
                except Exception as inst_err:
                    print(f"[Warning] 停止 TeamSpeak 容器 [{inst['id']}] 发生异常: {inst_err}")
                finally:
                    update_instance_status(inst["id"], "expired")
        except Exception as err:
            print(f"[Error in system_expiry_checker]: {err}")
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 确保存储目录存在
    try:
        os.makedirs(config.DATA_BASE_DIR, exist_ok=True)
    except Exception as e:
        print(f"[Warning] 无法创建数据根目录: {config.DATA_BASE_DIR}, 错误: {e}")

    # 自动放行服务器本地防火墙端口
    try:
        auto_open_firewall_ports()
    except Exception as e:
        print(f"[Warning] 自动配置本地防火墙异常: {e}")

    print(f"[*] TeamSpeak 管理服务已启动，监听端口: {config.SERVER_PORT}")
    print(f"[*] 数据存储根目录: {config.DATA_BASE_DIR}")
    print(f"[*] 音乐机器人对接中心: {get_bot_config()['bot_panel_url']}")
    
    # 启动到期自动停机监控后台任务
    checker_task = asyncio.create_task(system_expiry_checker())
    yield
    checker_task.cancel()

app = FastAPI(
    title="TeamSpeak Automated Hosting Platform",
    version="1.0.0",
    lifespan=lifespan
)

# 挂载静态文件与模板
BASE_DIR = Path(__file__).parent.resolve()
static_path = BASE_DIR / "static"
templates_path = BASE_DIR / "templates"

os.makedirs(static_path / "css", exist_ok=True)
os.makedirs(static_path / "js", exist_ok=True)
os.makedirs(templates_path, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
templates = Jinja2Templates(directory=str(templates_path))

# --- 请求模型 ---

class RedeemRequest(BaseModel):
    cdk: str = Field(min_length=1, max_length=128)
    subdomain: Optional[str] = Field(default=None, max_length=64)

class CheckSubdomainRequest(BaseModel):
    subdomain: str = Field(min_length=1, max_length=64)

class DnsConfigRequest(BaseModel):
    dns_enabled: bool = False
    dns_provider: Literal["disabled", "cloudflare", "aliyun", "tencent"] = "disabled"
    dns_root_domain: str = Field(default="", max_length=253)
    dns_target_host: Optional[str] = Field(default="", max_length=253)
    dns_cf_token: Optional[str] = Field(default="", max_length=500)
    dns_cf_zone_id: Optional[str] = Field(default="", max_length=500)
    dns_aliyun_ak: Optional[str] = Field(default="", max_length=500)
    dns_aliyun_sk: Optional[str] = Field(default="", max_length=500)
    dns_tencent_id: Optional[str] = Field(default="", max_length=500)
    dns_tencent_key: Optional[str] = Field(default="", max_length=500)

class TestDnsConfigRequest(BaseModel):
    dns_provider: Literal["disabled", "cloudflare", "aliyun", "tencent"] = "disabled"
    dns_root_domain: Optional[str] = Field(default="", max_length=253)
    dns_target_host: Optional[str] = Field(default="", max_length=253)
    dns_cf_token: Optional[str] = Field(default="", max_length=500)
    dns_cf_zone_id: Optional[str] = Field(default="", max_length=500)
    dns_aliyun_ak: Optional[str] = Field(default="", max_length=500)
    dns_aliyun_sk: Optional[str] = Field(default="", max_length=500)
    dns_tencent_id: Optional[str] = Field(default="", max_length=500)
    dns_tencent_key: Optional[str] = Field(default="", max_length=500)

class RedeemBotRequest(BaseModel):
    cdk: str = Field(min_length=1, max_length=128)
    name: str = Field(default="我的音乐机器人", min_length=1, max_length=100)
    serverAddress: str = Field(min_length=1, max_length=253)
    serverPort: int = Field(default=9987, ge=1, le=65535)
    nickname: str = Field(default="MusicBot", min_length=1, max_length=100)
    defaultChannel: Optional[str] = Field(default=None, max_length=200)
    serverPassword: Optional[str] = Field(default=None, max_length=255)
    webUsername: Optional[str] = Field(default=None, max_length=64)
    webPassword: Optional[str] = Field(default=None, max_length=128)

class GenerateCdksRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=200)
    remark: Optional[str] = Field(default="", max_length=500)
    cdk_type: Literal["teamspeak", "music_bot"] = "teamspeak"
    duration_months: Literal[0, 1, 3, 6, 12] = 0
    is_trial: Literal[0, 1] = 0

class InstanceActionRequest(BaseModel):
    action: Literal["start", "stop", "restart", "destroy"]

class BindInstanceDomainRequest(BaseModel):
    subdomain_prefix: str = Field(min_length=2, max_length=32)

class BotActionRequest(BaseModel):
    action: Literal["start", "stop", "restart", "delete"]
    cdk: Optional[str] = Field(default=None, max_length=128)

class RenewBotRequest(BaseModel):
    cdk: str = Field(min_length=1, max_length=128)
    bot_id: str = Field(min_length=1, max_length=128)

class RenewInstanceRequest(BaseModel):
    cdk: str = Field(min_length=1, max_length=128)
    instance_id: int = Field(gt=0)

class BatchDeleteFilter(BaseModel):
    cdk_type: Literal["all", "teamspeak", "music_bot"] = "all"
    duration_months: Optional[Literal[0, 1, 3, 6, 12]] = None
    is_trial: Optional[Literal[0, 1]] = None
    status: Literal["all", "unused", "used", "disabled", "processing"] = "all"

class BatchDeleteCdksRequest(BaseModel):
    codes: Optional[List[str]] = None
    filter: Optional[BatchDeleteFilter] = None

class BatchActionInstancesRequest(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=200)
    action: Literal["start", "stop", "restart", "destroy"]

class BatchActionBotsRequest(BaseModel):
    bot_ids: List[str] = Field(min_length=1, max_length=200)
    action: Literal["start", "stop", "restart", "delete"]

class AdminRenewBotRequest(BaseModel):
    duration_months: Optional[int] = 1
    cdk: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=255)
    new_password: str = Field(min_length=6, max_length=255)

class BotConfigRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    user: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    tutorial_url: Optional[str] = Field(default=None, max_length=500)

class TestBotConfigRequest(BaseModel):
    url: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None

class BotPermissionConfigRequest(BaseModel):
    role: str = Field(default="member", min_length=1, max_length=64)
    capabilities: List[str] = Field(default_factory=list)
    bot_scope: Literal["current", "all"] = "current"
    permission_notice: Optional[str] = Field(default=None, max_length=255)

class ParseTsTargetRequest(BaseModel):
    input: str = Field(min_length=1, max_length=5000)

# --- 辅助工具函数 ---

def resolve_srv_record(domain: str) -> Tuple[Optional[str], Optional[int]]:
    """
    通过 nslookup 查询 TeamSpeak SRV 记录 (_ts3._udp.<domain>)
    返回: (srv_host, srv_port)
    """
    try:
        cmd = ["nslookup", "-type=SRV", f"_ts3._udp.{domain}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        out = proc.stdout
        port_m = re.search(r"port\s*=\s*(\d+)", out, re.I)
        host_m = re.search(r"svr hostname\s*=\s*([^\s\r\n]+)", out, re.I)
        if port_m and host_m:
            srv_port = int(port_m.group(1))
            srv_host = host_m.group(1).rstrip(".")
            return srv_host, srv_port
    except Exception:
        pass
    return None, None

def get_ip_geo_info(ip_str: str) -> Dict[str, Any]:
    """
    查询 IP 的地理归属与是否为境外节点
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback:
            return {"is_overseas": False, "country": "内网/局域网", "location": "本地内网"}
    except Exception:
        return {"is_overseas": False, "country": "未知", "location": "未知"}

    try:
        req = urllib.request.Request(
            f"http://ip-api.com/json/{ip_str}?lang=zh-CN",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "success":
                code = data.get("countryCode", "")
                country = data.get("country", "")
                city = data.get("city", "")
                is_overseas = (code != "CN")
                loc = f"{country} {city}".strip() if country else "未知"
                return {"is_overseas": is_overseas, "country": country or "未知", "location": loc}
    except Exception:
        pass

    return {"is_overseas": False, "country": "国内/未知", "location": "默认线路"}

# --- 权限校验依赖 ---

def verify_admin(x_admin_password: Optional[str] = Header(None, alias="X-Admin-Password")):
    current_pwd = get_admin_password()
    if not x_admin_password or x_admin_password != current_pwd:
        raise HTTPException(status_code=401, detail="管理员密码错误或未提供")
    return True

def get_public_host(request: Request) -> str:
    """只返回规范化主机名，避免直接信任 Host 头造成错误连接地址。"""
    candidate = (
        (config.PUBLIC_SERVER_IP or "").strip()
        or request.headers.get("x-forwarded-host", "").strip()
        or request.headers.get("host", "127.0.0.1").strip()
    )
    try:
        _, clean_addr, _, _, _ = normalize_server_target(candidate, 9987)
        return f"[{clean_addr}]" if ":" in clean_addr else clean_addr
    except (TypeError, ValueError, OSError):
        return "127.0.0.1"

def claim_error_response(code: str, expected_type: str) -> JSONResponse:
    current = get_cdk(code)
    if not current:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})
    if current.get("cdk_type") != expected_type:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 类型与当前操作不匹配"})
    if current.get("status") == "processing":
        return JSONResponse(status_code=409, content={"success": False, "message": "该 CDK 正在处理中，请稍后查询结果"})
    return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

# --- 页面路由 ---

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    bot_cfg = get_bot_config()
    ctx = {
        "request": request,
        "bot_panel_url": bot_cfg.get("bot_panel_url", ""),
        "bot_tutorial_url": bot_cfg.get("bot_tutorial_url", "")
    }
    try:
        return templates.TemplateResponse(request=request, name="index.html", context=ctx)
    except TypeError:
        return templates.TemplateResponse("index.html", ctx)

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    bot_cfg = get_bot_config()
    ctx = {
        "request": request,
        "bot_panel_url": bot_cfg.get("bot_panel_url", "")
    }
    try:
        return templates.TemplateResponse(request=request, name="admin.html", context=ctx)
    except TypeError:
        return templates.TemplateResponse("admin.html", ctx)

# --- 用户端 API ---

@app.post("/api/parse-ts-target")
def parse_ts_target_endpoint(req: ParseTsTargetRequest):
    """
    智能解析 TeamSpeak 日志/域名/IP，区分境外源站与国内中转节点，优先提取中转地址
    """
    raw_text = req.input.strip()
    if not raw_text:
        return JSONResponse(status_code=400, content={"success": False, "message": "输入内容不能为空"})

    connect_host = None
    connect_port = None
    srv_target_host = None
    srv_target_port = None
    direct_ip = None
    direct_port = None
    lookup_ip = None
    lookup_port = None

    # 1. 匹配 Connect to server: host[:port]
    m_conn = list(re.finditer(r"Connect to server:\s*([^\s\r\n]+)", raw_text, re.I))
    if m_conn:
        val = m_conn[-1].group(1).strip()
        if ":" in val and not val.startswith("http"):
            parts = val.split(":")
            connect_host = parts[0].strip()
            try:
                connect_port = int(parts[1].strip())
            except Exception:
                pass
        else:
            connect_host = val

    # 2. 匹配 Trying to resolve
    if not connect_host:
        m_try = list(re.finditer(r"Trying to resolve\s*([^\s\r\n]+)", raw_text, re.I))
        if m_try:
            connect_host = m_try[-1].group(1).strip()

    # 3. 匹配 SRV DNS resolve successful
    m_srv = list(re.finditer(r'SRV DNS resolve successful[^\n\r]*?(?:=>|->)\s*"?([a-zA-Z0-9.\-]+):(\d+)', raw_text, re.I))
    if m_srv:
        srv_target_host = m_srv[-1].group(1).strip()
        srv_target_port = int(m_srv[-1].group(2).strip())

    # 4. 匹配 Lookup finished
    m_look = list(re.finditer(r'Lookup finished:.*?ip:([0-9a-zA-Z.:\-]+).*?port:(\d+)', raw_text, re.I))
    if m_look:
        lookup_ip = m_look[-1].group(1).strip()
        lookup_port = int(m_look[-1].group(2).strip())

    # 5. 匹配 Resolve successful / Initiating connection
    m_direct = list(re.finditer(r'(?:Resolve successful|Initiating connection|Connected to)[:\s]+(?:\[([0-9a-fA-F:]+)\]|([0-9]{1,3}(?:\.[0-9]{1,3}){3}|[a-zA-Z0-9.\-]+)):(\d+)', raw_text, re.I))
    if m_direct:
        direct_ip = m_direct[-1].group(1) or m_direct[-1].group(2)
        direct_port = int(m_direct[-1].group(3).strip())

    # 6. 单行输入容错 (IP:PORT 或 DOMAIN:PORT)
    if not connect_host and not direct_ip:
        m_generic = list(re.finditer(r'(?:(?:https?:\/\/)?(?:www\.)?([a-zA-Z0-9.\-]+(?:\.[a-zA-Z]{2,}|(?:\d{1,3}\.){3}\d{1,3}))):(\d{2,5})', raw_text))
        if m_generic:
            connect_host = m_generic[-1].group(1).strip()
            connect_port = int(m_generic[-1].group(2).strip())
        else:
            # 纯域名或纯 IP
            m_ip = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', raw_text)
            if m_ip:
                direct_ip = m_ip.group(0)
            else:
                m_dom = re.search(r'\b([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}\b', raw_text)
                if m_dom:
                    connect_host = m_dom.group(0)

    # 语音端口判定优先级
    final_port = srv_target_port or lookup_port or direct_port or connect_port or 9987

    # 若输入了域名且尚未提取到 SRV，主动通过后端 DNS 查询 SRV
    host_to_query = connect_host or direct_ip
    is_domain = False
    if host_to_query:
        try:
            ipaddress.ip_address(host_to_query)
        except ValueError:
            is_domain = True
            if not srv_target_host:
                s_host, s_port = resolve_srv_record(host_to_query)
                if s_host:
                    srv_target_host = s_host
                    srv_target_port = s_port or final_port
                    final_port = srv_target_port

    # 解析底层真实 IP
    resolved_underlying_ip = direct_ip or lookup_ip
    if not resolved_underlying_ip and host_to_query:
        try:
            resolved_underlying_ip = socket.getaddrinfo(host_to_query, None, type=socket.SOCK_STREAM)[0][4][0]
        except Exception:
            pass

    # 查询 IP 地理归属
    geo_info = {"is_overseas": False, "country": "国内/未知", "location": "默认线路"}
    if resolved_underlying_ip:
        geo_info = get_ip_geo_info(resolved_underlying_ip)

    # 确定中转节点 (transit_host)、推荐连接主机 (recommended_host) 与源站 (origin_ip)
    origin_ip = resolved_underlying_ip or direct_ip or connect_host or "127.0.0.1"

    # 判断节点类型与提示
    is_overseas = geo_info.get("is_overseas", False)
    loc_str = geo_info.get("location", "") or geo_info.get("country", "")

    recommended_host = None
    transit_host = None

    if srv_target_host:
        transit_host = srv_target_host
        recommended_host = srv_target_host
        node_type = "srv_relay"
        if is_overseas:
            badge_text = "⚡ SRV 国内中转节点（免翻墙）"
            badge_class = "badge badge-success"
            message = f"检测到境外源站 ({origin_ip} - {loc_str}) 已配置 SRV 国内中转 ({srv_target_host})，已自动优选中转线路避免直连阻断。"
        else:
            badge_text = "⚡ 国内高速线路（SRV 解析）"
            badge_class = "badge badge-success"
            message = f"检测到目标服务器为国内线路 ({origin_ip} - {loc_str})，已通过 SRV 解析提取真实端口与连接节点 ({srv_target_host})。"
    elif is_domain and not is_overseas:
        # 国内服务器 + 域名解析：底层已确认是国内机房 IP，优先推荐国内直连 IP（免 DNS 解析失败风险及额外解析开销）
        recommended_host = origin_ip
        transit_host = connect_host
        node_type = "domestic_direct"
        badge_text = "🟢 国内直连服务器 (直连推荐)"
        badge_class = "badge badge-success"
        message = f"检测到目标为国内机房服务器 ({origin_ip} - {loc_str})，已成功解析底层直连 IP，推荐直接使用国内直连 IP 连接以获得最稳定连接。"
    elif is_domain and is_overseas:
        # 境外服务器 + 域名解析
        recommended_host = connect_host or origin_ip
        transit_host = connect_host
        node_type = "domain_overseas"
        badge_text = f"⚠️ 境外服务器域名 ({loc_str or '海外'})"
        badge_class = "badge badge-warning"
        message = f"检测到目标底层为境外服务器 ({origin_ip} - {loc_str})，国内直连可能受阻或有较高延迟。"
    elif is_overseas:
        # 境外直连 IP
        recommended_host = origin_ip
        transit_host = origin_ip
        node_type = "overseas_origin"
        badge_text = f"⚠️ 境外直连源站 ({geo_info.get('country', '境外')})"
        badge_class = "badge badge-warning"
        message = f"检测到该地址为境外服务器直连 IP ({origin_ip} - {loc_str})，国内直连可能受阻或丢包，建议使用中转地址。"
    else:
        # 国内直连 IP
        recommended_host = origin_ip
        transit_host = origin_ip
        node_type = "direct"
        badge_text = f"🟢 国内直连节点 ({loc_str or '国内'})"
        badge_class = "badge badge-info"
        message = f"已成功提取国内服务器连接信息 ({origin_ip} - {loc_str}) 与语音端口。"

    return JSONResponse(status_code=200, content={
        "success": True,
        "recommended_host": recommended_host,
        "recommended_port": final_port,
        "transit_host": transit_host or recommended_host,
        "transit_port": final_port,
        "origin_ip": origin_ip,
        "origin_port": final_port,
        "domain_host": connect_host if is_domain else None,
        "target_host": connect_host or origin_ip,
        "full_address": f"{recommended_host}:{final_port}",
        "is_default_port": final_port == 9987,
        "node_type": node_type,
        "is_overseas": is_overseas,
        "geo_info": geo_info,
        "badge_text": badge_text,
        "badge_class": badge_class,
        "message": message
    })

@app.get("/api/dns-info")
def get_dns_info_endpoint():
    cfg = get_dns_config()
    bot_cfg = get_bot_config()
    return {
        "success": True,
        "dns_enabled": cfg.get("dns_enabled", False),
        "dns_provider": cfg.get("dns_provider", "disabled"),
        "root_domain": cfg.get("dns_root_domain", ""),
        "bot_tutorial_url": bot_cfg.get("bot_tutorial_url", "http://103.71.69.156:23452/")
    }

@app.post("/api/check-subdomain")
def check_subdomain_endpoint(req: CheckSubdomainRequest):
    sub = (req.subdomain or "").strip()
    available, msg, full_domain = is_subdomain_available(sub)
    return {
        "success": True,
        "available": available,
        "message": msg,
        "full_domain": full_domain,
        "subdomain": sub
    }

@app.post("/api/redeem")
def redeem_cdk(req: RedeemRequest, request: Request):
    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        # 自愈与容错机制：检测是否为已激活实例但绑定的 CDK 记录在 cdks 表中被误删
        existing_bot = get_bot_instance_by_cdk(code)
        if existing_bot:
            cdk_info = restore_bot_cdk(code, existing_bot["bot_id"], existing_bot.get("duration_months", 1))
        else:
            existing_inst = get_instance_by_cdk(code)
            if existing_inst:
                cdk_info = restore_instance_cdk(code, existing_inst["id"], existing_inst.get("duration_months", 0))

    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在，请检查后重试"})

    if cdk_info["status"] == "disabled":
        return JSONResponse(status_code=403, content={"success": False, "message": "该 CDK 已被系统禁用"})

    cdk_type = cdk_info.get("cdk_type", "teamspeak")

    # === 分支 1: 音乐机器人 CDK ===
    if cdk_type == "music_bot":
        # 如果已兑换过，直接返回已绑定的机器人实例信息
        if cdk_info["status"] == "used":
            bot_id = cdk_info.get("bot_id")
            bot = get_bot_instance_by_cdk(code) or (get_bot_instance_by_id(bot_id) if bot_id else None)
            if bot:
                # 尝试获取远程实时运行状态
                ok, remote_status = music_bot_client.get_bot(bot["bot_id"])
                if ok and isinstance(remote_status, dict):
                    bot["remote_status"] = remote_status
                return {
                    "success": True,
                    "type": "music_bot",
                    "message": f"该音乐机器人 CDK 已于 {cdk_info['used_at']} 激活",
                    "instance": bot,
                    "bot_panel_url": get_bot_config()["bot_panel_url"],
                    "bot_tutorial_url": get_bot_config().get("bot_tutorial_url", "http://103.71.69.156:23452/"),
                    "permission_notice": get_bot_permission_config().get("permission_notice", "月卡用户仅有控制功能，年卡用户独享音乐后台")
                }
            return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已被激活使用，但绑定的音乐机器人实例已不存在"})

        if cdk_info["status"] != "unused":
            return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 状态异常或不可用"})

        # 未使用：返回需要前端填写机器人连接配置
        is_trial = cdk_info.get("is_trial", 0)
        duration_m = cdk_info.get("duration_months", 1)
        duration_desc = "体验卡 (1个月/限用1次)" if is_trial else (f"{duration_m} 个月" if duration_m > 0 else "永久")
        return {
            "success": True,
            "type": "music_bot",
            "status": "unused",
            "need_config": True,
            "cdk": code,
            "is_trial": bool(is_trial),
            "duration_months": duration_m,
            "duration_desc": duration_desc,
            "bot_tutorial_url": get_bot_config().get("bot_tutorial_url", "http://103.71.69.156:23452/"),
            "message": f"CDK 验证成功！当前为【音乐机器人 - {duration_desc}】，请填写目标服务器连接配置以启动机器人"
        }

    # === 分支 2: TeamSpeak 语音服务器 CDK ===
    client_host = get_public_host(request)

    # 如果该 CDK 已经兑换过，直接返回已绑定的实例信息
    if cdk_info["status"] == "used":
        instance_id = cdk_info.get("instance_id")
        instance = get_instance_by_id(instance_id) if instance_id else None
        if instance:
            dns_cfg = get_dns_config()
            dns_enabled = dns_cfg.get("dns_enabled", False)
            subdomain_input = (req.subdomain or "").strip()

            # 智能补绑：如果当前实例尚未绑定二级域名，且用户在前台填写了二级域名前缀，且系统开启了DNS，则自动补绑！
            if dns_enabled and subdomain_input and not instance.get("subdomain"):
                avail, err_msg, full_domain = is_subdomain_available(subdomain_input)
                if not avail:
                    return JSONResponse(status_code=400, content={"success": False, "message": f"二级域名不可用: {err_msg}"})
                target_host = dns_cfg.get("dns_target_host") or client_host
                ok_dns, rec_id, full_d, err_dns = dns_service.create_ts_srv_record(
                    subdomain_prefix=subdomain_input,
                    target_host=target_host,
                    voice_port=instance["voice_port"],
                    dns_cfg=dns_cfg
                )
                if ok_dns:
                    update_instance_domain(instance["id"], full_d, rec_id)
                    instance["subdomain"] = full_d
                    instance["domain_record_id"] = rec_id
                    instance["public_host"] = full_d
                    instance["has_domain"] = True
                    return {
                        "success": True,
                        "type": "teamspeak",
                        "message": f"该 CDK 已激活。已成功为您的服务器补绑专属二级域名: {full_d}（免输入端口直连）！",
                        "instance": instance
                    }
                else:
                    return JSONResponse(status_code=400, content={"success": False, "message": f"域名补绑失败: {err_dns}"})

            instance["public_host"] = instance.get("subdomain") or client_host
            instance["has_domain"] = bool(instance.get("subdomain"))
            return {
                "success": True,
                "type": "teamspeak",
                "message": f"该 CDK 已于 {cdk_info['used_at']} 激活，已为您加载服务器连接信息",
                "instance": instance
            }
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已被激活使用，但绑定的 TeamSpeak 实例已不存在"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 状态异常或不可用"})

    dns_cfg = get_dns_config()
    dns_enabled = dns_cfg.get("dns_enabled", False)
    subdomain_input = (req.subdomain or "").strip()

    # 如果系统开启了 DNS 自动化绑定且用户填写了二级域名，先做严格校验与查重
    if dns_enabled and subdomain_input:
        avail, err_msg, full_domain = is_subdomain_available(subdomain_input)
        if not avail:
            return JSONResponse(status_code=400, content={"success": False, "message": f"二级域名不可用: {err_msg}"})

    claimed_cdk = claim_cdk(code, "teamspeak")
    if not claimed_cdk:
        return claim_error_response(code, "teamspeak")
    cdk_info = claimed_cdk

    # 未使用：开始分配端口与开通
    try:
        instance_id, ports = allocate_ports_for_instance()
    except Exception as e:
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": f"端口分配失败: {str(e)}"})

    name = f"ts{instance_id}"
    container_name = f"ts-teamspeak-{instance_id}"
    instance_dir = os.path.join(config.DATA_BASE_DIR, name)

    # 执行 Docker 部署流水线
    try:
        success, creds, msg = deploy_teamspeak_instance(instance_id, ports)
    except Exception as e:
        success, creds, msg = False, {}, f"部署过程异常: {e}"
    if not success:
        destroy_instance_container(instance_id, delete_files=True)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": f"服务器创建失败: {msg}"})

    live_status = get_container_status(instance_id)
    if live_status != "running":
        destroy_instance_container(instance_id, delete_files=True)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={
            "success": False,
            "message": f"服务器容器未处于运行状态（当前状态: {live_status}）"
        })

    admin_token = creds.get("admin_token", "")
    query_password = creds.get("query_password", "")
    query_apikey = creds.get("query_apikey", "")

    is_trial = cdk_info.get("is_trial", 0)
    duration_m = cdk_info.get("duration_months", 0)
    if duration_m > 0:
        expire_at = (datetime.now() + timedelta(days=30 * duration_m)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        expire_at = "permanent"

    # 执行 DNS 自动绑定 (SRV 记录)
    bound_subdomain = None
    domain_record_id = None
    dns_bind_msg = ""
    if dns_enabled and subdomain_input:
        ok_dns, rec_id, full_domain, err_dns = dns_service.create_ts_srv_record(
            subdomain_prefix=subdomain_input,
            target_host=client_host,
            voice_port=ports["voice"],
            dns_cfg=dns_cfg
        )
        if ok_dns:
            bound_subdomain = full_domain
            domain_record_id = rec_id
            dns_bind_msg = f"（已自动绑定二级域名: {bound_subdomain}，客户端直连无需输入端口）"
        else:
            dns_bind_msg = f"（DNS 自动绑定提示: {err_dns}）"

    # 记录到数据库；数据库失败时回收已经启动的容器和临时 CDK 占用。
    try:
        instance = create_instance(
            instance_id=instance_id,
            name=name,
            container_name=container_name,
            dir_path=instance_dir,
            voice_port=ports["voice"],
            file_port=ports["file"],
            query_port=ports["query"],
            tsdns_port=ports["tsdns"],
            admin_token=admin_token,
            query_password=query_password,
            query_apikey=query_apikey,
            cdk_code=code,
            duration_months=duration_m,
            expire_at=expire_at,
            status="running",
            subdomain=bound_subdomain,
            domain_record_id=domain_record_id
        )
        if not instance or not bind_cdk_instance(code, instance_id):
            raise RuntimeError("CDK 绑定失败")
    except Exception as e:
        if domain_record_id:
            try:
                ok_del, err_del = dns_service.delete_ts_srv_record(domain_record_id, dns_cfg=dns_cfg)
                if not ok_del:
                    print(f"[Warning] 回滚时删除 DNS 记录失败: {err_del}")
            except Exception as d_err:
                print(f"[Warning] 回滚时删除 DNS 记录异常: {d_err}")
        delete_instance(instance_id)
        destroy_instance_container(instance_id, delete_files=True)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": f"服务器记录失败: {str(e)}"})
    instance["public_host"] = bound_subdomain or client_host
    instance["has_domain"] = bool(bound_subdomain)
    instance["credentials_ready"] = bool(admin_token or query_password or query_apikey)

    # 如果是体验卡开通，记录该服务器地址
    if is_trial:
        try:
            record_trial_server(
                addr=bound_subdomain or client_host,
                port=ports["voice"],
                cdk_code=code,
                cdk_type="teamspeak",
                target_id=str(instance_id),
                raw_input=f"{bound_subdomain or client_host}:{ports['voice']}"
            )
        except Exception as e:
            if domain_record_id:
                try:
                    ok_del, err_del = dns_service.delete_ts_srv_record(domain_record_id, dns_cfg=dns_cfg)
                    if not ok_del:
                        print(f"[Warning] 体验卡回滚时删除 DNS 记录失败: {err_del}")
                except Exception as d_err:
                    print(f"[Warning] 体验卡回滚时删除 DNS 记录异常: {d_err}")
            delete_instance(instance_id)
            destroy_instance_container(instance_id, delete_files=True)
            unbind_cdk_instance(code, instance_id)
            release_cdk_claim(code)
            return JSONResponse(status_code=500, content={"success": False, "message": f"体验记录失败: {str(e)}"})

    # 针对新创建的实例端口进行本地防火墙即时放行
    try:
        open_single_instance_ports(ports["voice"], ports["file"], ports["query"], ports["tsdns"])
    except Exception:
        pass

    expire_desc = f"到期时间: {expire_at}" if expire_at != "permanent" else "永久有效"
    credential_desc = "凭据已提取" if instance["credentials_ready"] else "容器已启动，但首次凭据仍在日志中等待提取"
    return {
        "success": True,
        "type": "teamspeak",
        "message": f"恭喜！TeamSpeak 服务器 ({name}) 已成功开通并启动！{dns_bind_msg} ({expire_desc}；{credential_desc})",
        "instance": instance
    }

@app.post("/api/redeem-bot")
def redeem_bot_instance(req: RedeemBotRequest):
    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info.get("cdk_type") != "music_bot":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是音乐机器人兑换码"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

    try:
        _, raw_addr, target_port, _, _ = normalize_server_target(req.serverAddress, req.serverPort)
    except (TypeError, ValueError, OSError) as e:
        return JSONResponse(status_code=400, content={"success": False, "message": f"服务器地址或端口无效: {e}"})

    claimed_cdk = claim_cdk(code, "music_bot")
    if not claimed_cdk:
        return claim_error_response(code, "music_bot")
    cdk_info = claimed_cdk

    # 体验卡防刷检测（同一 IP 不同端口视为独立服务器）
    is_trial = cdk_info.get("is_trial", 0)
    trial_reserved = False
    if is_trial:
        trial_reserved, rec = reserve_trial_server(
            raw_addr, target_port, code, "music_bot", raw_input=req.serverAddress
        )
        if not trial_reserved:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={
                "success": False,
                "message": "该服务器已使用过体验卡，每个服务器只能使用一次体验卡，请联系退款"
            })

    # 校验用户输入的后台账号密码（若提供）
    web_username = (req.webUsername or "").strip()
    web_password = (req.webPassword or "").strip()
    if web_username or web_password:
        if not web_username or len(web_username) < 3 or len(web_username) > 32:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={"success": False, "message": "后台账号用户名长度必须为 3 到 32 个字符"})
        if not re.match(r"^[a-zA-Z0-9_\-\.@]+$", web_username):
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={"success": False, "message": "用户名包含非法字符，仅支持字母、数字、下划线、短横线与点"})
        if not web_password or len(web_password) < 8:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={"success": False, "message": "后台账号密码长度不能少于 8 位"})

    # 调用远程音乐机器人 API 创建实例
    try:
        ok, res = music_bot_client.create_bot(
            name=req.name.strip() or "我的音乐机器人",
            server_address=raw_addr,
            server_port=target_port,
            nickname=req.nickname.strip() or "MusicBot",
            default_channel=req.defaultChannel.strip() if req.defaultChannel else None,
            server_password=req.serverPassword if req.serverPassword else None,
            auto_start=True
        )
    except Exception as e:
        ok, res = False, f"远程音乐机器人接口异常: {e}"

    if not ok or not isinstance(res, dict) or "id" not in res:
        if isinstance(res, dict) and res.get("id"):
            music_bot_client.delete_bot(str(res["id"]))
        if trial_reserved:
            release_trial_reservation(raw_addr, target_port, code)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": f"音乐机器人创建失败: {res}"})

    bot_id = res["id"]
    duration_m = cdk_info.get("duration_months", 1)
    if duration_m > 0:
        expire_at = (datetime.now() + timedelta(days=30 * duration_m)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        expire_at = "permanent"

    # 如果填写了 Web 账号密码，在机器人后台创建该用户并按系统后台配置分配权限
    created_web_user_id = None
    perm_cfg = get_bot_permission_config()
    configured_role = perm_cfg.get("role", "member")
    configured_caps = perm_cfg.get("capabilities", ["player.control", "player.queue"])
    configured_scope = perm_cfg.get("bot_scope", "current")
    target_bots = "all" if configured_scope == "all" else [str(bot_id)]

    if web_username and web_password:
        try:
            ok_u, res_u = music_bot_client.create_user(web_username, web_password, role=configured_role)
            if not ok_u:
                err_msg = str(res_u)
                if "already" in err_msg.lower() or "409" in err_msg or "exists" in err_msg.lower() or "HTTP 400" in err_msg:
                    friendly_err = f"Web 点歌用户名【{web_username}】可能已存在或不合规，请更换其他用户名重试"
                else:
                    friendly_err = f"Web 点歌账号创建失败: {res_u}"
                # 回滚已创建的机器人
                music_bot_client.delete_bot(str(bot_id))
                if trial_reserved:
                    release_trial_reservation(raw_addr, target_port, code)
                release_cdk_claim(code)
                return JSONResponse(status_code=400, content={"success": False, "message": friendly_err})
            
            created_web_user_id = res_u.get("id") if isinstance(res_u, dict) else None
            
            # 分配管理员在后台配置的能力权限 (包含机器人管理权限、播放控制等)，按授权范围绑定
            if created_web_user_id:
                ok_p, res_p = music_bot_client.set_user_permissions(
                    user_id=str(created_web_user_id),
                    capabilities=configured_caps,
                    bots=target_bots
                )
                if not ok_p:
                    music_bot_client.delete_user(str(created_web_user_id))
                    music_bot_client.delete_bot(str(bot_id))
                    if trial_reserved:
                        release_trial_reservation(raw_addr, target_port, code)
                    release_cdk_claim(code)
                    return JSONResponse(status_code=500, content={"success": False, "message": f"Web 用户权限配置失败: {res_p}"})
        except Exception as err_u:
            if created_web_user_id:
                try:
                    music_bot_client.delete_user(str(created_web_user_id))
                except Exception:
                    pass
            music_bot_client.delete_bot(str(bot_id))
            if trial_reserved:
                release_trial_reservation(raw_addr, target_port, code)
            release_cdk_claim(code)
            return JSONResponse(status_code=500, content={"success": False, "message": f"创建 Web 用户及权限配置异常: {err_u}"})

    # 保存本地数据库；失败时回滚远程机器人和 CDK 占用。
    try:
        bot_inst = create_bot_instance(
            bot_id=bot_id,
            name=req.name.strip() or "我的音乐机器人",
            server_address=raw_addr,
            server_port=target_port,
            nickname=req.nickname.strip() or "MusicBot",
            cdk_code=code,
            duration_months=duration_m,
            expire_at=expire_at,
            default_channel=req.defaultChannel.strip() if req.defaultChannel else None,
            status="active",
            web_username=web_username or None,
            web_password=web_password or None,
            web_user_id=str(created_web_user_id) if created_web_user_id else None
        )
        if not bot_inst or not bind_cdk_bot(code, bot_id):
            raise RuntimeError("CDK 绑定失败")
    except Exception as e:
        if created_web_user_id:
            try:
                music_bot_client.delete_user(str(created_web_user_id))
            except Exception:
                pass
        delete_bot_instance(bot_id)
        music_bot_client.delete_bot(bot_id)
        if trial_reserved:
            release_trial_reservation(raw_addr, target_port, code)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": f"音乐机器人本地记录失败: {e}"})

    # 体验卡记录指纹到本地数据库
    if is_trial:
        try:
            record_trial_server(
                addr=raw_addr,
                port=target_port,
                cdk_code=code,
                cdk_type="music_bot",
                target_id=bot_id,
                raw_input=req.serverAddress
            )
        except Exception as e:
            if created_web_user_id:
                try:
                    music_bot_client.delete_user(str(created_web_user_id))
                except Exception:
                    pass
            delete_bot_instance(bot_id)
            music_bot_client.delete_bot(bot_id)
            unbind_cdk_bot(code, bot_id)
            if trial_reserved:
                release_trial_reservation(raw_addr, target_port, code)
            return JSONResponse(status_code=500, content={"success": False, "message": f"体验记录失败: {e}"})

    return {
        "success": True,
        "type": "music_bot",
        "message": f"🎉 音乐机器人已成功创建并对接！请在 TS 客户端右键机器人赋予【服务器管理员】权限。到期时间: {expire_at}",
        "instance": bot_inst,
        "bot_panel_url": get_bot_config()["bot_panel_url"],
        "bot_tutorial_url": get_bot_config().get("bot_tutorial_url", "http://103.71.69.156:23452/"),
        "permission_notice": perm_cfg.get("permission_notice", "月卡用户仅有控制功能，年卡用户独享音乐后台")
    }

@app.post("/api/bot-instances/{bot_id}/action")
def user_bot_action(bot_id: str, req: BotActionRequest):
    action = req.action.lower()
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="未找到该机器人实例")

    access_cdk = (req.cdk or "").strip().upper()
    cdk_info = get_cdk(access_cdk) if access_cdk else None
    if (
        not cdk_info
        or cdk_info.get("status") != "used"
        or cdk_info.get("cdk_type") != "music_bot"
        or cdk_info.get("bot_id") != bot_id
    ):
        raise HTTPException(status_code=403, detail="缺少有效的机器人访问凭据")

    # 到期安全校验：若已超时，禁止非管理员启动
    if action in ("start", "restart"):
        if bot.get("expire_at") and bot["expire_at"] != "permanent":
            try:
                exp_dt = datetime.strptime(bot["expire_at"], "%Y-%m-%d %H:%M:%S")
                if exp_dt < datetime.now():
                    update_bot_instance_status(bot_id, "expired")
                    return JSONResponse(status_code=403, content={
                        "success": False,
                        "message": f"该音乐机器人已于 {bot['expire_at']} 到期并已自动停止。请使用新的 CDK 进行续费！"
                    })
            except Exception:
                pass

    if action == "start":
        ok, res = music_bot_client.start_bot(bot_id)
        if ok:
            update_bot_instance_status(bot_id, "active")
            return {"success": True, "message": "机器人已启动并尝试连接语音服务器"}
        return JSONResponse(status_code=500, content={"success": False, "message": f"启动失败: {res}"})

    elif action == "stop":
        ok, res = music_bot_client.stop_bot(bot_id)
        if ok:
            update_bot_instance_status(bot_id, "stopped")
            return {"success": True, "message": "机器人已停止"}
        return JSONResponse(status_code=500, content={"success": False, "message": f"停止失败: {res}"})

    elif action == "restart":
        ok, res = music_bot_client.restart_bot(bot_id)
        if ok:
            update_bot_instance_status(bot_id, "active")
            return {"success": True, "message": "机器人已重启"}
        return JSONResponse(status_code=500, content={"success": False, "message": f"重启失败: {res}"})

    else:
        raise HTTPException(status_code=400, detail="不支持的操作指令")

@app.post("/api/renew-bot")
def renew_bot_endpoint(req: RenewBotRequest):
    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

    if cdk_info.get("cdk_type") != "music_bot":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是音乐机器人兑换码"})

    bot = get_bot_instance_by_id(req.bot_id)
    if not bot:
        return JSONResponse(status_code=404, content={"success": False, "message": "未找到要续费的机器人实例"})

    claimed_cdk = claim_cdk(code, "music_bot")
    if not claimed_cdk:
        return claim_error_response(code, "music_bot")
    cdk_info = claimed_cdk

    # 体验卡续费检测
    is_trial = cdk_info.get("is_trial", 0)
    trial_reserved = False
    if is_trial:
        trial_reserved, rec = reserve_trial_server(
            bot["server_address"], bot["server_port"], code, "music_bot",
            raw_input=f"{bot['server_address']}:{bot['server_port']}"
        )
        if not trial_reserved:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={
                "success": False,
                "message": "该服务器已使用过体验卡，每个服务器只能使用一次体验卡，请联系退款"
            })

    add_m = 1 if is_trial else cdk_info.get("duration_months", 1)
    renewed_bot = renew_bot_instance(req.bot_id, add_m)
    if not renewed_bot:
        if trial_reserved:
            release_trial_reservation(bot["server_address"], bot["server_port"], code)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": "机器人续费失败"})
    if not bind_cdk_bot(code, req.bot_id):
        update_bot_instance_expiry(req.bot_id, bot["expire_at"])
        if trial_reserved:
            release_trial_reservation(bot["server_address"], bot["server_port"], code)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": "续费卡绑定失败"})

    # 若为体验卡，记录入库
    if is_trial:
        try:
            record_trial_server(
                addr=bot["server_address"],
                port=bot["server_port"],
                cdk_code=code,
                cdk_type="music_bot",
                target_id=req.bot_id,
                raw_input=f"{bot['server_address']}:{bot['server_port']}"
            )
        except Exception as e:
            update_bot_instance_expiry(req.bot_id, bot["expire_at"])
            unbind_cdk_bot(code, req.bot_id)
            if trial_reserved:
                release_trial_reservation(bot["server_address"], bot["server_port"], code)
            return JSONResponse(status_code=500, content={"success": False, "message": f"体验记录失败: {e}"})

    # 尝试重新拉起机器人
    start_ok, start_res = music_bot_client.start_bot(req.bot_id)
    if start_ok:
        update_bot_instance_status(req.bot_id, "active")
        renewed_bot["status"] = "active"
        start_desc = "机器人已自动恢复运行"
    else:
        update_bot_instance_status(req.bot_id, "stopped")
        renewed_bot["status"] = "stopped"
        start_desc = f"续费成功，但自动启动失败: {start_res}"

    return {
        "success": True,
        "type": "music_bot",
        "message": f"🎉 续费成功！机器人有效期已顺延至: {renewed_bot['expire_at']}；{start_desc}",
        "instance": renewed_bot,
        "bot_panel_url": get_bot_config()["bot_panel_url"]
    }

@app.post("/api/renew-instance")
def renew_instance_endpoint(req: RenewInstanceRequest, request: Request):
    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

    if cdk_info.get("cdk_type") != "teamspeak":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是 TeamSpeak 服务器兑换码"})

    instance = get_instance_by_id(req.instance_id)
    if not instance:
        return JSONResponse(status_code=404, content={"success": False, "message": "未找到要续费的 TeamSpeak 实例"})

    claimed_cdk = claim_cdk(code, "teamspeak")
    if not claimed_cdk:
        return claim_error_response(code, "teamspeak")
    cdk_info = claimed_cdk

    client_host = get_public_host(request)

    # 体验卡续费检测
    is_trial = cdk_info.get("is_trial", 0)
    trial_reserved = False
    if is_trial:
        trial_reserved, rec = reserve_trial_server(
            client_host, instance["voice_port"], code, "teamspeak",
            raw_input=f"{client_host}:{instance['voice_port']}"
        )
        if not trial_reserved:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={
                "success": False,
                "message": "该服务器已使用过体验卡，每个服务器只能使用一次体验卡，请联系退款"
            })

    add_m = 1 if is_trial else cdk_info.get("duration_months", 0)
    renewed_inst = renew_instance(req.instance_id, add_m)
    if not renewed_inst:
        if trial_reserved:
            release_trial_reservation(client_host, instance["voice_port"], code)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": "TeamSpeak 实例续费失败"})
    if not bind_cdk_instance(code, req.instance_id):
        update_instance_expiry(req.instance_id, instance["expire_at"])
        if trial_reserved:
            release_trial_reservation(client_host, instance["voice_port"], code)
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": "续费卡绑定失败"})

    if is_trial:
        try:
            record_trial_server(
                addr=client_host,
                port=instance["voice_port"],
                cdk_code=code,
                cdk_type="teamspeak",
                target_id=str(req.instance_id),
                raw_input=f"{client_host}:{instance['voice_port']}"
            )
        except Exception as e:
            update_instance_expiry(req.instance_id, instance["expire_at"])
            unbind_cdk_instance(code, req.instance_id)
            if trial_reserved:
                release_trial_reservation(client_host, instance["voice_port"], code)
            return JSONResponse(status_code=500, content={"success": False, "message": f"体验记录失败: {e}"})

    # 尝试重新拉起/启动 TS 容器
    start_ok = start_instance_container(req.instance_id)
    if start_ok:
        update_instance_status(req.instance_id, "running")
        renewed_inst["status"] = "running"
        start_desc = "实例已自动恢复运行"
    else:
        update_instance_status(req.instance_id, "stopped")
        renewed_inst["status"] = "stopped"
        start_desc = "续费成功，但实例自动启动失败"

    renewed_inst["public_host"] = client_host

    return {
        "success": True,
        "type": "teamspeak",
        "message": f"🎉 续费成功！TeamSpeak 服务器有效期已顺延至: {renewed_inst['expire_at']}；{start_desc}",
        "instance": renewed_inst
    }

# --- 管理员 API ---

@app.get("/api/admin/system-status")
def get_system_status(_: bool = Depends(verify_admin)):
    cdks = get_all_cdks()
    instances = get_all_instances()
    bot_instances = get_all_bot_instances()
    used_ports = get_all_used_ports()

    voice_ports = used_ports["voice"]
    port_range_str = f"{min(voice_ports)} ~ {max(voice_ports)}" if voice_ports else "暂无"

    return {
        "success": True,
        "total_instances": len(instances),
        "total_bots": len(bot_instances),
        "total_cdks": len(cdks),
        "unused_cdks": len([c for c in cdks if c["status"] == "unused"]),
        "voice_ports_summary": port_range_str,
        "data_base_dir": config.DATA_BASE_DIR,
        "bot_panel_url": get_bot_config()["bot_panel_url"]
    }

@app.get("/api/admin/instances")
def list_instances(_: bool = Depends(verify_admin)):
    instances = get_all_instances()
    now_dt = datetime.now()
    # 动态探测 Docker 实际状态与到期天数
    for inst in instances:
        inst["live_status"] = get_container_status(inst["id"])
        if inst.get("expire_at") and inst["expire_at"] != "permanent":
            try:
                exp_dt = datetime.strptime(inst["expire_at"], "%Y-%m-%d %H:%M:%S")
                delta = exp_dt - now_dt
                inst["days_left"] = max(0, delta.days)
                inst["is_expired"] = delta.total_seconds() < 0
            except Exception:
                inst["days_left"] = 0
                inst["is_expired"] = False
        else:
            inst["days_left"] = "永久"
            inst["is_expired"] = False
    return {"success": True, "instances": instances}

@app.get("/api/admin/bots")
def list_admin_bots(_: bool = Depends(verify_admin)):
    bots = get_all_bot_instances()
    # 动态探测远程平台实际状态
    ok, remote_bots = music_bot_client.get_all_bots()
    remote_map = {}
    if ok and isinstance(remote_bots, dict) and "bots" in remote_bots:
        for b in remote_bots["bots"]:
            remote_map[b["id"]] = b

    all_cdk_codes = {c["code"] for c in get_all_cdks()}
    now_dt = datetime.now()
    for bot in bots:
        r_info = remote_map.get(bot["bot_id"])
        bot["remote_info"] = r_info
        bot["connected"] = r_info.get("connected", False) if r_info else False
        bot["playing"] = r_info.get("playing", False) if r_info else False
        bot["cdk_exists"] = (bot.get("cdk_code") in all_cdk_codes) if bot.get("cdk_code") else False
        
        # 计算剩余有效天数
        if bot["expire_at"] and bot["expire_at"] != "permanent":
            try:
                exp_dt = datetime.strptime(bot["expire_at"], "%Y-%m-%d %H:%M:%S")
                delta = exp_dt - now_dt
                bot["days_left"] = max(0, delta.days)
                bot["is_expired"] = delta.total_seconds() < 0
            except Exception:
                bot["days_left"] = 0
                bot["is_expired"] = False
        else:
            bot["days_left"] = "永久"
            bot["is_expired"] = False

    return {"success": True, "bots": bots, "bot_panel_url": get_bot_config()["bot_panel_url"]}

@app.post("/api/admin/bots/{bot_id}/action")
def manage_admin_bot(bot_id: str, req: BotActionRequest, _: bool = Depends(verify_admin)):
    action = req.action.lower()
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="未找到该机器人实例")

    if action == "start":
        ok, res = music_bot_client.start_bot(bot_id)
        if ok:
            update_bot_instance_status(bot_id, "active")
            return {"success": True, "message": f"机器人 {bot['name']} 已启动"}
        return JSONResponse(status_code=500, content={"success": False, "message": f"启动失败: {res}"})

    elif action == "stop":
        ok, res = music_bot_client.stop_bot(bot_id)
        if ok:
            update_bot_instance_status(bot_id, "stopped")
            return {"success": True, "message": f"机器人 {bot['name']} 已停止"}
        return JSONResponse(status_code=500, content={"success": False, "message": f"停止失败: {res}"})

    elif action == "restart":
        ok, res = music_bot_client.restart_bot(bot_id)
        if ok:
            update_bot_instance_status(bot_id, "active")
            return {"success": True, "message": f"机器人 {bot['name']} 已重启"}
        return JSONResponse(status_code=500, content={"success": False, "message": f"重启失败: {res}"})

    elif action == "delete":
        ok, res = music_bot_client.delete_bot(bot_id)
        if not ok:
            return JSONResponse(status_code=500, content={"success": False, "message": f"远程机器人删除失败: {res}"})
        delete_bot_instance(bot_id)
        return {"success": True, "message": f"机器人 {bot['name']} 已删除"}

    else:
        raise HTTPException(status_code=400, detail="不支持的操作指令")

@app.post("/api/admin/bots/{bot_id}/renew")
def admin_renew_bot_api(bot_id: str, req: AdminRenewBotRequest, _: bool = Depends(verify_admin)):
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        return JSONResponse(status_code=404, content={"success": False, "message": "未找到要续费的机器人实例"})

    if req.cdk and req.cdk.strip():
        code = req.cdk.strip().upper()
        cdk_info = get_cdk(code)
        if not cdk_info:
            return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})
        if cdk_info["status"] != "unused":
            return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})
        if cdk_info.get("cdk_type") != "music_bot":
            return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是音乐机器人兑换码"})
        claimed = claim_cdk(code, "music_bot")
        if not claimed:
            return claim_error_response(code, "music_bot")
        add_m = 1 if claimed.get("is_trial", 0) else claimed.get("duration_months", 1)
        renewed_bot = renew_bot_instance(bot_id, add_m)
        if not renewed_bot:
            release_cdk_claim(code)
            return JSONResponse(status_code=500, content={"success": False, "message": "续费失败"})
        bind_cdk_bot(code, bot_id)
    else:
        add_m = req.duration_months if req.duration_months is not None else 1
        renewed_bot = renew_bot_instance(bot_id, add_m)
        if not renewed_bot:
            return JSONResponse(status_code=500, content={"success": False, "message": "续费失败"})

    start_ok, _ = music_bot_client.start_bot(bot_id)
    if start_ok:
        update_bot_instance_status(bot_id, "active")
        renewed_bot["status"] = "active"

    duration_desc = "永久" if renewed_bot["expire_at"] == "permanent" else f"顺延至: {renewed_bot['expire_at']}"
    return {
        "success": True,
        "message": f"🎉 机器人续费成功！有效期已{duration_desc}",
        "bot": renewed_bot
    }

@app.post("/api/admin/bots/{bot_id}/restore-cdk")
def admin_restore_bot_cdk_api(bot_id: str, _: bool = Depends(verify_admin)):
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        return JSONResponse(status_code=404, content={"success": False, "message": "机器人实例不存在"})
    cdk_code = (bot.get("cdk_code") or "").strip()
    if not cdk_code:
        return JSONResponse(status_code=400, content={"success": False, "message": "该机器人无绑定的 CDK 编码"})
    
    cdk_info = restore_bot_cdk(cdk_code, bot_id, bot.get("duration_months", 1), remark="管理员后台一键补全恢复")
    return {
        "success": True,
        "message": f"🎉 CDK【{cdk_code}】记录已成功恢复至卡密数据库！",
        "cdk": cdk_info
    }

@app.post("/api/admin/instances/{instance_id}/action")
def manage_instance(instance_id: int, req: InstanceActionRequest, _: bool = Depends(verify_admin)):
    action = req.action.lower()
    instance = get_instance_by_id(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="未找到该实例")

    if action == "start":
        ok = start_instance_container(instance_id)
        if ok:
            update_instance_status(instance_id, "running")
            return {"success": True, "message": f"实例 ts{instance_id} 已启动"}
        return JSONResponse(status_code=500, content={"success": False, "message": "启动失败"})

    elif action == "stop":
        ok = stop_instance_container(instance_id)
        if ok:
            update_instance_status(instance_id, "stopped")
            return {"success": True, "message": f"实例 ts{instance_id} 已停止"}
        return JSONResponse(status_code=500, content={"success": False, "message": "停止失败"})

    elif action == "restart":
        ok = restart_instance_container(instance_id)
        if ok:
            update_instance_status(instance_id, "running")
            return {"success": True, "message": f"实例 ts{instance_id} 已重启"}
        return JSONResponse(status_code=500, content={"success": False, "message": "重启失败"})

    elif action == "destroy":
        if instance.get("domain_record_id"):
            try:
                ok_del, err_del = dns_service.delete_ts_srv_record(instance["domain_record_id"])
                if not ok_del:
                    print(f"[Warning] 销毁实例 ts{instance_id} 时删除 DNS 记录失败: {err_del}")
            except Exception as e:
                print(f"[Warning] 销毁实例 ts{instance_id} 时删除 DNS 记录异常: {e}")
        ok = destroy_instance_container(instance_id, delete_files=True)
        if not ok:
            return JSONResponse(status_code=500, content={
                "success": False,
                "message": f"实例 ts{instance_id} 清理失败，数据库记录已保留，请检查 Docker 和数据目录"
            })
        delete_instance(instance_id)
        return {"success": True, "message": f"实例 ts{instance_id} 及其存储目录已彻底销毁"}

    else:
        raise HTTPException(status_code=400, detail="不支持的操作指令")

@app.post("/api/admin/instances/{instance_id}/bind-domain")
def admin_bind_instance_domain_api(instance_id: int, req: BindInstanceDomainRequest, request: Request, _: bool = Depends(verify_admin)):
    inst = get_instance_by_id(instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="未找到该实例")

    dns_cfg = get_dns_config()
    if not dns_cfg.get("dns_enabled", False):
        return JSONResponse(status_code=400, content={"success": False, "message": "系统尚未开启 DNS 自动化绑定功能，请先在【域名与 DNS 自动绑定】页面启用并保存配置"})

    subdomain_prefix = req.subdomain_prefix.strip()
    avail, err_msg, full_domain = is_subdomain_available(subdomain_prefix)
    if not avail:
        return JSONResponse(status_code=400, content={"success": False, "message": f"二级域名不可用: {err_msg}"})

    # 如果原先已经有绑定记录，先尝试删除旧记录
    old_record_id = inst.get("domain_record_id")
    if old_record_id:
        try:
            dns_service.delete_ts_srv_record(old_record_id, dns_cfg=dns_cfg)
        except Exception:
            pass

    client_host = get_public_host(request)
    target_host = dns_cfg.get("dns_target_host") or client_host
    ok_dns, rec_id, full_d, err_dns = dns_service.create_ts_srv_record(
        subdomain_prefix=subdomain_prefix,
        target_host=target_host,
        voice_port=inst["voice_port"],
        dns_cfg=dns_cfg
    )
    if not ok_dns:
        return JSONResponse(status_code=400, content={"success": False, "message": f"DNS 绑定失败: {err_dns}"})

    update_instance_domain(instance_id, full_d, rec_id)
    return {
        "success": True,
        "message": f"成功为实例 ts{instance_id} 绑定专属二级域名: {full_d}！",
        "subdomain": full_d,
        "domain_record_id": rec_id
    }

@app.post("/api/admin/instances/{instance_id}/unbind-domain")
def admin_unbind_instance_domain_api(instance_id: int, _: bool = Depends(verify_admin)):
    inst = get_instance_by_id(instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="未找到该实例")

    old_record_id = inst.get("domain_record_id")
    if old_record_id:
        try:
            dns_cfg = get_dns_config()
            dns_service.delete_ts_srv_record(old_record_id, dns_cfg=dns_cfg)
        except Exception as e:
            print(f"[Warning] 解绑域名时删除 DNS 记录异常: {e}")

    update_instance_domain(instance_id, None, None)
    return {"success": True, "message": f"实例 ts{instance_id} 已成功解绑二级域名，恢复为 IP 直连"}

@app.post("/api/admin/instances/batch-action")
def batch_manage_instances_api(req: BatchActionInstancesRequest, _: bool = Depends(verify_admin)):
    action = req.action.lower()
    success_count = 0
    for instance_id in req.ids:
        if action == "start":
            if start_instance_container(instance_id):
                update_instance_status(instance_id, "running")
                success_count += 1
        elif action == "stop":
            if stop_instance_container(instance_id):
                update_instance_status(instance_id, "stopped")
                success_count += 1
        elif action == "restart":
            if restart_instance_container(instance_id):
                update_instance_status(instance_id, "running")
                success_count += 1
        elif action == "destroy":
            inst = get_instance_by_id(instance_id)
            if inst and inst.get("domain_record_id"):
                try:
                    ok_del, err_del = dns_service.delete_ts_srv_record(inst["domain_record_id"])
                    if not ok_del:
                        print(f"[Warning] 批量销毁实例 ts{instance_id} 时删除 DNS 记录失败: {err_del}")
                except Exception as d_err:
                    print(f"[Warning] 批量销毁实例 ts{instance_id} 时删除 DNS 记录异常: {d_err}")
            if destroy_instance_container(instance_id, delete_files=True) and delete_instance(instance_id):
                success_count += 1
    return {"success": True, "count": success_count, "message": f"已成功对 {success_count} 个 TS 实例执行【{action}】操作"}

@app.post("/api/admin/bots/batch-action")
def batch_manage_bots_api(req: BatchActionBotsRequest, _: bool = Depends(verify_admin)):
    action = req.action.lower()
    success_count = 0
    for bot_id in req.bot_ids:
        if action == "start":
            ok, _ = music_bot_client.start_bot(bot_id)
            if ok:
                update_bot_instance_status(bot_id, "active")
                success_count += 1
        elif action == "stop":
            ok, _ = music_bot_client.stop_bot(bot_id)
            if ok:
                update_bot_instance_status(bot_id, "stopped")
                success_count += 1
        elif action == "restart":
            ok, _ = music_bot_client.restart_bot(bot_id)
            if ok:
                update_bot_instance_status(bot_id, "active")
                success_count += 1
        elif action == "delete":
            ok, _ = music_bot_client.delete_bot(bot_id)
            if ok and delete_bot_instance(bot_id):
                success_count += 1
    return {"success": True, "count": success_count, "message": f"已成功对 {success_count} 个音乐机器人执行【{action}】操作"}

@app.get("/api/admin/instances/{instance_id}/logs")
def get_instance_logs_api(instance_id: int, _: bool = Depends(verify_admin)):
    logs = fetch_container_logs(instance_id)
    return {"success": True, "logs": logs}

@app.get("/api/admin/cdks")
def list_cdks(_: bool = Depends(verify_admin)):
    return {"success": True, "cdks": get_all_cdks()}

@app.post("/api/admin/cdks/generate")
def generate_cdks_api(req: GenerateCdksRequest, _: bool = Depends(verify_admin)):
    if req.count < 1 or req.count > 200:
        raise HTTPException(status_code=400, detail="生成数量必须在 1 到 200 之间")
    created = create_cdks(
        count=req.count,
        remark=req.remark or "",
        cdk_type=req.cdk_type or "teamspeak",
        duration_months=req.duration_months or 0,
        is_trial=req.is_trial or 0
    )
    return {"success": True, "created": created}

@app.delete("/api/admin/cdks/{code}")
def delete_cdk_api(code: str, _: bool = Depends(verify_admin)):
    ok = delete_cdk(code)
    return {"success": ok}

@app.post("/api/admin/cdks/batch-delete")
def batch_delete_cdks_api(req: BatchDeleteCdksRequest, _: bool = Depends(verify_admin)):
    if req.codes is not None:
        count = delete_cdks(req.codes)
        return {"success": True, "deleted_count": count, "message": f"已成功删除选中的 {count} 个 CDK"}
    elif req.filter is not None:
        count = delete_cdks_by_filter(
            cdk_type=req.filter.cdk_type,
            duration_months=req.filter.duration_months,
            is_trial=req.filter.is_trial,
            status=req.filter.status
        )
        return {"success": True, "deleted_count": count, "message": f"已成功按条件批量删除 {count} 个 CDK"}
    else:
        raise HTTPException(status_code=400, detail="缺少删除参数或筛选条件")

@app.get("/api/admin/trial-records")
def list_trial_records_api(_: bool = Depends(verify_admin)):
    records = get_all_trial_records()
    return {"success": True, "records": records}

@app.delete("/api/admin/trial-records/{record_id}")
def delete_trial_record_api(record_id: int, _: bool = Depends(verify_admin)):
    ok = delete_trial_record(record_id)
    if ok:
        return {"success": True, "message": "已成功清除该服务器的体验记录，体验资格已重置！"}
    else:
        return JSONResponse(status_code=404, content={"success": False, "message": "未找到该条体验记录"})

@app.get("/api/admin/cdks/export")
def export_cdks_txt(status: Optional[str] = "unused", _: bool = Depends(verify_admin)):
    from fastapi.responses import PlainTextResponse
    cdks = get_all_cdks()
    if status == "unused":
        selected = [c["code"] for c in cdks if c["status"] == "unused"]
        filename = "unused_cdks.txt"
    elif status == "used":
        selected = [
            f"{c['code']}\t类型: {'音乐机器人' if c.get('cdk_type') == 'music_bot' else 'TS服务器'}\t绑定: {c.get('bot_id') or (('ts' + str(c.get('instance_id'))) if c.get('instance_id') else '-')}"
            for c in cdks if c["status"] == "used"
        ]
        filename = "used_cdks.txt"
    else:
        selected = []
        for c in cdks:
            is_trial = c.get("is_trial") == 1
            dur_str = "体验卡(1个月)" if is_trial else (f"{c.get('duration_months')}个月" if c.get("duration_months") else "永久")
            type_str = "音乐机器人" if c.get("cdk_type") == "music_bot" else "TS服务器"
            bound_str = c.get("bot_id") or (f"ts{c.get('instance_id')}" if c.get("instance_id") else "-")
            selected.append(f"{c['code']}\t类型: {type_str}\t时长: {dur_str}\t状态: {c['status']}\t绑定: {bound_str}\t备注: {c.get('remark') or '无'}")
        filename = "all_cdks.txt"

    content = "\n".join(selected)
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.post("/api/admin/change-password")
def change_admin_password_api(req: ChangePasswordRequest, _: bool = Depends(verify_admin)):
    old_pwd = req.old_password.strip()
    new_pwd = req.new_password.strip()
    current_pwd = get_admin_password()

    if old_pwd != current_pwd:
        return JSONResponse(status_code=400, content={"success": False, "message": "原密码不正确，请重新输入"})

    if len(new_pwd) < 6:
        return JSONResponse(status_code=400, content={"success": False, "message": "新密码长度不能少于 6 位"})

    set_admin_password(new_pwd)
    return {"success": True, "message": "管理员密码修改成功！请使用新密码重新登录"}

@app.get("/api/admin/bot-config")
def get_bot_config_api(_: bool = Depends(verify_admin)):
    cfg = get_bot_config()
    return {
        "success": True,
        "config": {
            "url": cfg["bot_panel_url"],
            "user": cfg["bot_panel_user"],
            "password": cfg["bot_panel_pass"],
            "tutorial_url": cfg.get("bot_tutorial_url", "http://103.71.69.156:23452/")
        }
    }

@app.post("/api/admin/bot-config")
def update_bot_config_api(req: BotConfigRequest, _: bool = Depends(verify_admin)):
    url = req.url.strip()
    user = req.user.strip()
    password = req.password.strip()
    tutorial_url = req.tutorial_url.strip() if req.tutorial_url else None

    if not url:
        raise HTTPException(status_code=400, detail="机器人网站地址 (URL) 不能为空")
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="网站地址格式不正确，必须以 http:// 或 https:// 开头")
    if tutorial_url and not (tutorial_url.startswith("http://") or tutorial_url.startswith("https://")):
        raise HTTPException(status_code=400, detail="使用教程跳转网址格式不正确，必须以 http:// 或 https:// 开头")
    if not user:
        raise HTTPException(status_code=400, detail="管理员登录账号不能为空")
    if not password:
        raise HTTPException(status_code=400, detail="管理员登录密码不能为空")

    saved_cfg = set_bot_config(url, user, password, tutorial_url=tutorial_url)
    music_bot_client.update_config(saved_cfg["bot_panel_url"], saved_cfg["bot_panel_user"], saved_cfg["bot_panel_pass"])

    return {
        "success": True,
        "message": "音乐机器人平台对接配置已成功保存并即时生效！",
        "config": {
            "url": saved_cfg["bot_panel_url"],
            "user": saved_cfg["bot_panel_user"],
            "password": saved_cfg["bot_panel_pass"],
            "tutorial_url": saved_cfg.get("bot_tutorial_url", "http://103.71.69.156:23452/")
        }
    }

@app.post("/api/admin/bot-config/test")
def test_bot_config_api(req: Optional[TestBotConfigRequest] = None, _: bool = Depends(verify_admin)):
    url = req.url if req else None
    user = req.user if req else None
    password = req.password if req else None

    ok, msg, data = music_bot_client.test_connection(url, user, password)
    if ok:
        return {
            "success": True,
            "message": msg,
            "data": data
        }
    else:
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": msg,
            "data": data
        })

# --- 音乐机器人用户权限与后台同步 API ---

@app.get("/api/admin/bot-permissions")
def get_bot_permissions_admin_api(_: bool = Depends(verify_admin)):
    cfg = get_bot_permission_config()
    standard_capabilities = [
        {"token": "player.control", "key": "player.control", "label": "播放控制", "name": "播放控制", "desc": "允许暂停、继续、切歌及调节音量等"},
        {"token": "player.queue", "key": "player.queue", "label": "队列管理", "name": "队列管理", "desc": "允许提交点歌、清空队列与排队调整"},
        {"token": "bot.manage", "key": "bot.manage", "label": "机器人管理", "name": "机器人管理", "desc": "允许启停机器人、修改配置与频道（机器人管理权限）"},
        {"token": "bot.create", "key": "bot.create", "label": "创建新实例", "name": "创建新实例", "desc": "允许在机器人后台创建新的机器人"},
        {"token": "platform.auth", "key": "platform.auth", "label": "平台登录凭据", "name": "平台登录凭据", "desc": "允许配置网易云/QQ音乐/B站登录凭据"},
        {"token": "quality", "key": "quality", "label": "音质设置", "name": "音质设置", "desc": "允许调整音质比特率与采样率参数"}
    ]
    return {
        "success": True,
        "config": cfg,
        "standard_capabilities": standard_capabilities
    }

@app.post("/api/admin/bot-permissions")
def update_bot_permissions_admin_api(req: BotPermissionConfigRequest, _: bool = Depends(verify_admin)):
    saved = set_bot_permission_config(
        role=req.role,
        capabilities=req.capabilities,
        bot_scope=req.bot_scope,
        permission_notice=req.permission_notice
    )
    return {
        "success": True,
        "message": "机器人用户权限配置已成功保存并即时生效！后续新建用户将自动分配此权限",
        "config": saved
    }

@app.post("/api/admin/bot-permissions/sync")
def sync_bot_permissions_admin_api(_: bool = Depends(verify_admin)):
    ok, result = music_bot_client.sync_bot_permissions()
    return {
        "success": ok,
        "data": result
    }

@app.post("/api/admin/bot-instances/{bot_id}/sync-permission")
def sync_single_bot_instance_permission(bot_id: str, _: bool = Depends(verify_admin)):
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="未找到该机器人实例")
    web_user_id = bot.get("web_user_id")
    if not web_user_id:
        # 尝试通过绑定的用户名查找 ID
        web_username = bot.get("web_username")
        if web_username:
            ok_u, u_res = music_bot_client.get_users()
            if ok_u and isinstance(u_res, dict) and "users" in u_res:
                for u in u_res["users"]:
                    if isinstance(u, dict) and u.get("username") == web_username:
                        web_user_id = str(u.get("id"))
                        break
        if not web_user_id:
            raise HTTPException(status_code=400, detail="该机器人实例未关联 Web 点歌用户或找不到用户 ID")
    
    perm_cfg = get_bot_permission_config()
    target_bots = "all" if perm_cfg.get("bot_scope") == "all" else [str(bot_id)]
    ok, res = music_bot_client.set_user_permissions(
        user_id=str(web_user_id),
        capabilities=perm_cfg.get("capabilities", ["player.control", "player.queue"]),
        bots=target_bots
    )
    if not ok:
        return JSONResponse(status_code=500, content={"success": False, "message": f"权限同步失败: {res}"})
    return {
        "success": True,
        "message": f"成功为用户【{bot.get('web_username', web_user_id)}】重新同步并应用了最新权限！",
        "capabilities": perm_cfg.get("capabilities", []),
        "bots": target_bots
    }


# --- DNS 自动化绑定配置 API ---

@app.get("/api/admin/dns-config")
def get_dns_config_admin_api(_: bool = Depends(verify_admin)):
    cfg = get_dns_config()
    return {
        "success": True,
        "config": cfg
    }

@app.post("/api/admin/dns-config")
def update_dns_config_admin_api(req: DnsConfigRequest, _: bool = Depends(verify_admin)):
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    saved = set_dns_config(data)
    return {
        "success": True,
        "message": "DNS 自动化绑定配置已成功保存！",
        "config": saved
    }

@app.post("/api/admin/dns-config/test")
def test_dns_config_admin_api(req: TestDnsConfigRequest, _: bool = Depends(verify_admin)):
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    ok, msg = dns_service.test_connection(data)
    if ok:
        return {"success": True, "message": msg}
    else:
        return JSONResponse(status_code=400, content={"success": False, "message": msg})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
