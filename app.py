import os
import re
import socket
import subprocess
import ipaddress
import urllib.request
import json
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit
from typing import Optional, Literal, Dict, Any, Tuple, List
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    reserve_instance_slot,
    finalize_instance,
    update_instance_credentials_if_empty,
    update_instance_status,
    update_instance_expiry,
    get_expired_active_instances,
    renew_instance,
    delete_instance,
    get_all_used_ports,
    create_bot_instance,
    get_bot_instance_by_id,
    get_bot_instance_by_cdk,
    get_all_bot_instances,
    restore_bot_cdk,
    restore_instance_cdk,
    update_bot_instance_status,
    delete_bot_instance,
    get_expired_active_bots,
    renew_bot_instance,
    update_bot_instance_expiry,
    get_admin_password,
    set_admin_password,
    get_bot_config,
    set_bot_config,
    MASKED_SECRET,
    get_bot_permission_config,
    set_bot_permission_config,
    record_trial_server,
    get_all_trial_records,
    delete_trial_record,
    claim_cdk,
    release_cdk_claim,
    release_stale_cdk_claims,
    unbind_cdk_instance,
    unbind_cdk_bot,
    reserve_trial_server,
    release_trial_reservation,
    reserve_trial_client_ip,
    release_trial_client_ip,
    confirm_trial_client_ip,
    normalize_server_target,
    get_dns_config,
    get_dns_config_for_provider,
    set_dns_config,
    is_subdomain_available,
    update_instance_domain,
    save_admin_session,
    delete_admin_session,
    delete_all_admin_sessions,
    is_admin_session_valid,
    prune_admin_sessions,
    touch_admin_session,
    has_ip_used_teamspeak_trial,
    update_bot_instance_web_user_id
)
from port_manager import allocate_ports_for_instance
import rate_limit
import docker_service
from docker_service import (
    get_container_status,
    start_instance_container,
    stop_instance_container,
    restart_instance_container,
    destroy_instance_container,
    fetch_container_logs
)
from music_bot_service import music_bot_client
from firewall_service import auto_open_firewall_ports, open_single_instance_ports
from dns_service import dns_service

from contextlib import asynccontextmanager

async def system_expiry_checker():
    """
    后台守护任务：定期扫描所有已到期的音乐机器人和 TeamSpeak 服务器实例，自动停机下线并标记状态为 expired；
    同时回收因进程崩溃/部署中断而长期停留在 processing 的 CDK 占用。
    """
    while True:
        try:
            # 0. 回收超时的 CDK processing 占用（10 分钟未完成即视为异常中断）
            try:
                released = await asyncio.to_thread(release_stale_cdk_claims, 10)
                if released:
                    print(f"[*] 已自动回收 {released} 个超时未完成的 CDK 占用（processing → unused）")
            except Exception as claim_err:
                print(f"[Warning] 回收超时 CDK 占用失败: {claim_err}")
            # 1. 扫描已到期的音乐机器人
            expired_bots = await asyncio.to_thread(get_expired_active_bots)
            for b in expired_bots:
                stop_ok = False
                stop_res = None
                try:
                    print(f"[*] ⏰ 监测到机器人 [{b['name']}] (ID: {b['bot_id']}) 已到达有效期限 ({b['expire_at']})，正在执行自动停机下线...")
                    stop_ok, stop_res = await asyncio.to_thread(music_bot_client.stop_bot, b["bot_id"])
                except Exception as b_err:
                    print(f"[Warning] 停止机器人 [{b['bot_id']}] 发生异常: {b_err}")
                # 关键：必须真正停机成功才标记 expired。
                # 若停机失败仍写入 expired，该实例会因状态不再是 active 而永远逃出本扫描（查询条件为 status='active'），
                # 造成「库中已过期、容器仍在运行」的资源白占与免费续用。
                stop_str = str(stop_res or "").lower()
                is_already_stopped_or_gone = ("404" in stop_str or "not found" in stop_str or "not running" in stop_str or "already stopped" in stop_str)
                if stop_ok or is_already_stopped_or_gone:
                    await asyncio.to_thread(update_bot_instance_status, b["bot_id"], "expired")
                else:
                    print(f"[Warning] 机器人 [{b['bot_id']}] 停机失败（{stop_res}），保留 active 状态等待下一轮重试")

            # 2. 扫描已到期的 TeamSpeak 语音服务器
            expired_instances = await asyncio.to_thread(get_expired_active_instances)
            for inst in expired_instances:
                stop_ok = False
                try:
                    print(f"[*] ⏰ 监测到 TeamSpeak 服务器 [{inst['name']}] (ID: {inst['id']}) 已到达有效期限 ({inst['expire_at']})，正在执行自动停机下线...")
                    stop_ok = await asyncio.to_thread(stop_instance_container, inst["id"])
                except Exception as inst_err:
                    print(f"[Warning] 停止 TeamSpeak 容器 [{inst['id']}] 发生异常: {inst_err}")
                # 同上：停机未成功就保留 running 状态，下一轮继续尝试，绝不让容器偷偷活着
                if stop_ok:
                    await asyncio.to_thread(update_instance_status, inst["id"], "expired")
                else:
                    print(f"[Warning] TeamSpeak 容器 [{inst['id']}] 停机失败，保留 running 状态等待下一轮重试")
        except Exception as err:
            print(f"[Error in system_expiry_checker]: {err}")
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 目录创建属于启动动作，放到 lifespan 而非模块导入期，
    # 避免 import 阶段产生副作用（只读部署 / 多进程启动时会直接抛错）
    for _dir in (config.DATA_BASE_DIR, str(static_path / "css"), str(static_path / "js"), str(templates_path)):
        try:
            os.makedirs(_dir, exist_ok=True)
        except Exception as e:
            print(f"[Warning] 无法创建目录 {_dir}: {e}")

    # 自动放行服务器本地防火墙端口（同步命令，放入线程池避免阻塞事件循环）
    try:
        await asyncio.to_thread(auto_open_firewall_ports)
    except Exception as e:
        print(f"[Warning] 自动配置本地防火墙异常: {e}")

    print(f"[*] TeamSpeak 管理服务已启动，监听端口: {config.SERVER_PORT}")
    print(f"[*] 数据存储根目录: {config.DATA_BASE_DIR}")
    print(f"[*] 音乐机器人对接中心: {get_bot_config()['bot_panel_url']}")
    
    # 启动到期自动停机监控后台任务
    checker_task = asyncio.create_task(system_expiry_checker())
    try:
        yield
    finally:
        # 先取消并等待任务真正结束，避免关闭时报 "Task was destroyed but it is pending"
        checker_task.cancel()
        try:
            await asyncio.gather(checker_task, return_exceptions=True)
        except Exception:
            pass
        # 关闭音乐机器人 HTTP 连接池，避免进程退出时连接泄漏
        try:
            await asyncio.to_thread(music_bot_client.close)
        except Exception:
            pass

