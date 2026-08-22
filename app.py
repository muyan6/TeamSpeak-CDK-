import os
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import asyncio
from datetime import datetime, timedelta
import config
from database import (
    init_db,
    get_cdk,
    create_cdks,
    get_all_cdks,
    delete_cdk,
    bind_cdk_instance,
    bind_cdk_bot,
    get_instance_by_id,
    get_all_instances,
    create_instance,
    update_instance_token,
    update_instance_status,
    delete_instance,
    get_all_used_ports,
    create_bot_instance,
    get_bot_instance_by_id,
    get_bot_instance_by_cdk,
    get_all_bot_instances,
    update_bot_instance_status,
    delete_bot_instance,
    get_expired_active_bots,
    renew_bot_instance,
    get_admin_password,
    set_admin_password
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

from contextlib import asynccontextmanager

async def bot_expiry_checker():
    """
    后台守护任务：定期扫描所有已到期的音乐机器人，自动向远程平台发送停止命令并标记状态为 expired
    """
    while True:
        try:
            expired_list = get_expired_active_bots()
            for b in expired_list:
                print(f"[*] ⏰ 监测到机器人 [{b['name']}] (ID: {b['bot_id']}) 已到达有效期限 ({b['expire_at']})，正在执行自动停机下线...")
                music_bot_client.stop_bot(b["bot_id"])
                update_bot_instance_status(b["bot_id"], "expired")
        except Exception as err:
            print(f"[Error in bot_expiry_checker]: {err}")
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
    print(f"[*] 音乐机器人对接中心: {config.BOT_PANEL_URL}")
    
    # 启动到期自动停机监控后台任务
    checker_task = asyncio.create_task(bot_expiry_checker())
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
    cdk: str

class RedeemBotRequest(BaseModel):
    cdk: str
    name: str = "我的音乐机器人"
    serverAddress: str
    serverPort: int = 9987
    nickname: str = "MusicBot"
    defaultChannel: Optional[str] = None
    serverPassword: Optional[str] = None

class GenerateCdksRequest(BaseModel):
    count: int = 1
    remark: Optional[str] = ""
    cdk_type: Optional[str] = "teamspeak"  # 'teamspeak' 或 'music_bot'
    duration_months: Optional[int] = 0     # 0 为永久, 1, 3, 6, 12 为月数

class InstanceActionRequest(BaseModel):
    action: str  # 'start', 'stop', 'restart', 'destroy'

class BotActionRequest(BaseModel):
    action: str  # 'start', 'stop', 'restart', 'delete'

class RenewBotRequest(BaseModel):
    cdk: str
    bot_id: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

# --- 权限校验依赖 ---

def verify_admin(x_admin_password: Optional[str] = Header(None, alias="X-Admin-Password")):
    current_pwd = get_admin_password()
    if not x_admin_password or x_admin_password != current_pwd:
        raise HTTPException(status_code=401, detail="管理员密码错误或未提供")
    return True

# --- 页面路由 ---

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    try:
        return templates.TemplateResponse(request=request, name="index.html")
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    try:
        return templates.TemplateResponse(request=request, name="admin.html")
    except TypeError:
        return templates.TemplateResponse("admin.html", {"request": request})

# --- 用户端 API ---

@app.post("/api/redeem")
def redeem_cdk(req: RedeemRequest, request: Request):
    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
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
                    "bot_panel_url": config.BOT_PANEL_URL
                }

        # 未使用：返回需要前端填写机器人连接配置
        duration_m = cdk_info.get("duration_months", 1)
        duration_desc = f"{duration_m} 个月" if duration_m > 0 else "永久"
        return {
            "success": True,
            "type": "music_bot",
            "status": "unused",
            "need_config": True,
            "cdk": code,
            "duration_months": duration_m,
            "duration_desc": duration_desc,
            "message": f"CDK 验证成功！当前为【音乐机器人 - {duration_desc}】，请填写目标服务器连接配置以启动机器人"
        }

    # === 分支 2: TeamSpeak 语音服务器 CDK ===
    client_host = config.PUBLIC_SERVER_IP or request.headers.get("host", "127.0.0.1").split(":")[0]

    # 如果该 CDK 已经兑换过，直接返回已绑定的实例信息
    if cdk_info["status"] == "used":
        instance_id = cdk_info["instance_id"]
        instance = get_instance_by_id(instance_id)
        if instance:
            instance["public_host"] = client_host
            return {
                "success": True,
                "type": "teamspeak",
                "message": f"该 CDK 已于 {cdk_info['used_at']} 激活，已为您加载服务器连接信息",
                "instance": instance
            }

    # 未使用：开始分配端口与开通
    try:
        instance_id, ports = allocate_ports_for_instance()
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": f"端口分配失败: {str(e)}"})

    name = f"ts{instance_id}"
    container_name = f"ts-teamspeak-{instance_id}"
    instance_dir = os.path.join(config.DATA_BASE_DIR, name)

    # 执行 Docker 部署流水线
    success, creds, msg = deploy_teamspeak_instance(instance_id, ports)
    if not success:
        return JSONResponse(status_code=500, content={"success": False, "message": f"服务器创建失败: {msg}"})

    admin_token = creds.get("admin_token", "")
    query_password = creds.get("query_password", "")
    query_apikey = creds.get("query_apikey", "")

    # 记录到数据库
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
        status="running"
    )

    # 绑定 CDK
    bind_cdk_instance(code, instance_id)
    instance["public_host"] = client_host

    # 针对新创建的实例端口进行本地防火墙即时放行
    try:
        open_single_instance_ports(ports["voice"], ports["file"], ports["query"], ports["tsdns"])
    except Exception:
        pass

    return {
        "success": True,
        "type": "teamspeak",
        "message": f"恭喜！TeamSpeak 服务器 ({name}) 已成功开通并启动！",
        "instance": instance
    }

