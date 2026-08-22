import os
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from database import (
    init_db,
    get_cdk,
    create_cdks,
    get_all_cdks,
    delete_cdk,
    bind_cdk_instance,
    get_instance_by_id,
    get_all_instances,
    create_instance,
    update_instance_token,
    update_instance_status,
    delete_instance,
    get_all_used_ports,
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

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 确保存储目录存在
    try:
        os.makedirs(config.DATA_BASE_DIR, exist_ok=True)
    except Exception as e:
        print(f"[Warning] 无法创建数据根目录: {config.DATA_BASE_DIR}, 错误: {e}")
    print(f"[*] TeamSpeak 管理服务已启动，监听端口: {config.SERVER_PORT}")
    print(f"[*] 数据存储根目录: {config.DATA_BASE_DIR}")
    yield

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

class GenerateCdksRequest(BaseModel):
    count: int = 1
    remark: Optional[str] = ""

class InstanceActionRequest(BaseModel):
    action: str  # 'start', 'stop', 'restart', 'destroy'

# --- 权限校验依赖 ---

def verify_admin(x_admin_password: Optional[str] = Header(None, alias="X-Admin-Password")):
    if not x_admin_password or x_admin_password != config.ADMIN_PASSWORD:
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

    # 客户端展示用的 Host / IP
    client_host = config.PUBLIC_SERVER_IP or request.headers.get("host", "127.0.0.1").split(":")[0]

    # 如果该 CDK 已经兑换过，直接返回已绑定的实例信息
    if cdk_info["status"] == "used":
        instance_id = cdk_info["instance_id"]
        instance = get_instance_by_id(instance_id)
        if instance:
            instance["public_host"] = client_host
            return {
                "success": True,
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
    success, admin_token, msg = deploy_teamspeak_instance(instance_id, ports)
    if not success:
        return JSONResponse(status_code=500, content={"success": False, "message": f"服务器创建失败: {msg}"})

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
        status="running"
    )

    # 绑定 CDK
    bind_cdk_instance(code, instance_id)
    instance["public_host"] = client_host

    return {
        "success": True,
        "message": f"恭喜！TeamSpeak 服务器 ({name}) 已成功开通并启动！",
        "instance": instance
    }

# --- 管理员 API ---

@app.get("/api/admin/system-status")
def get_system_status(_: bool = Depends(verify_admin)):
    cdks = get_all_cdks()
    instances = get_all_instances()
    used_ports = get_all_used_ports()

    voice_ports = used_ports["voice"]
    port_range_str = f"{min(voice_ports)} ~ {max(voice_ports)}" if voice_ports else "暂无"

    return {
        "success": True,
        "total_instances": len(instances),
        "total_cdks": len(cdks),
        "unused_cdks": len([c for c in cdks if c["status"] == "unused"]),
        "voice_ports_summary": port_range_str,
        "data_base_dir": config.DATA_BASE_DIR
    }

@app.get("/api/admin/instances")
def list_instances(_: bool = Depends(verify_admin)):
    instances = get_all_instances()
    # 动态探测 Docker 实际状态
    for inst in instances:
        inst["live_status"] = get_container_status(inst["id"])
    return {"success": True, "instances": instances}

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
    created = create_cdks(req.count, req.remark or "")
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
        selected = [f"{c['code']} (ts{c['instance_id']})" for c in cdks if c["status"] == "used"]
        filename = "used_cdks.txt"
    else:
        selected = [c["code"] for c in cdks]
        filename = "all_cdks.txt"

    content = "\n".join(selected)
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