app = FastAPI(
    title="TeamSpeak Automated Hosting Platform",
    version="1.0.0",
    lifespan=lifespan
)

# 挂载静态文件与模板
BASE_DIR = Path(__file__).parent.resolve()
static_path = BASE_DIR / "static"
templates_path = BASE_DIR / "templates"

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
    # 限制单请求可携带的卡密数量，避免超大请求体在解析/清洗阶段打满线程
    codes: Optional[List[str]] = Field(default=None, max_length=2000)
    filter: Optional[BatchDeleteFilter] = None

class BatchActionInstancesRequest(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=200)
    action: Literal["start", "stop", "restart", "destroy"]

class BatchActionBotsRequest(BaseModel):
    bot_ids: List[str] = Field(min_length=1, max_length=200)
    action: Literal["start", "stop", "restart", "delete"]

class AdminRenewBotRequest(BaseModel):
    # 与前台一致：仅允许 0(永久)/1/3/6/12 个月，避免负数或超大值生成非法到期时间
    duration_months: Optional[Literal[0, 1, 3, 6, 12]] = 1
    cdk: Optional[str] = Field(default=None, max_length=128)

class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=255)
    new_password: str = Field(min_length=6, max_length=255)

class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=255)

class BotConfigRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    user: str = Field(min_length=1, max_length=255)
    # 允许为空/掩码：表示沿用数据库中原密码，既不回传明文也不覆盖旧值
    password: str = Field(default="", max_length=255)
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

# GeoIP 结果缓存：带 TTL，避免 IP 归属被永久缓存（原 lru_cache 无过期时间）
_GEO_CACHE_TTL_SECONDS = 6 * 3600
_GEO_CACHE: Dict[str, Tuple[float, Tuple[bool, str, str]]] = {}
_GEO_CACHE_LOCK = threading.Lock()


def _cached_ip_geo(ip_str: str) -> Tuple[bool, str, str]:
    """查询 IP 归属，带 TTL 缓存；缓存命中时直接返回，避免频繁打公共 API 触发限流。"""
    now_ts = datetime.now().timestamp()
    with _GEO_CACHE_LOCK:
        hit = _GEO_CACHE.get(ip_str)
        if hit and now_ts < hit[0]:
            return hit[1]
        if len(_GEO_CACHE) > 4096:
            # 简单淘汰：清掉所有已过期项，仍超量则整体清空，避免无界增长
            for key in [k for k, v in _GEO_CACHE.items() if v[0] <= now_ts]:
                _GEO_CACHE.pop(key, None)
            if len(_GEO_CACHE) > 4096:
                _GEO_CACHE.clear()

    result = _fetch_ip_geo(ip_str)
    with _GEO_CACHE_LOCK:
        _GEO_CACHE[ip_str] = (now_ts + _GEO_CACHE_TTL_SECONDS, result)
    return result


def _fetch_ip_geo(ip_str: str) -> Tuple[bool, str, str]:
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback:
            return False, "内网/局域网", "本地内网"
    except Exception:
        return False, "未知", "未知"

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
                return is_overseas, country or "未知", loc
    except Exception:
        pass

    return False, "国内/未知", "默认线路"

def get_ip_geo_info(ip_str: str) -> Dict[str, Any]:
    """
    查询 IP 的地理归属与是否为境外节点（带 LRU 缓存，避免频繁请求命中公共 API 速率限制）
    """
    is_overseas, country, location = _cached_ip_geo(ip_str)
    return {"is_overseas": is_overseas, "country": country, "location": location}

def get_real_client_ip(request: Optional[Request]) -> str:
    """
    提取客户端真实 IP。

    安全前提：X-Forwarded-For / X-Real-IP 是客户端可任意伪造的请求头。
    过去无条件信任 XFF 首段，导致体验卡 IP 防刷可被「每次换一个 XFF」绕过，
    也可让调用方伪装成任意 IP。因此默认只使用 TCP 对端地址（request.client.host），
    仅当显式配置 TRUST_PROXY_HEADERS=1（确认部署在自建可信反向代理之后）时才采信代理头。
    """
    if request is None:
        return "127.0.0.1"
    if config.TRUST_PROXY_HEADERS:
        for header_name in ("x-forwarded-for", "x-real-ip"):
            raw = request.headers.get(header_name)
            if not raw:
                continue
            candidate = raw.split(",")[0].strip()
            if not candidate:
                continue
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"

# --- 权限校验依赖 ---

ADMIN_SESSION_COOKIE = "ts_admin_session"
_ADMIN_SESSION_TTL_SECONDS = 12 * 3600
# 进程内会话一级缓存：token -> 过期时间戳（与 SQLite admin_sessions 表保持持久化同步）
_admin_sessions: Dict[str, float] = {}


def _prune_admin_sessions() -> None:
    now_ts = datetime.now().timestamp()
    for token, expires_at in list(_admin_sessions.items()):
        if expires_at <= now_ts:
            _admin_sessions.pop(token, None)
    try:
        prune_admin_sessions()
    except Exception:
        pass


def _is_https_request(request: Optional[Request]) -> bool:
    """判断当前请求是否走 HTTPS（含反代终止 TLS 的情形），用于决定 Cookie 是否附加 Secure。"""
    if request is None:
        return False
    try:
        if request.url.scheme == "https":
            return True
    except Exception:
        pass
    if config.TRUST_PROXY_HEADERS:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            return True
    return False


def create_admin_session(response: JSONResponse, request: Optional[Request] = None) -> str:
    """创建服务端会话并通过 HttpOnly Cookie 下发，并持久化到数据库以支持重启保持登录。"""
    _prune_admin_sessions()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now().timestamp() + _ADMIN_SESSION_TTL_SECONDS
    _admin_sessions[token] = expires_at
    try:
        save_admin_session(token, expires_at)
    except Exception as e:
        print(f"[Warning] 持久化管理员会话失败（服务重启后需重新登录）: {e}")
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        token,
        max_age=_ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # HTTPS 部署时自动附加 Secure，避免会话在明文链路上被嗅探
        secure=_is_https_request(request),
        path="/",
    )
    return token


def destroy_admin_session(request: Request, response: JSONResponse) -> None:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if token:
        _admin_sessions.pop(token, None)
        try:
            delete_admin_session(token)
        except Exception:
            pass
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")


