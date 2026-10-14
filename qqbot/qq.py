import asyncio
import time
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings


class SendError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class QQClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http
        self.token = ""
        self.expires_at = 0
        self.lock = asyncio.Lock()

    async def access_token(self):
        async with self.lock:
            if self.token and time.time() < self.expires_at:
                return self.token
            response = await self.http.post(self.settings.qq_token_url, json={
                "appId": self.settings.qq_app_id, "clientSecret": self.settings.qq_app_secret}, timeout=15)
            if response.status_code >= 400:
                raise SendError(f"QQ token HTTP {response.status_code}", response.status_code >= 500)
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("token response is not an object")
                expires = int(data.get("expires_in", 7200))
            except (ValueError, TypeError):
                raise SendError("QQ token 响应格式无效") from None
            if not isinstance(data.get("access_token"), str) or not data["access_token"]:
                raise SendError("QQ token 返回缺少 access_token")
            self.token = data["access_token"]
            self.expires_at = time.time() + max(10, expires - 120)
            return self.token

    async def send(self, identity: dict, text: str, msg_id=None, msg_seq=1):
        if not msg_id:
            raise SendError("QQ 主动推送已停止提供，发送必须带有效 msg_id")
        if self.settings.qq_dry_run:
            return "simulated"
        target = quote(identity["target_id"], safe="")
        resource = "users" if identity["scene"] == "c2c" else "groups"
        payload = {"content": text[:1500], "msg_type": 0, "msg_seq": msg_seq}
        if msg_id:
            payload["msg_id"] = msg_id
        for refresh in range(2):
            token = await self.access_token()
            response = await self.http.post(f"{self.settings.qq_api_base.rstrip('/')}/v2/{resource}/{target}/messages",
                                            headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.settings.qq_app_id},
                                            json=payload, timeout=15)
            if response.status_code == 401 and refresh == 0:
                self.token = ""
                continue
            if response.status_code not in (200, 201, 202, 204):
                try:
                    code = response.json().get("code", "unknown")
                except ValueError:
                    code = "unknown"
                raise SendError(f"QQ HTTP {response.status_code}, code={code}", response.status_code == 429 or response.status_code >= 500)
            return "sent"

    async def gateway(self):
        for refresh in range(2):
            token = await self.access_token()
            response = await self.http.get(self.settings.qq_api_base.rstrip("/") + "/gateway/bot",
                                           headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.settings.qq_app_id},
                                           timeout=15)
            if response.status_code == 401 and refresh == 0:
                self.token = ""
                continue
            if response.status_code != 200:
                raise SendError(f"QQ gateway HTTP {response.status_code}", response.status_code == 429 or response.status_code >= 500)
            try:
                data = response.json()
                parts = urlsplit(data["url"])
                if parts.scheme != "wss" or not parts.hostname or parts.username or parts.password:
                    raise ValueError("invalid gateway URL")
                if parts.port not in (None, 443) or not (parts.hostname.endswith(".qq.com") or parts.hostname == "qq.com"):
                    raise ValueError("gateway must be an official QQ host")
                shards = data.get("shards", 1)
                if isinstance(shards, bool) or not isinstance(shards, int) or shards < 1:
                    raise ValueError("invalid shard count")
                limits = data.get("session_start_limit", {})
                if not isinstance(limits, dict):
                    raise ValueError("invalid gateway limits")
                for key in ("remaining", "reset_after"):
                    if key in limits and (type(limits[key]) is not int or limits[key] < 0):
                        raise ValueError("invalid gateway limits")
                return data
            except (ValueError, TypeError, KeyError, AttributeError):
                raise SendError("QQ gateway 响应格式或 WSS 地址无效") from None
