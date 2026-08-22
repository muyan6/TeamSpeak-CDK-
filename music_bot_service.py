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

    def _login(self) -> bool:
        """
        向音乐机器人后台登录并保存 Session Cookie
        """
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
        if not self._session_cookie or time.time() > self._cookie_expires_at:
            if not self._login():
                return None
        return self._session_cookie

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """
        通用的 API 请求封装，支持自动处理 401 重连并带有 CSRF Origin 校验
        """
        cookie = self._get_cookie()
        if not cookie:
            return False, "未能连接到音乐机器人服务中心"

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
            self.start_bot(bot_id)
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

# 单例实例
music_bot_client = MusicBotClient()