def _is_valid_admin_session(request: Request) -> bool:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not token:
        return False
    # 1. 优先检查进程内缓存
    expires_at = _admin_sessions.get(token)
    if expires_at and expires_at > datetime.now().timestamp():
        return True
    # 2. 进程内未命中（如服务重启），查询 SQLite 持久化会话表
    try:
        if is_admin_session_valid(token):
            new_expires = datetime.now().timestamp() + _ADMIN_SESSION_TTL_SECONDS
            _admin_sessions[token] = new_expires
            # 滑动续期必须回写持久化表，否则重启后过期时间会回退，且续期永远不落库
            try:
                touch_admin_session(token, new_expires)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def verify_admin(
    request: Request,
    x_admin_password: Optional[str] = Header(None, alias="X-Admin-Password"),
):
    """
    管理员鉴权：优先使用服务端会话 Cookie；同时保留 X-Admin-Password 头以兼容旧版客户端/脚本。
    """
    if _is_valid_admin_session(request):
        return True
    current_pwd = get_admin_password()
    if x_admin_password and secrets.compare_digest(str(x_admin_password), str(current_pwd)):
        return True
    raise HTTPException(status_code=401, detail="管理员密码错误或未提供，请重新登录")

_HOST_HEADER_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

def _safe_host_candidate(value: str) -> Optional[str]:
    """仅接受纯主机名/IP 形式（字母数字、点、下划线、短横线），拒绝带端口/路径/空格的伪造 Host。"""
    host = (value or "").strip().split(",")[0].strip()
    if not host or len(host) > 253:
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not _HOST_HEADER_RE.match(host):
        return None
    return host

def get_public_host(request: Request) -> str:
    """
    只返回规范化主机名，避免直接信任 Host 头造成错误连接地址。

    优先级：PUBLIC_SERVER_IP > (可信反代时才采信 X-Forwarded-Host) > Host > 127.0.0.1。
    Host 头同样可被伪造，因此每一层都要通过格式白名单校验；
    真正面向公网部署时应显式配置 PUBLIC_SERVER_IP，否则默认会退化为实际访问用的 Host。
    """
    candidate = (config.PUBLIC_SERVER_IP or "").strip()
    if not candidate:
        forwarded = (
            _safe_host_candidate(request.headers.get("x-forwarded-host", ""))
            if config.TRUST_PROXY_HEADERS else None
        )
        candidate = (
            forwarded
            or _safe_host_candidate(request.headers.get("host", ""))
            or "127.0.0.1"
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

# --- 管理员会话 API ---

@app.post("/api/admin/login")
def admin_login_api(req: AdminLoginRequest, request: Request):
    """管理员登录：校验口令后下发 HttpOnly 会话 Cookie，前端不再长期保存明文口令。"""
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.LOGIN_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"登录尝试过于频繁，请 {retry_after} 秒后再试"},
        )
    current_pwd = get_admin_password()
    provided = (req.password or "").strip()
    if not provided or not secrets.compare_digest(provided, str(current_pwd)):
        return JSONResponse(status_code=401, content={"success": False, "message": "管理员密码错误"})
    # 登录成功即重置该 IP 的失败计数，避免正常用户被历史失败次数拖累
    rate_limit.reset_rate_limit(f"{rate_limit.LOGIN_LIMITER.name}:{client_ip}")
    resp = JSONResponse(content={"success": True, "message": "登录成功"})
    create_admin_session(resp, request)
    return resp

@app.post("/api/admin/logout")
def admin_logout_api(request: Request):
    resp = JSONResponse(content={"success": True, "message": "已安全退出后台"})
    destroy_admin_session(request, resp)
    return resp

@app.get("/api/admin/session")
def admin_session_api(request: Request):
    """探测当前会话是否有效，供前端决定是否显示登录页。"""
    if _is_valid_admin_session(request):
        return {"success": True, "authenticated": True}
    current_pwd = get_admin_password()
    header_pwd = request.headers.get("x-admin-password")
    if header_pwd and secrets.compare_digest(str(header_pwd), str(current_pwd)):
        return {"success": True, "authenticated": True}
    return JSONResponse(status_code=401, content={"success": False, "authenticated": False})

# --- 用户端 API ---