@app.post("/api/redeem-bot")
def redeem_bot_instance(req: RedeemBotRequest):
    code = req.cdk.strip().upper()
    cdk_info = get_cdk(code)
    if not cdk_info:
        return JSONResponse(status_code=400, content={"success": False, "message": "CDK 无效或不存在"})

    if cdk_info["status"] != "unused":
        return JSONResponse(status_code=400, content={"success": False, "message": "该 CDK 已经使用或不可用"})

    # 智能兼容：如果用户在服务器地址中直接填入了 IP:Port (如 stormclub.ts3.uno:60001)
    raw_addr = req.serverAddress.strip()
    target_port = req.serverPort
    if ":" in raw_addr and not raw_addr.startswith("http://") and not raw_addr.startswith("https://"):
        parts = raw_addr.split(":")
        raw_addr = parts[0].strip()
        try:
            target_port = int(parts[1].strip())
        except ValueError:
            pass

    # 调用远程音乐机器人 API 创建实例
    ok, res = music_bot_client.create_bot(
        name=req.name.strip() or "我的音乐机器人",
        server_address=raw_addr,
        server_port=target_port,
        nickname=req.nickname.strip() or "MusicBot",
        default_channel=req.defaultChannel.strip() if req.defaultChannel else None,
        server_password=req.serverPassword if req.serverPassword else None,
        auto_start=True
    )

    if not ok or not isinstance(res, dict) or "id" not in res:
        return JSONResponse(status_code=500, content={"success": False, "message": f"音乐机器人创建失败: {res}"})

    bot_id = res["id"]
    duration_m = cdk_info.get("duration_months", 1)
    if duration_m > 0:
        expire_at = (datetime.now() + timedelta(days=30 * duration_m)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        expire_at = "permanent"

    # 保存本地数据库
    bot_inst = create_bot_instance(
        bot_id=bot_id,
        name=req.name,
        server_address=raw_addr,
        server_port=target_port,
        nickname=req.nickname,
        cdk_code=code,
        duration_months=duration_m,
        expire_at=expire_at,
        default_channel=req.defaultChannel,
        status="active"
    )

    # 绑定 CDK
    bind_cdk_bot(code, bot_id)

    return {
        "success": True,
        "type": "music_bot",
        "message": f"🎉 音乐机器人已成功创建并对接！到期时间: {expire_at}",
        "instance": bot_inst,
        "bot_panel_url": config.BOT_PANEL_URL
    }

@app.post("/api/bot-instances/{bot_id}/action")
def user_bot_action(bot_id: str, req: BotActionRequest):
    action = req.action.lower()
    bot = get_bot_instance_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="未找到该机器人实例")

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

    add_m = cdk_info.get("duration_months", 1)
    renewed_bot = renew_bot_instance(req.bot_id, add_m)
    bind_cdk_bot(code, req.bot_id)

    # 尝试重新拉起机器人
    music_bot_client.start_bot(req.bot_id)

    return {
        "success": True,
        "type": "music_bot",
        "message": f"🎉 续费成功！机器人有效期已顺延至: {renewed_bot['expire_at']}",
        "instance": renewed_bot,
        "bot_panel_url": config.BOT_PANEL_URL
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
        "bot_panel_url": config.BOT_PANEL_URL
    }

