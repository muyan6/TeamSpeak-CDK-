import json
import logging
import threading
import time
from http.cookies import SimpleCookie
from email.utils import parsedate_to_datetime
from typing import Dict, Any, Optional, Tuple, List

import httpx

from config import BOT_PANEL_URL, BOT_PANEL_USER, BOT_PANEL_PASS

logger = logging.getLogger("music_bot")

# 连接池复用：避免每次请求都重新建立 TCP/TLS（原 urllib 实现每个请求新建连接）
_HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
_LOGIN_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_MAX_RETRIES = 2
_DEFAULT_COOKIE_TTL = 5 * 86400


def _parse_session_cookie(set_cookie_values: List[str]) -> Tuple[Optional[str], float]:
    """
    从可能的多条 Set-Cookie 中解析会话 Cookie 与真实有效期。
    优先选择名称含 session/token/auth 的 Cookie，并按 Max-Age / Expires 计算过期时间，
    不再无条件缓存 5 天。
    """
    fallback: Optional[Tuple[str, float]] = None
    for raw in set_cookie_values:
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            continue
        for name, morsel in jar.items():
            ttl = _DEFAULT_COOKIE_TTL
            max_age = morsel["max-age"]
            expires = morsel["expires"]
            if max_age:
                try:
                    ttl = int(max_age)
                except (TypeError, ValueError):
                    pass
            elif expires:
                try:
                    exp_dt = parsedate_to_datetime(expires)
                    ttl = int(exp_dt.timestamp() - time.time())
                except Exception:
                    pass
            ttl = max(60, min(ttl, 30 * 86400))
            pair = (f"{name}={morsel.value}", time.time() + ttl)
            if any(k in name.lower() for k in ("session", "token", "auth", "sid")):
                return pair
            if fallback is None:
                fallback = pair
    return fallback if fallback else (None, 0)