@app.post("/api/parse-ts-target")
def parse_ts_target_endpoint(req: ParseTsTargetRequest, request: Request):
    """
    智能解析 TeamSpeak 日志/域名/IP，区分境外源站与国内中转节点，优先提取中转地址

    该接口内部会触发 nslookup / DNS 解析 / 外部 GeoIP HTTP 请求，
    因此必须限流，否则会被当作 DNS/HTTP 放大器批量滥用。
    """
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.PARSE_LOG_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"解析请求过于频繁，请 {retry_after} 秒后再试"},
        )

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
        if val.startswith("[") and "]" in val:
            bracket_end = val.find("]")
            connect_host = val[1:bracket_end].strip()
            rest = val[bracket_end + 1:]
            if rest.startswith(":"):
                try:
                    connect_port = int(rest[1:].strip())
                except Exception:
                    pass
        elif ":" in val and not val.startswith("http"):
            parts = val.split(":")
            if len(parts) == 2:
                connect_host = parts[0].strip()
                try:
                    connect_port = int(parts[1].strip())
                except Exception:
                    pass
            else:
                connect_host = val
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

    # 若输入了域名且尚未提取到 SRV，且日志中未直接包含已解析的真实IP，主动通过后端 DNS 查询 SRV
    host_to_query = connect_host or direct_ip
    is_domain = False
    if host_to_query:
        try:
            ipaddress.ip_address(host_to_query)
        except ValueError:
            is_domain = True
            if not srv_target_host and not (direct_ip or lookup_ip):
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
def check_subdomain_endpoint(req: CheckSubdomainRequest, request: Request):
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.PUBLIC_LOOKUP_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "available": False,
                     "message": f"查询过于频繁，请 {retry_after} 秒后再试"},
        )
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
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.REDEEM_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"兑换请求过于频繁，请 {retry_after} 秒后再试"},
        )

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
        return JSONResponse(status_code=403, content={"success": False, "message": "该 CDK 已被系统禁用或吊销"})

    if cdk_info["status"] == "processing":
        return JSONResponse(status_code=409, content={
            "success": False,
            "message": "该 CDK 正在处理中，请稍后重新查询结果"
        })

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
                # 出参脱敏：web_password 绝不回传前端（只保留是否存在标记）
                bot = _mask_bot_secrets(bot)
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
        # 预占中的实例（provisioning）对用户不可见，提示稍后重试即可
        if instance and instance.get("status") == "provisioning":
            return JSONResponse(status_code=409, content={
                "success": False,
                "message": "该 CDK 对应的服务器正在开通中，请稍等片刻后重新查询"
            })
        if instance:
            # 凭据自愈：若首次开机慢导致凭据未提取，再次使用 CDK 访问时从日志尝试提取并持久化
            if not instance.get("admin_token"):
                try:
                    recovered = docker_service.extract_credentials_from_container(instance["id"])
                    if recovered.get("admin_token"):
                        # 逐字段仅在库中为空时补写：整组覆盖会把已成功提取到的
                        # query_password / query_apikey 清空
                        refreshed = update_instance_credentials_if_empty(
                            instance["id"],
                            recovered.get("admin_token", ""),
                            recovered.get("query_password", ""),
                            recovered.get("query_apikey", "")
                        )
                        if refreshed:
                            instance.update(refreshed)
                        else:
                            instance.update(recovered)
                except Exception:
                    pass

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
                    curr_provider = (dns_cfg.get("dns_provider") or "").lower() or None
                    update_instance_domain(instance["id"], full_d, rec_id, domain_provider=curr_provider)
                    instance["subdomain"] = full_d
                    instance["domain_record_id"] = rec_id
                    instance["domain_provider"] = curr_provider
                    instance["public_host"] = full_d
                    instance["has_domain"] = True
                    instance_view = dict(instance)
                    instance_view.pop("dir_path", None)
                    instance_view.pop("domain_record_id", None)
                    return {
                        "success": True,
                        "type": "teamspeak",
                        "message": f"该 CDK 已激活。已成功为您的服务器补绑专属二级域名: {full_d}（免输入端口直连）！",
                        "instance": instance_view
                    }
                else:
                    return JSONResponse(status_code=400, content={"success": False, "message": f"域名补绑失败: {err_dns}"})

            instance["public_host"] = instance.get("subdomain") or client_host
            instance["has_domain"] = bool(instance.get("subdomain"))
            instance_view = dict(instance)
            instance_view.pop("dir_path", None)
            instance_view.pop("domain_record_id", None)
            return {
                "success": True,
                "type": "teamspeak",
                "message": f"该 CDK 已于 {cdk_info['used_at']} 激活，已为您加载服务器连接信息",
                "instance": instance_view
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

    is_trial = bool(cdk_info.get("is_trial", 0))
    real_client_ip = get_real_client_ip(request)

    # 体验卡开通防白嫖校验：针对动态分配端口的 TS 实例，限制同一客户端 IP 7天内只能体验开通一次。
    # 这里的「先查后写」存在竞态窗口（同 IP 并发两发可同时通过），因此额外用服务端 IP 做一次原子预占，
    # 预占失败立即释放 CDK 占用并拒绝；预占成功则无论后续成功或失败都必须释放，避免占位泄漏。
    trial_reserved = False
    if is_trial:
        has_used, trial_rec = has_ip_used_teamspeak_trial(real_client_ip)
        if has_used:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={
                "success": False,
                "message": f"您的 IP ({real_client_ip}) 近期已兑换过 TeamSpeak 体验服务器（CDK: {trial_rec.get('cdk_code', '')}），7 天内限体验 1 次！"
            })
        reserved_ok, _reserved_rec = reserve_trial_client_ip(real_client_ip, code, "teamspeak")
        if not reserved_ok:
            release_cdk_claim(code)
            return JSONResponse(status_code=400, content={
                "success": False,
                "message": "该客户端近期已领取过体验服务器，7 天内限体验 1 次！"
            })
        trial_reserved = True

    duration_m = cdk_info.get("duration_months", 0)
    if duration_m > 0:
        expire_at = (datetime.now() + timedelta(days=30 * duration_m)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        expire_at = "permanent"

    # === 关键并发防护：在分配锁内先落库预占实例编号与四类端口 ===
    # 端口/容器名上的 UNIQUE 索引是真正的并发防线。若不做预占，两个并发兑换会拿到同一 instance_id，
    # 后失败的一方回滚时执行 destroy_instance_container(delete_files=True)，
    # 会把另一方正在运行的容器与数据目录一起删掉。
    reservation: Dict[str, Any] = {}

    def _reserve_slot(candidate_id: int, candidate_ports: Dict[str, int]) -> bool:
        slot_name = f"ts{candidate_id}"
        slot = reserve_instance_slot(
            instance_id=candidate_id,
            name=slot_name,
            container_name=f"ts-teamspeak-{candidate_id}",
            dir_path=os.path.join(config.DATA_BASE_DIR, slot_name),
            voice_port=candidate_ports["voice"],
            file_port=candidate_ports["file"],
            query_port=candidate_ports["query"],
            tsdns_port=candidate_ports["tsdns"],
            cdk_code=code,
            duration_months=duration_m,
            expire_at=expire_at,
            subdomain=subdomain_input or None,
        )
        if slot is None:
            return False
        reservation["slot"] = slot
        return True

    try:
        instance_id, ports = allocate_ports_for_instance(reserve=_reserve_slot)
    except Exception as e:
        if trial_reserved:
            try:
                release_trial_client_ip(real_client_ip, code)
            except Exception:
                pass
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": f"端口分配失败: {str(e)}"})

    name = f"ts{instance_id}"
    container_name = f"ts-teamspeak-{instance_id}"
    instance_dir = os.path.join(config.DATA_BASE_DIR, name)

    def _release_trial_hold():
        """释放体验卡 IP 维度的原子预占，避免部署失败后把该 IP 的体验资格白白占掉。"""
        if not trial_reserved:
            return
        try:
            release_trial_client_ip(real_client_ip, code)
        except Exception as t_err:
            print(f"[Warning] 释放体验卡 IP 预占失败: {t_err}")

    def _rollback_provision(extra_msg: str = ""):
        """部署失败回滚：只清理本次预占的槽位与容器，绝不触碰其它实例。"""
        _release_trial_hold()
        try:
            docker_service.destroy_instance_container(instance_id, delete_files=True)
        except Exception as d_err:
            print(f"[Warning] 回滚销毁容器失败 instance_id={instance_id}: {d_err}")
        try:
            delete_instance(instance_id)
        except Exception as db_err:
            print(f"[Warning] 回滚删除实例预占记录失败 instance_id={instance_id}: {db_err}")
        release_cdk_claim(code)
        return JSONResponse(status_code=500, content={"success": False, "message": extra_msg})

    # 执行 Docker 部署流水线
    try:
        success, creds, msg = docker_service.deploy_teamspeak_instance(instance_id, ports)
    except Exception as e:
        success, creds, msg = False, {}, f"部署过程异常: {e}"
    if not success:
        return _rollback_provision(f"服务器创建失败: {msg}")

    live_status = docker_service.get_container_status(instance_id)
    # 仅 running 视为成功：docker 不可用/查询异常返回 error，不能当作开通成功入库
    if live_status != "running":
        return _rollback_provision(
            f"服务器容器未处于运行状态（当前状态: {live_status}），已自动回滚，请检查服务器 Docker 服务后重试"
        )

    admin_token = creds.get("admin_token", "")
    query_password = creds.get("query_password", "")
    query_apikey = creds.get("query_apikey", "")

    # 执行 DNS 自动绑定 (SRV 记录)
    bound_subdomain = None
    domain_record_id = None
    bound_dns_provider = None
    dns_bind_msg = ""
    if dns_enabled and subdomain_input:
        target_host = dns_cfg.get("dns_target_host") or client_host
        ok_dns, rec_id, full_domain, err_dns = dns_service.create_ts_srv_record(
            subdomain_prefix=subdomain_input,
            target_host=target_host,
            voice_port=ports["voice"],
            dns_cfg=dns_cfg
        )
        if ok_dns:
            bound_subdomain = full_domain
            domain_record_id = rec_id
            # 记录本次使用的服务商：后续销毁/换绑时即便管理员切换了默认服务商，也能删对记录
            bound_dns_provider = (dns_cfg.get("dns_provider") or "").lower() or None
            dns_bind_msg = f"（已自动绑定二级域名: {bound_subdomain}，客户端直连无需输入端口）"
        else:
            dns_bind_msg = f"（DNS 自动绑定提示: {err_dns}）"

    # 把预占槽位补齐凭据并转为 running；失败时回收已经启动的容器和临时 CDK 占用。
    try:
        instance = finalize_instance(
            instance_id=instance_id,
            admin_token=admin_token,
            query_password=query_password,
            query_apikey=query_apikey,
            status="running",
            subdomain=bound_subdomain,
            domain_record_id=domain_record_id,
            domain_provider=bound_dns_provider
        )
        if not instance:
            raise RuntimeError("实例记录写入失败")
        if not bind_cdk_instance(code, instance_id):
            raise RuntimeError("CDK 绑定失败")
    except Exception as e:
        if domain_record_id:
            try:
                ok_del, err_del = dns_service.delete_ts_srv_record(domain_record_id, dns_cfg=dns_cfg)
                if not ok_del:
                    print(f"[Warning] 回滚时删除 DNS 记录失败: {err_del}")
            except Exception as d_err:
                print(f"[Warning] 回滚时删除 DNS 记录异常: {d_err}")
        _release_trial_hold()
        # 回滚顺序：先销毁容器与目录，再删库，最后释放 CDK 占用；每步独立兜底，避免半成品残留
        try:
            docker_service.destroy_instance_container(instance_id, delete_files=True)
        except Exception as d_err:
            print(f"[Warning] 回滚销毁容器失败 instance_id={instance_id}: {d_err}")
        try:
            delete_instance(instance_id)
        except Exception as db_err:
            print(f"[Warning] 回滚删除实例记录失败 instance_id={instance_id}: {db_err}")
        try:
            unbind_cdk_instance(code, instance_id)
        except Exception:
            pass
        try:
            release_cdk_claim(code)
        except Exception as c_err:
            print(f"[Warning] 回滚释放 CDK 占用失败 code={code}: {c_err}")
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
                raw_input=f"{bound_subdomain or client_host}:{ports['voice']}",
                client_ip=real_client_ip
            )
            # 体验记录已正式落库，把 IP 维度的 pending 预占转为正式记录
            confirm_trial_client_ip(real_client_ip, code, target_id=str(instance_id))
        except Exception as e:
            if domain_record_id:
                try:
                    ok_del, err_del = dns_service.delete_ts_srv_record(domain_record_id, dns_cfg=dns_cfg)
                    if not ok_del:
                        print(f"[Warning] 体验卡回滚时删除 DNS 记录失败: {err_del}")
                except Exception as d_err:
                    print(f"[Warning] 体验卡回滚时删除 DNS 记录异常: {d_err}")
            _release_trial_hold()
            delete_instance(instance_id)
            docker_service.destroy_instance_container(instance_id, delete_files=True)
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
    instance_view = dict(instance)
    instance_view.pop("dir_path", None)
    instance_view.pop("domain_record_id", None)
    return {
        "success": True,
        "type": "teamspeak",
        "message": f"恭喜！TeamSpeak 服务器 ({name}) 已成功开通并启动！{dns_bind_msg} ({expire_desc}；{credential_desc})",
        "instance": instance_view
    }

