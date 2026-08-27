import json
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Tuple, List
from config import BOT_PANEL_URL, BOT_PANEL_USER, BOT_PANEL_PASS

class MusicBotClient:
    def __init__(self):
        self.base_url = BOT_PANEL_URL
        self.username = BOT_PANEL_USER
        self.password = BOT_PANEL_PASS
        self._session_cookie: Optional[str] = None
        self._cookie_expires_at: float = 0
        self._sync_with_db()

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
                self._session_cookie = None
                self._cookie_expires_at = 0
        except Exception:
            pass

    def reload_config(self):
        self._session_cookie = None
        self._cookie_expires_at = 0
        self._sync_with_db()

    def update_config(self, base_url: str, username: str, password: str):
        self.base_url = base_url.strip().rstrip("/")
        self.username = username.strip()
        self.password = password.strip()
        self._session_cookie = None
        self._cookie_expires_at = 0

    def _login(self) -> bool:
        """
        向音乐机器人后台登录并保存 Session Cookie
        """
        self._sync_with_db()
        login_url = f"{self.base_url}/api/session/login"
        payload = json.dumps({
            "username": self.username,
            "password": self.password
        }).encode("utf-8")

        req = urllib.request.Request(
            login_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "TeamSpeak-Manager/1.0",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    set_cookie = resp.headers.get("Set-Cookie")
                    if set_cookie:
                        # 提取 session cookie
                        self._session_cookie = set_cookie.split(";")[0]
                        # 缓存 5 天
                        self._cookie_expires_at = time.time() + 5 * 86400
                        return True
        except Exception as e:
            print(f"[MusicBotClient] 登录失败: {e}")
        return False

    def _get_cookie(self) -> Optional[str]:
        self._sync_with_db()
        if not self._session_cookie or time.time() > self._cookie_expires_at:
            if not self._login():
                return None
        return self._session_cookie

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """
        通用的 API 请求封装，支持自动处理 401 重连并带有 CSRF Origin 校验
        """
        self._sync_with_db()
        cookie = self._get_cookie()
        if not cookie:
            return False, "未能连接到音乐机器人服务中心，请检查后台机器人平台地址、账号与密码配置"

        url = f"{self.base_url}{path}"
        headers = {
            "Cookie": cookie,
            "User-Agent": "TeamSpeak-Manager/1.0",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/"
        }

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_text = resp.read().decode("utf-8")
                try:
                    result = json.loads(resp_text)
                except Exception:
                    result = resp_text
                return True, result
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Cookie 过期，重新登录一次并重试
                self._session_cookie = None
                cookie = self._get_cookie()
                if cookie:
                    headers["Cookie"] = cookie
                    req2 = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
                    try:
                        with urllib.request.urlopen(req2, timeout=15) as resp2:
                            resp_text = resp2.read().decode("utf-8")
                            try:
                                result = json.loads(resp_text)
                            except Exception:
                                result = resp_text
                            return True, result
                    except Exception as err2:
                        return False, str(err2)
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            return False, f"HTTP {e.code}: {err_body or e.reason}"
        except Exception as e:
            return False, str(e)

    def test_connection(self, url: Optional[str] = None, username: Optional[str] = None, password: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """
        测试与指定或当前音乐机器人平台的连通性与账号密码有效性
        """
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
        payload = json.dumps({
            "username": target_user,
            "password": target_pass
        }).encode("utf-8")

        req = urllib.request.Request(
            login_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "TeamSpeak-Manager/1.0",
                "Origin": target_url,
                "Referer": f"{target_url}/"
            }
        )

        session_cookie = None
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    set_cookie = resp.headers.get("Set-Cookie")
                    if set_cookie:
                        session_cookie = set_cookie.split(";")[0]
                else:
                    return False, f"登录失败，远程服务器返回状态码: {resp.status}", {}
        except urllib.error.HTTPError as e:
            if e.code == 401 or e.code == 403:
                return False, f"鉴权失败 (HTTP {e.code})：账号或密码错误，请核对后重试", {}
            return False, f"连接异常 (HTTP {e.code}): {e.reason}", {}
        except urllib.error.URLError as e:
            return False, f"无法连接到目标服务器: {e.reason}", {}
        except Exception as e:
            return False, f"连接失败: {str(e)}", {}

        if not session_cookie:
            return False, "登录成功但未收到 Session Cookie 响应", {}

        # 2. 尝试读取机器人列表
        bots_url = f"{target_url}/api/bot"
        req_bots = urllib.request.Request(
            bots_url,
            headers={
                "Cookie": session_cookie,
                "User-Agent": "TeamSpeak-Manager/1.0",
                "Origin": target_url,
                "Referer": f"{target_url}/"
            }
        )

        try:
            with urllib.request.urlopen(req_bots, timeout=10) as resp_bots:
                if resp_bots.status == 200:
                    raw_text = resp_bots.read().decode("utf-8")
                    try:
                        data = json.loads(raw_text)
                    except Exception:
                        data = {}
                    bot_list = data.get("bots", []) if isinstance(data, dict) else []
                    return True, f"对接成功！已成功握手远程平台，当前平台共有 {len(bot_list)} 个机器人实例", {
                        "bot_count": len(bot_list),
                        "connected_url": target_url
                    }
                else:
                    return False, f"抓取机器人列表失败 (HTTP {resp_bots.status})", {}
        except Exception as e:
            return False, f"读取机器人列表异常: {str(e)}", {}

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
        """
        在音乐机器人后台创建新实例
        """
        payload = {
            "name": name,
            "serverAddress": server_address,
            "serverPort": server_port,
            "nickname": nickname,
            "autoStart": auto_start
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
            # 确保启动机器人
            bot_id = res["id"]
            start_ok, start_res = self.start_bot(bot_id)
            if not start_ok:
                return False, {
                    "id": bot_id,
                    "error": f"机器人已创建，但自动启动失败: {start_res}"
                }
        return ok, res

    def start_bot(self, bot_id: str) -> Tuple[bool, Any]:
        return self._request("POST", f"/api/bot/{bot_id}/start")

    def stop_bot(self, bot_id: str) -> Tuple[bool, Any]:
        return self._request("POST", f"/api/bot/{bot_id}/stop")

    def restart_bot(self, bot_id: str) -> Tuple[bool, Any]:
        self.stop_bot(bot_id)
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
        """
        获取音乐机器人后台的所有用户列表
        """
        return self._request("GET", "/api/users")

    def create_user(self, username: str, password: str, role: str = "member") -> Tuple[bool, Any]:
        """
        在音乐机器人后台创建新用户
        """
        payload = {
            "username": username.strip(),
            "password": password.strip(),
            "role": role
        }
        ok, res = self._request("POST", "/api/users", payload)
        if not ok:
            return False, res

        # 尝试从返回结果中提取用户 ID
        user_id = None
        if isinstance(res, dict):
            user_id = res.get("id") or (res.get("user", {}).get("id") if isinstance(res.get("user"), dict) else None)
        
        # 若创建接口未直接返回 ID，调用列表接口查询对应用户的 ID
        if not user_id:
            ok_list, list_res = self.get_users()
            if ok_list and isinstance(list_res, dict) and "users" in list_res:
                for u in list_res["users"]:
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
        """
        设置用户的能力权限与机器人访问范围
        capabilities: 例如 ["player.control", "player.queue"]
        bots: "all" 或 指定的机器人 ID 列表，例如 ["bot_id_123"]
        """
        if capabilities is None:
            capabilities = ["player.control", "player.queue"]
        if bots is None:
            bots = []

        payload = {
            "capabilities": capabilities,
            "bots": bots
        }
        return self._request("PUT", f"/api/users/{user_id}/permissions", payload)

    def delete_user(self, user_id: str) -> Tuple[bool, Any]:
        """
        删除音乐机器人后台的指定用户
        """
        return self._request("DELETE", f"/api/users/{user_id}")

    def reset_user_password(self, user_id: str, new_password: str) -> Tuple[bool, Any]:
        """
        重置指定用户的登录密码
        """
        payload = {
            "newPassword": new_password.strip()
        }
        return self._request("POST", f"/api/users/{user_id}/reset-password", payload)

# 单例实例
music_bot_client = MusicBotClient()