class MusicBotClient:
    def __init__(self):
        self.base_url = BOT_PANEL_URL
        self.username = BOT_PANEL_USER
        self.password = BOT_PANEL_PASS
        self._session_cookie: Optional[str] = None
        self._cookie_expires_at: float = 0
        # 会话 Cookie 在多线程（FastAPI 线程池）下共享，必须加锁保护读写
        self._cookie_lock = threading.Lock()
        self._client = httpx.Client(
            limits=_HTTP_LIMITS,
            headers={"User-Agent": "TeamSpeak-Manager/1.0"},
            follow_redirects=False,
        )
        self._sync_with_db()

    def close(self):
        """释放连接池（应用关闭时调用）。"""
        try:
            self._client.close()
        except Exception:
            pass

    def _sync_with_db(self):
        try:
            from database import get_bot_config
            cfg = get_bot_config()
            new_url = cfg["bot_panel_url"]
            new_user = cfg["bot_panel_user"]
            new_pass = cfg["bot_panel_pass"]
            if new_url != self.base_url or new_user != self.username or new_pass != self.password:
                self.base_url = new_url
                self.username = new_user
                self.password = new_pass
                with self._cookie_lock:
                    self._session_cookie = None
                    self._cookie_expires_at = 0
        except Exception as err:
            # 配置读取失败不应静默：明确记录，便于排查数据库/配置问题
            logger.warning("同步机器人平台配置失败，沿用当前配置: %s", err)

    def reload_config(self):
        with self._cookie_lock:
            self._session_cookie = None
            self._cookie_expires_at = 0
        self._sync_with_db()

    def update_config(self, base_url: str, username: str, password: str):
        self.base_url = base_url.strip().rstrip("/")
        self.username = username.strip()
        self.password = password.strip()
        with self._cookie_lock:
            self._session_cookie = None
            self._cookie_expires_at = 0

    def _store_cookie(self, cookie: Optional[str], expires_at: float) -> None:
        with self._cookie_lock:
            self._session_cookie = cookie
            self._cookie_expires_at = expires_at

    def _login(self) -> bool:
        """向音乐机器人后台登录并保存 Session Cookie（线程安全）。"""
        self._sync_with_db()
        login_url = f"{self.base_url}/api/session/login"
        payload = {"username": self.username, "password": self.password}
        try:
            resp = self._client.post(
                login_url,
                json=payload,
                headers={"Origin": self.base_url, "Referer": f"{self.base_url}/"},
                timeout=_LOGIN_TIMEOUT,
            )
        except Exception as err:
            logger.warning("登录音乐机器人平台失败: %s", err)
            return False

        if resp.status_code != 200:
            logger.warning("登录音乐机器人平台返回状态码: %s", resp.status_code)
            return False

        cookie, expires_at = _parse_session_cookie(resp.headers.get_list("set-cookie"))
        if not cookie:
            logger.warning("登录成功但响应中未包含可用的 Session Cookie")
            return False
        self._store_cookie(cookie, expires_at)
        return True

    def _get_cookie(self) -> Optional[str]:
        self._sync_with_db()
        with self._cookie_lock:
            cookie = self._session_cookie
            valid = bool(cookie) and time.time() <= self._cookie_expires_at
        if valid:
            return cookie
        if not self._login():
            return None
        with self._cookie_lock:
            return self._session_cookie

    def _do_request(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> httpx.Response:
        """带指数退避的请求执行，缓解远端瞬时抖动/超时。"""
        last_err: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._client.request(
                    method.upper(), url, content=body, headers=headers, timeout=_REQUEST_TIMEOUT
                )
            except Exception as err:
                last_err = err
                if attempt < _MAX_RETRIES:
                    time.sleep(0.5 * (2 ** attempt))
        raise last_err if last_err else RuntimeError("请求失败")

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """通用 API 请求封装：自动处理 401 重连，并对网络错误重试。"""
        self._sync_with_db()
        cookie = self._get_cookie()
        if not cookie:
            return False, "未能连接到音乐机器人服务中心，请检查后台机器人平台地址、账号与密码配置"

        url = f"{self.base_url}{path}"
        headers = {
            "Cookie": cookie,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            resp = self._do_request(method, url, headers, body)
        except Exception as err:
            logger.warning("请求音乐机器人平台失败 %s %s: %s", method, path, err)
            return False, f"无法连接到音乐机器人平台，请检查网络与后台配置（{type(err).__name__}）"

        if resp.status_code == 401:
            # Cookie 失效：清空后重新登录一次并重试
            self._store_cookie(None, 0)
            cookie = self._get_cookie()
            if not cookie:
                return False, "音乐机器人平台登录态已失效，且使用当前配置重新登录失败"
            headers["Cookie"] = cookie
            try:
                resp = self._do_request(method, url, headers, body)
            except Exception as err:
                logger.warning("重试请求音乐机器人平台失败 %s %s: %s", method, path, err)
                return False, f"重试请求音乐机器人平台失败（{type(err).__name__}）"

        if resp.status_code >= 400:
            err_body = ""
            try:
                err_body = resp.text[:500]
            except Exception:
                pass
            logger.warning("音乐机器人平台返回 HTTP %s: %s", resp.status_code, err_body)
            return False, f"HTTP {resp.status_code}: {err_body or resp.reason_phrase}"

        try:
            return True, json.loads(resp.text)
        except Exception:
            # 非 JSON 响应（如网关 HTML 错误页）统一按原始文本返回，调用方需做类型判断
            return True, resp.text

    def test_connection(self, url: Optional[str] = None, username: Optional[str] = None, password: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """测试与指定或当前音乐机器人平台的连通性与账号密码有效性"""
        target_url = (url or self.base_url).strip().rstrip("/")
        target_user = (username or self.username).strip()
        target_pass = (password or self.password).strip()

        if not target_url:
            return False, "机器人平台网址 (URL) 不能为空", {}
        if not target_url.startswith("http://") and not target_url.startswith("https://"):
            return False, "平台网址格式不正确，必须以 http:// 或 https:// 开头", {}
        if not target_user:
            return False, "管理员账号不能为空", {}
        if not target_pass:
            return False, "管理员密码不能为空", {}

        # 1. 尝试登录获取 Cookie
        login_url = f"{target_url}/api/session/login"
        try:
            resp = self._client.post(
                login_url,
                json={"username": target_user, "password": target_pass},
                headers={"Origin": target_url, "Referer": f"{target_url}/"},
                timeout=_LOGIN_TIMEOUT,
            )
        except httpx.HTTPStatusError as e:
            return False, f"连接异常 (HTTP {e.response.status_code})", {}
        except Exception as e:
            return False, f"连接失败: {type(e).__name__}", {}

        if resp.status_code in (401, 403):
            return False, f"鉴权失败 (HTTP {resp.status_code})：账号或密码错误，请核对后重试", {}
        if resp.status_code != 200:
            return False, f"登录失败，远程服务器返回状态码: {resp.status_code}", {}

        session_cookie, _ = _parse_session_cookie(resp.headers.get_list("set-cookie"))
        if not session_cookie:
            return False, "登录成功但未收到 Session Cookie 响应", {}

        # 2. 尝试读取机器人列表
        try:
            bots_resp = self._client.get(
                f"{target_url}/api/bot",
                headers={"Cookie": session_cookie, "Origin": target_url, "Referer": f"{target_url}/"},
                timeout=_LOGIN_TIMEOUT,
            )
        except Exception as e:
            return False, f"读取机器人列表异常: {type(e).__name__}", {}

        if bots_resp.status_code != 200:
            return False, f"抓取机器人列表失败 (HTTP {bots_resp.status_code})", {}

        try:
            data = json.loads(bots_resp.text)
        except Exception:
            data = {}
        bot_list = data.get("bots", []) if isinstance(data, dict) else []
        return True, f"对接成功！已成功握手远程平台，当前平台共有 {len(bot_list)} 个机器人实例", {
            "bot_count": len(bot_list),
            "connected_url": target_url,
        }

    def create_bot(
        self,
        name: str,
        server_address: str,
        server_port: int = 9987,
        nickname: str = "MusicBot",
        default_channel: Optional[str] = None,
        channel_id: Optional[int] = None,
        server_password: Optional[str] = None,
        channel_password: Optional[str] = None,
        auto_start: bool = True
    ) -> Tuple[bool, Any]:
        """在音乐机器人后台创建新实例"""
        payload = {
            "name": name,
            "serverAddress": server_address,
            "serverPort": server_port,
            "nickname": nickname,
            "autoStart": auto_start,
        }
        if default_channel:
            payload["defaultChannel"] = default_channel
        if channel_id:
            payload["channelId"] = channel_id
        if server_password:
            payload["serverPassword"] = server_password
        if channel_password:
            payload["channelPassword"] = channel_password

        ok, res = self._request("POST", "/api/bot", payload)
        if ok and auto_start and isinstance(res, dict) and "id" in res:
            bot_id = res["id"]
            start_ok, start_res = self.start_bot(bot_id)
            if not start_ok:
                return False, {
                    "id": bot_id,
                    "error": f"机器人已创建，但自动启动失败: {start_res}",
                }
        return ok, res

    def start_bot(self, bot_id: str) -> Tuple[bool, Any]:
        return self._request("POST", f"/api/bot/{bot_id}/start")

    def stop_bot(self, bot_id: str) -> Tuple[bool, Any]:
        return self._request("POST", f"/api/bot/{bot_id}/stop")

    def restart_bot(self, bot_id: str) -> Tuple[bool, Any]:
        stop_ok, stop_res = self.stop_bot(bot_id)
        if not stop_ok:
            logger.info("restart_bot: 停止机器人 [%s] 响应: %s（可能处于非运行状态，继续尝试启动）", bot_id, stop_res)
        time.sleep(1)
        return self.start_bot(bot_id)

    def delete_bot(self, bot_id: str) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/api/bot/{bot_id}")

    def get_bot(self, bot_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/api/bot/{bot_id}")

    def get_all_bots(self) -> Tuple[bool, Any]:
        return self._request("GET", "/api/bot")

    # --- 用户与权限管理 ---

    def get_users(self) -> Tuple[bool, Any]:
        """获取音乐机器人后台的所有用户列表"""
        return self._request("GET", "/api/users")

    def create_user(self, username: str, password: str, role: str = "member") -> Tuple[bool, Any]:
        """在音乐机器人后台创建新用户"""
        payload = {
            "username": username.strip(),
            "password": password.strip(),
            "role": role,
        }
        ok, res = self._request("POST", "/api/users", payload)
        if not ok:
            return False, res

        user_id = None
        if isinstance(res, dict):
            inner = res.get("user")
            user_id = res.get("id") or (inner.get("id") if isinstance(inner, dict) else None)

        if not user_id:
            ok_list, list_res = self.get_users()
            if ok_list:
                raw_users = list_res.get("users", []) if isinstance(list_res, dict) else (list_res if isinstance(list_res, list) else [])
                for u in raw_users:
                    if isinstance(u, dict) and u.get("username") == username.strip():
                        user_id = u.get("id")
                        res = u
                        break

        if user_id:
            if isinstance(res, dict):
                res["id"] = user_id
            else:
                res = {"id": user_id, "username": username.strip(), "role": role}
            return True, res
        return ok, res

    def set_user_permissions(self, user_id: str, capabilities: Optional[List[str]] = None, bots: Any = None) -> Tuple[bool, Any]:
        """设置用户的能力权限与机器人访问范围"""
        if capabilities is None:
            capabilities = ["player.control", "player.queue"]
        if bots is None:
            bots = []
        payload = {"capabilities": capabilities, "bots": bots}
        return self._request("PUT", f"/api/users/{user_id}/permissions", payload)

    def get_user_permissions(self, user_id: str) -> Tuple[bool, Any]:
        """获取指定用户的权限能力与机器人授权列表"""
        return self._request("GET", f"/api/users/{user_id}/permissions")

    def sync_bot_permissions(self) -> Tuple[bool, Dict[str, Any]]:
        """从音乐机器人后台拉取现有用户列表与权限配置，萃取平台支持的权限能力与模板用户"""
        standard_capabilities = [
            {"token": "player.control", "key": "player.control", "label": "播放控制", "name": "播放控制", "desc": "允许暂停、继续、切歌及调节音量等"},
            {"token": "player.queue", "key": "player.queue", "label": "队列管理", "name": "队列管理", "desc": "允许提交点歌、清空队列与排队调整"},
            {"token": "bot.manage", "key": "bot.manage", "label": "机器人管理", "name": "机器人管理", "desc": "允许启停机器人、修改配置与频道（机器人管理权限）"},
            {"token": "bot.create", "key": "bot.create", "label": "创建新实例", "name": "创建新实例", "desc": "允许在机器人后台创建新的机器人"},
            {"token": "platform.auth", "key": "platform.auth", "label": "平台登录凭据", "name": "平台登录凭据", "desc": "允许配置网易云/QQ音乐/B站登录凭据"},
            {"token": "quality", "key": "quality", "label": "音质设置", "name": "音质设置", "desc": "允许调整音质比特率与采样率参数"},
        ]

        ok_users, users_res = self.get_users()
        extracted_users = []
        discovered_caps = set(c["token"] for c in standard_capabilities)

        if ok_users and isinstance(users_res, dict) and "users" in users_res:
            raw_users = users_res["users"]
        elif ok_users and isinstance(users_res, list):
            raw_users = users_res
        else:
            raw_users = []

        for u in raw_users:
            if not isinstance(u, dict):
                continue
            u_id = str(u.get("id") or u.get("userId") or "")
            u_name = u.get("username") or u.get("name") or "未知用户"
            u_role = u.get("role") or "member"
            caps = u.get("capabilities") or []
            if not caps and u_id:
                ok_p, p_res = self.get_user_permissions(u_id)
                if ok_p and isinstance(p_res, dict):
                    caps = p_res.get("capabilities", [])

            if isinstance(caps, list):
                for c in caps:
                    if isinstance(c, str) and c.strip():
                        discovered_caps.add(c.strip())

            extracted_users.append({
                "id": u_id,
                "username": u_name,
                "role": u_role,
                "capabilities": caps if isinstance(caps, list) else [],
            })

        return True, {
            "connected": ok_users,
            "message": f"成功同步机器人平台！发现 {len(extracted_users)} 个平台用户" if ok_users else "未能连接机器人平台，已载入标准权限库",
            "users": extracted_users,
            "standard_capabilities": standard_capabilities,
            "discovered_capabilities": sorted(list(discovered_caps)),
        }

    def delete_user(self, user_id: str) -> Tuple[bool, Any]:
        """删除音乐机器人后台的指定用户"""
        return self._request("DELETE", f"/api/users/{user_id}")

    def reset_user_password(self, user_id: str, new_password: str) -> Tuple[bool, Any]:
        """重置指定用户的登录密码"""
        payload = {"newPassword": new_password.strip()}
        return self._request("POST", f"/api/users/{user_id}/reset-password", payload)


# 单例实例
music_bot_client = MusicBotClient()