@app.post("/api/redeem-bot")
def redeem_bot_instance(req: RedeemBotRequest, request: Request):
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.REDEEM_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"兑换请求过于频繁，请 {retry_after} 秒后再试"},
        )

    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info.get("cdk_type") != "music_bot":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是音乐机器人兑换码"})

    if cdk_info.get("status") == "disabled":
        return JSONResponse(status_code=403, content={"success": False, "message": "该 CDK 已被系统禁用"})

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
                err_msg = str(res_u).lower()
                # 远端平台对「用户名已存在」的返回文案不固定，这里放宽匹配面，
                # 同时保留原始错误便于排查（不再依赖单一字符串）
                conflict_markers = ("already", "exist", "duplicate", "conflict", "409", "http 400", "用户名")
                if any(marker in err_msg for marker in conflict_markers):
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
        try:
            unbind_cdk_bot(code, bot_id)
        except Exception:
            pass
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
                raw_input=req.serverAddress,
                client_ip=client_ip
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
        # 开通成功后本次仍回传明文密码供用户一次性保存，后续查询走掩码
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
def renew_bot_endpoint(req: RenewBotRequest, request: Request):
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.REDEEM_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"续费请求过于频繁，请 {retry_after} 秒后再试"},
        )

    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info.get("status") == "disabled":
        return JSONResponse(status_code=403, content={"success": False, "message": "该 CDK 已被系统禁用"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

    if cdk_info.get("cdk_type") != "music_bot":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是音乐机器人兑换码"})

    bot = get_bot_instance_by_id(req.bot_id)
    if not bot:
        return JSONResponse(status_code=404, content={"success": False, "message": "未找到要续费的机器人实例"})

    # 永久有效的实例再消耗付费 CDK 续费只会白白浪费一张卡，直接拦截
    if (bot.get("expire_at") or "") == "permanent" and not cdk_info.get("is_trial", 0):
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "该机器人已是永久有效，无需使用续费卡（卡密未消耗，可用于其他实例）"
        })

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
    client_ip = get_real_client_ip(request)
    allowed, retry_after = rate_limit.REDEEM_LIMITER.hit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"续费请求过于频繁，请 {retry_after} 秒后再试"},
        )

    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info.get("status") == "disabled":
        return JSONResponse(status_code=403, content={"success": False, "message": "该 CDK 已被系统禁用"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

    if cdk_info.get("cdk_type") != "teamspeak":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 不是 TeamSpeak 服务器兑换码"})

    instance = get_instance_by_id(req.instance_id)
    if not instance:
        return JSONResponse(status_code=404, content={"success": False, "message": "未找到要续费的 TeamSpeak 实例"})

    # 永久有效实例无需续费，避免空耗卡密
    if (instance.get("expire_at") or "") == "permanent" and not cdk_info.get("is_trial", 0):
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "该服务器已是永久有效，无需使用续费卡（卡密未消耗，可用于其他实例）"
        })

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