@app.get("/api/admin/instances")
def list_instances(_: bool = Depends(verify_admin)):
    instances = get_all_instances()
    # 动态探测 Docker 实际状态
    for inst in instances:
        inst["live_status"] = get_container_status(inst["id"])
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

    now_dt = datetime.now()
    for bot in bots:
        r_info = remote_map.get(bot["bot_id"])
        bot["remote_info"] = r_info
        bot["connected"] = r_info.get("connected", False) if r_info else False
        bot["playing"] = r_info.get("playing", False) if r_info else False
        
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

    return {"success": True, "bots": bots, "bot_panel_url": config.BOT_PANEL_URL}

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
        music_bot_client.delete_bot(bot_id)
        delete_bot_instance(bot_id)
        return {"success": True, "message": f"机器人 {bot['name']} 已删除"}

    else:
        raise HTTPException(status_code=400, detail="不支持的操作指令")

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
        ok = destroy_instance_container(instance_id, delete_files=True)
        delete_instance(instance_id)
        return {"success": True, "message": f"实例 ts{instance_id} 及其存储目录已彻底销毁"}

    else:
        raise HTTPException(status_code=400, detail="不支持的操作指令")

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
        duration_months=req.duration_months or 0
    )
    return {"success": True, "created": created}

@app.delete("/api/admin/cdks/{code}")
def delete_cdk_api(code: str, _: bool = Depends(verify_admin)):
    ok = delete_cdk(code)
    return {"success": ok}

@app.get("/api/admin/cdks/export")
def export_cdks_txt(status: Optional[str] = "unused", _: bool = Depends(verify_admin)):
    from fastapi.responses import PlainTextResponse
    cdks = get_all_cdks()
    if status == "unused":
        selected = [c["code"] for c in cdks if c["status"] == "unused"]
        filename = "unused_cdks.txt"
    elif status == "used":
        selected = [
            f"{c['code']}\t类型: {'音乐机器人' if c.get('cdk_type') == 'music_bot' else 'TS服务器'}\t绑定: {c.get('bot_id') or ('ts' + str(c.get('instance_id')))}"
            for c in cdks if c["status"] == "used"
        ]
        filename = "used_cdks.txt"
    else:
        selected = [
            f"{c['code']}\t类型: {'音乐机器人' if c.get('cdk_type') == 'music_bot' else 'TS服务器'}\t时长: {str(c.get('duration_months')) + '个月' if c.get('duration_months') else '永久'}\t状态: {c['status']}\t备注: {c.get('remark') or '无'}"
            for c in cdks
        ]
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