def _mask_instance_secrets(inst: Dict[str, Any]) -> Dict[str, Any]:
    """
    列表接口出参脱敏：只回传「凭据是否存在」的布尔标记，不再把 admin_token / query 密码明文塞进响应体。
    管理员需要真实值时走 /api/admin/instances/{id}/credentials 按需单取，避免密钥进入 DOM、浏览器历史与日志。
    """
    view = dict(inst)
    for field in ("admin_token", "query_password", "query_apikey"):
        view[f"has_{field}"] = bool(view.get(field))
        view.pop(field, None)
    return view


def _mask_bot_secrets(bot: Dict[str, Any]) -> Dict[str, Any]:
    view = dict(bot)
    view["has_web_password"] = bool(view.get("web_password"))
    view.pop("web_password", None)
    return view


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
    return {"success": True, "instances": [_mask_instance_secrets(i) for i in instances]}

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

    return {"success": True, "bots": [_mask_bot_secrets(b) for b in bots], "bot_panel_url": get_bot_config()["bot_panel_url"]}

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
        if not bind_cdk_bot(code, bot_id):
            # 绑定失败必须回滚到期时间并释放占用，否则卡密会永久卡在 processing，而有效期已经顺延
            update_bot_instance_expiry(bot_id, bot.get("expire_at"))
            release_cdk_claim(code)
            return JSONResponse(status_code=500, content={"success": False, "message": "续费卡绑定失败，已自动回滚"})
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
        # 顺序很关键：先销毁容器与数据目录，成功后再删 DNS 记录。
        # 若先删 DNS 而容器清理失败，会留下「可连但无域名」且库中 domain_record_id 已悬空的中间态。
        ok = destroy_instance_container(instance_id, delete_files=True)
        if not ok:
            return JSONResponse(status_code=500, content={
                "success": False,
                "message": f"实例 ts{instance_id} 清理失败，数据库记录与 DNS 解析均已保留，请检查 Docker 和数据目录"
            })
        if instance.get("domain_record_id"):
            try:
                # 必须传入实例绑定时的服务商配置：切换过服务商后，用「当前」配置去删旧服务商记录会失败并残留 SRV
                inst_dns_cfg = get_dns_config_for_provider(instance.get("domain_provider"))
                ok_del, err_del = dns_service.delete_ts_srv_record(instance["domain_record_id"], dns_cfg=inst_dns_cfg)
                if not ok_del:
                    print(f"[Warning] 销毁实例 ts{instance_id} 时删除 DNS 记录失败: {err_del}")
            except Exception as e:
                print(f"[Warning] 销毁实例 ts{instance_id} 时删除 DNS 记录异常: {e}")
        delete_instance(instance_id)
        if instance.get("cdk_code"):
            unbind_cdk_instance(instance["cdk_code"], instance_id)
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

    # 换绑策略：先创建新记录，成功后再删除旧记录。
    # 若先删后建，一旦创建失败实例会彻底失去解析，而库里仍保留已被删除的旧 record_id。
    client_host = get_public_host(request)
    target_host = dns_cfg.get("dns_target_host") or client_host
    ok_dns, rec_id, full_d, err_dns = dns_service.create_ts_srv_record(
        subdomain_prefix=subdomain_prefix,
        target_host=target_host,
        voice_port=inst["voice_port"],
        dns_cfg=dns_cfg
    )
    if not ok_dns:
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": f"DNS 绑定失败: {err_dns}（原有解析未受影响，可修正后重试）"
        })

    old_record_id = inst.get("domain_record_id")
    if old_record_id and old_record_id != rec_id:
        try:
            old_dns_cfg = get_dns_config_for_provider(inst.get("domain_provider"))
            dns_service.delete_ts_srv_record(old_record_id, dns_cfg=old_dns_cfg)
        except Exception as e:
            print(f"[Warning] 换绑域名时删除旧 DNS 记录失败（不影响新解析）: {e}")

    curr_provider = (dns_cfg.get("dns_provider") or "").lower() or None
    update_instance_domain(instance_id, full_d, rec_id, domain_provider=curr_provider)
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
            inst_dns_cfg = get_dns_config_for_provider(inst.get("domain_provider"))
            dns_service.delete_ts_srv_record(old_record_id, dns_cfg=inst_dns_cfg)
        except Exception as e:
            print(f"[Warning] 解绑域名时删除 DNS 记录异常: {e}")

    update_instance_domain(instance_id, None, None, domain_provider=None)
    return {"success": True, "message": f"实例 ts{instance_id} 已成功解绑二级域名，恢复为 IP 直连"}

# 批量操作并发度：串行 30s×N 会长时间占满线程池，并发执行可把总耗时压到个位数量级
_BATCH_MAX_WORKERS = 8


def _batch_instance_one(instance_id: int, action: str) -> Tuple[int, bool, str]:
    try:
        if action == "start":
            if start_instance_container(instance_id):
                update_instance_status(instance_id, "running")
                return instance_id, True, ""
            return instance_id, False, "启动失败"
        if action == "stop":
            if stop_instance_container(instance_id):
                update_instance_status(instance_id, "stopped")
                return instance_id, True, ""
            return instance_id, False, "停止失败"
        if action == "restart":
            if restart_instance_container(instance_id):
                update_instance_status(instance_id, "running")
                return instance_id, True, ""
            return instance_id, False, "重启失败"
        if action == "destroy":
            inst = get_instance_by_id(instance_id)
            if not inst:
                return instance_id, False, "实例不存在"
            # 先清理容器与目录，成功后再删 DNS 记录，避免留下「可连但无域名」的悬空状态
            if not destroy_instance_container(instance_id, delete_files=True):
                return instance_id, False, "容器或数据目录清理失败"
            if inst.get("domain_record_id"):
                try:
                    inst_dns_cfg = get_dns_config_for_provider(inst.get("domain_provider"))
                    ok_del, err_del = dns_service.delete_ts_srv_record(
                        inst["domain_record_id"], dns_cfg=inst_dns_cfg
                    )
                    if not ok_del:
                        print(f"[Warning] 批量销毁实例 ts{instance_id} 时删除 DNS 记录失败: {err_del}")
                except Exception as d_err:
                    print(f"[Warning] 批量销毁实例 ts{instance_id} 时删除 DNS 记录异常: {d_err}")
            if not delete_instance(instance_id):
                return instance_id, False, "数据库记录删除失败"
            if inst.get("cdk_code"):
                unbind_cdk_instance(inst["cdk_code"], instance_id)
            return instance_id, True, ""
        return instance_id, False, f"不支持的操作: {action}"
    except Exception as e:
        return instance_id, False, str(e)


@app.post("/api/admin/instances/batch-action")
def batch_manage_instances_api(req: BatchActionInstancesRequest, _: bool = Depends(verify_admin)):
    action = req.action.lower()
    results: List[Tuple[int, bool, str]] = []
    # 并发执行：既避免线程池被长任务占满，也让 200 个实例的批量操作在可接受时间内返回
    with ThreadPoolExecutor(max_workers=min(_BATCH_MAX_WORKERS, max(1, len(req.ids)))) as pool:
        futures = [pool.submit(_batch_instance_one, i, action) for i in req.ids]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append((-1, False, str(e)))

    ok_ids = [r[0] for r in results if r[1]]
    failed = [{"id": r[0], "reason": r[2]} for r in results if not r[1]]
    return {
        "success": True,
        "count": len(ok_ids),
        "failed_count": len(failed),
        "failed": failed,
        "message": (
            f"已成功对 {len(ok_ids)} 个 TS 实例执行【{action}】操作"
            + (f"，{len(failed)} 个失败" if failed else "")
        ),
    }


def _batch_bot_one(bot_id: str, action: str) -> Tuple[str, bool, str]:
    try:
        if action == "start":
            ok, res = music_bot_client.start_bot(bot_id)
            if ok:
                update_bot_instance_status(bot_id, "active")
                return bot_id, True, ""
            return bot_id, False, str(res)
        if action == "stop":
            ok, res = music_bot_client.stop_bot(bot_id)
            if ok:
                update_bot_instance_status(bot_id, "stopped")
                return bot_id, True, ""
            return bot_id, False, str(res)
        if action == "restart":
            ok, res = music_bot_client.restart_bot(bot_id)
            if ok:
                update_bot_instance_status(bot_id, "active")
                return bot_id, True, ""
            return bot_id, False, str(res)
        if action == "delete":
            ok, res = music_bot_client.delete_bot(bot_id)
            if not ok:
                return bot_id, False, str(res)
            if not delete_bot_instance(bot_id):
                return bot_id, False, "远程已删除，但本地记录清理失败"
            return bot_id, True, ""
        return bot_id, False, f"不支持的操作: {action}"
    except Exception as e:
        return bot_id, False, str(e)


@app.post("/api/admin/bots/batch-action")
def batch_manage_bots_api(req: BatchActionBotsRequest, _: bool = Depends(verify_admin)):
    action = req.action.lower()
    results: List[Tuple[str, bool, str]] = []
    with ThreadPoolExecutor(max_workers=min(_BATCH_MAX_WORKERS, max(1, len(req.bot_ids)))) as pool:
        futures = [pool.submit(_batch_bot_one, b, action) for b in req.bot_ids]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append(("", False, str(e)))

    ok_ids = [r[0] for r in results if r[1]]
    failed = [{"bot_id": r[0], "reason": r[2]} for r in results if not r[1]]
    return {
        "success": True,
        "count": len(ok_ids),
        "failed_count": len(failed),
        "failed": failed,
        "message": (
            f"已成功对 {len(ok_ids)} 个音乐机器人执行【{action}】操作"
            + (f"，{len(failed)} 个失败" if failed else "")
        ),
    }


@app.get("/api/admin/instances/{instance_id}/logs")
def get_instance_logs_api(instance_id: int, tail: int = 150, _: bool = Depends(verify_admin)):
    # 限制 tail 范围，避免一次拉取超长日志撑爆响应体
    safe_tail = max(10, min(int(tail or 150), 2000))
    logs = fetch_container_logs(instance_id, tail_lines=safe_tail)
    return {"success": True, "logs": logs, "tail": safe_tail}


@app.get("/api/admin/instances/{instance_id}/credentials")
def get_instance_credentials_api(instance_id: int, _: bool = Depends(verify_admin)):
    """
    按需单取实例凭据。

    列表接口已做脱敏（只回传 has_admin_token 等布尔标记），管理员需要复制真实值时走本接口，
    避免密钥在页面加载时就进入 DOM 与浏览器历史。
    """
    inst = get_instance_by_id(instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="未找到该实例")
    return {
        "success": True,
        "credentials": {
            "query_user": "serveradmin",
            "admin_token": inst.get("admin_token") or "",
            "query_password": inst.get("query_password") or "",
            "query_apikey": inst.get("query_apikey") or "",
        },
    }

@app.get("/api/admin/cdks")
def list_cdks(_: bool = Depends(verify_admin)):
    return {"success": True, "cdks": get_all_cdks()}

@app.post("/api/admin/cdks/generate")
def generate_cdks_api(req: GenerateCdksRequest, _: bool = Depends(verify_admin)):
    if req.count < 1 or req.count > 200:
        raise HTTPException(status_code=400, detail="生成数量必须在 1 到 200 之间")
    try:
        created = create_cdks(
            count=req.count,
            remark=req.remark or "",
            cdk_type=req.cdk_type or "teamspeak",
            duration_months=req.duration_months or 0,
            is_trial=req.is_trial or 0
        )
    except ValueError as e:
        # 参数非法属于客户端错误，不应返回 500
        raise HTTPException(status_code=400, detail=str(e))
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

def _csv_safe(value: str) -> str:
    """
    防表格公式注入：以 = + - @ 开头的单元格在 Excel/WPS 中会被当公式执行。
    这里统一加一个前导单引号，让表格软件按纯文本处理。
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


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

    content = "\n".join(_csv_safe(line) for line in selected)
    return PlainTextResponse(
        content=content,
        headers={
            # 文件名加引号，避免部分客户端解析异常；UTF-8 声明防止中文备注乱码
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/plain; charset=utf-8",
        }
    )

@app.post("/api/admin/change-password")
def change_admin_password_api(req: ChangePasswordRequest, request: Request, _: bool = Depends(verify_admin)):
    old_pwd = req.old_password.strip()
    new_pwd = req.new_password.strip()
    current_pwd = get_admin_password()

    if not secrets.compare_digest(old_pwd, str(current_pwd)):
        return JSONResponse(status_code=400, content={"success": False, "message": "原密码不正确，请重新输入"})

    if len(new_pwd) < 6:
        return JSONResponse(status_code=400, content={"success": False, "message": "新密码长度不能少于 6 位"})

    set_admin_password(new_pwd)
    # 口令变更必须同时吊销「进程内缓存」与「SQLite 持久化会话表」。
    # 过去只清 _admin_sessions，旧 Cookie 会在 _is_valid_admin_session 回查数据库时被重新写回缓存，
    # 导致被盗会话在改密后依然长期有效——等于永远无法强制下线。
    _admin_sessions.clear()
    try:
        revoked = delete_all_admin_sessions()
        print(f"[*] 管理员改密：已吊销 {revoked} 个历史会话")
    except Exception as e:
        print(f"[Warning] 吊销历史管理员会话失败: {e}")
    resp = JSONResponse(content={"success": True, "message": "管理员密码修改成功！所有旧登录已失效，已自动为您续期当前会话"})
    create_admin_session(resp, request)
    return resp

@app.get("/api/admin/bot-config")
def get_bot_config_api(_: bool = Depends(verify_admin)):
    cfg = get_bot_config()
    return {
        "success": True,
        "config": {
            "url": cfg["bot_panel_url"],
            "user": cfg["bot_panel_user"],
            # 不回传明文密码：已配置则返回掩码，前端提交掩码表示“不修改”
            "password": MASKED_SECRET if cfg["bot_panel_pass"] else "",
            "tutorial_url": cfg.get("bot_tutorial_url", "http://103.71.69.156:23452/")
        }
    }

def _validate_public_url(url: str, label: str) -> Optional[str]:
    """
    校验管理员填写的对外 URL：必须带 http(s) 协议头，且不得指向内网 / 环回 / 云元数据地址。
    返回 None 表示通过，否则返回错误说明。
    """
    if not url:
        return f"{label}不能为空"
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"{label}格式不正确，必须以 http:// 或 https:// 开头"
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        return f"{label}格式不正确"
    if not host:
        return f"{label}格式不正确"
    if host in ("localhost", "metadata", "metadata.google.internal"):
        return f"{label}不允许指向内网/元数据地址"
    try:
        addrs = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        # 解析失败交给后续真实连接报错，避免误伤暂时不可解析的合法域名
        return None
    for info in addrs:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return f"{label}不允许指向内网地址（{ip}）"
    return None


@app.post("/api/admin/bot-config")
def update_bot_config_api(req: BotConfigRequest, _: bool = Depends(verify_admin)):
    url = req.url.strip()
    user = req.user.strip()
    password = req.password.strip()
    tutorial_url = req.tutorial_url.strip() if req.tutorial_url else None

    err = _validate_public_url(url, "机器人网站地址 (URL)")
    if err:
        raise HTTPException(status_code=400, detail=err)
    if tutorial_url:
        err_tut = _validate_public_url(tutorial_url, "使用教程跳转网址")
        if err_tut:
            raise HTTPException(status_code=400, detail=err_tut)
    if not user:
        raise HTTPException(status_code=400, detail="管理员登录账号不能为空")

    saved_cfg = set_bot_config(url, user, password, tutorial_url=tutorial_url)
    music_bot_client.update_config(saved_cfg["bot_panel_url"], saved_cfg["bot_panel_user"], saved_cfg["bot_panel_pass"])

    return {
        "success": True,
        "message": "音乐机器人平台对接配置已成功保存并即时生效！",
        "config": {
            "url": saved_cfg["bot_panel_url"],
            "user": saved_cfg["bot_panel_user"],
            # 与 GET 一致：绝不把面板密码明文回传（前端提交掩码即表示不修改）
            "password": MASKED_SECRET if saved_cfg["bot_panel_pass"] else "",
            "tutorial_url": saved_cfg.get("bot_tutorial_url", "http://103.71.69.156:23452/")
        }
    }

@app.post("/api/admin/bot-config/test")
def test_bot_config_api(req: Optional[TestBotConfigRequest] = None, _: bool = Depends(verify_admin)):
    url = (req.url or "").strip() if req and req.url else None
    user = (req.user or "").strip() if req and req.user else None
    password = (req.password or "").strip() if req and req.password else None

    # SSRF 防护：该接口会让服务端主动请求管理员提供的任意 URL（含 169.254.169.254 与内网），必须做地址校验
    if url:
        reason = _validate_public_url(url, "机器人网站地址 (URL)")
        if reason:
            return JSONResponse(status_code=400, content={"success": False, "message": reason, "data": {}})

    # 允许「不改密码」场景提交空值或掩码：此时回落到数据库中的真实配置，否则测试必然鉴权失败
    if not password or password == MASKED_SECRET:
        password = None
    if not user:
        user = None

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
            if ok_u:
                raw_users = u_res.get("users", []) if isinstance(u_res, dict) else (u_res if isinstance(u_res, list) else [])
                for u in raw_users:
                    if isinstance(u, dict) and u.get("username") == web_username:
                        web_user_id = str(u.get("id"))
                        break
        if not web_user_id:
            raise HTTPException(status_code=400, detail="该机器人实例未关联 Web 点歌用户或找不到用户 ID")
        try:
            update_bot_instance_web_user_id(bot_id, web_user_id)
        except Exception:
            pass
    
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

# 需要对前端掩码的 DNS 字段：密钥之外，AK / ZoneId / SecretId 一并保护
_DNS_SENSITIVE_FIELDS = (
    "dns_cf_token", "dns_cf_zone_id",
    "dns_aliyun_ak", "dns_aliyun_sk",
    "dns_tencent_id", "dns_tencent_key",
)


def _mask_dns_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    safe_cfg = dict(cfg)
    for key in _DNS_SENSITIVE_FIELDS:
        if safe_cfg.get(key):
            safe_cfg[key] = MASKED_SECRET
    return safe_cfg


@app.get("/api/admin/dns-config")
def get_dns_config_admin_api(_: bool = Depends(verify_admin)):
    # 敏感字段只回传掩码，避免明文进入页面 DOM 与浏览器历史
    return {
        "success": True,
        "config": _mask_dns_config(get_dns_config())
    }

@app.post("/api/admin/dns-config")
def update_dns_config_admin_api(req: DnsConfigRequest, _: bool = Depends(verify_admin)):
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    saved = set_dns_config(data)
    return {
        "success": True,
        "message": "DNS 自动化绑定配置已成功保存！",
        # 与 GET 保持一致：POST 也绝不能把 Token / SecretKey 明文回传
        "config": _mask_dns_config(saved)
    }

@app.post("/api/admin/dns-config/test")
def test_dns_config_admin_api(req: TestDnsConfigRequest, _: bool = Depends(verify_admin)):
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    # 前端加载配置后密钥框是空的（仅在已配置时显示占位提示），直接拿去测试必然鉴权失败。
    # 这里把空值 / 掩码回落到数据库中的真实值，让「留空表示不修改」在测试场景同样成立。
    stored = get_dns_config()
    for key in _DNS_SENSITIVE_FIELDS:
        val = str(data.get(key) or "").strip()
        if not val or val == MASKED_SECRET:
            data[key] = stored.get(key, "")
    ok, msg = dns_service.test_connection(data)
    if ok:
        return {"success": True, "message": msg}
    else:
        return JSONResponse(status_code=400, content={"success": False, "message": msg})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
