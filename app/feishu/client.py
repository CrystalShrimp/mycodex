from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from config.settings import settings

logger = logging.getLogger("mycodex.feishu")

# Feishu API base
API_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """Feishu API client for sending messages, files and interactive cards."""

    def __init__(self) -> None:
        self._app_id = settings.feishu_app_id
        self._app_secret = settings.feishu_app_secret
        self._tenant_access_token: str = ""
        self._bot_open_id: str = ""
        self._http = httpx.AsyncClient(timeout=30.0)

    @property
    def bot_open_id(self) -> str:
        return self._bot_open_id

    async def fetch_bot_open_id(self) -> str:
        """Fetch and cache the bot's own open_id via GET /bot/v3/info."""
        if self._bot_open_id:
            return self._bot_open_id
        headers = await self._api_headers()
        resp = await self._http.get(f"{API_BASE}/bot/v3/info", headers=headers)
        data = resp.json()
        # 该接口正常返回 {"code":0,"bot":{"open_id":"ou_..."}}；老版本可能无 code 字段
        if data.get("code") not in (0, None):
            logger.error("fetch_bot_open_id failed: %s", data)
            return ""
        open_id = (data.get("bot") or {}).get("open_id", "")
        if not open_id:
            logger.error("fetch_bot_open_id: no open_id in response: %s", data)
            return ""
        self._bot_open_id = open_id
        logger.info("Bot open_id resolved: %s", open_id)
        return open_id

    async def _ensure_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        resp = await self._http.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._app_id,
                "app_secret": self._app_secret,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Failed to get tenant_access_token: %s", data)
            raise RuntimeError(f"Feishu auth failed: {data.get('msg')}")
        self._tenant_access_token = data["tenant_access_token"]
        return self._tenant_access_token

    async def _api_headers(self) -> dict:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, receive_id: str, text: str, *, is_chat: bool = False) -> dict:
        """Send a text message to a user or chat."""
        receive_id_type = "chat_id" if is_chat else "open_id"
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("send_text failed: %s", data)
            # Retry with refreshed token
            if data.get("code") == 99991663:
                await self._refresh_token()
                return await self.send_text(receive_id, text, is_chat=is_chat)
        return data

    async def send_card(
        self, receive_id: str, card: dict, *, is_chat: bool = False
    ) -> dict:
        """Send an interactive card message."""
        receive_id_type = "chat_id" if is_chat else "open_id"
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("send_card failed: %s", data)
            # Retry with refreshed token (same as send_text)
            if data.get("code") == 99991663:
                await self._refresh_token()
                return await self.send_card(receive_id, card, is_chat=is_chat)
            # Raise on other errors so callers know the card wasn't delivered
            raise RuntimeError(f"send_card failed: code={data.get('code')} msg={data.get('msg')}")
        return data

    async def list_group_members(self, chat_id: str) -> set[str] | None:
        """拉取群成员 open_id 集合（分页）。失败返回 None（权限缺失/网络）。

        API: GET /im/v1/chats/{chat_id}/members
        需要应用开通"获取群成员列表"权限（im:chat 相关只读权限）并发布版本。
        """
        headers = await self._api_headers()
        members: set[str] = set()
        page_token = ""
        for _ in range(50):  # 最多 50 页防死循环
            params: dict = {"member_id_type": "open_id", "page_size": 100}
            if page_token:
                params["page_token"] = page_token
            try:
                resp = await self._http.get(
                    f"{API_BASE}/im/v1/chats/{chat_id}/members",
                    params=params,
                    headers=headers,
                )
            except Exception as exc:
                logger.error("list_group_members request failed: %s", exc)
                return None
            data = resp.json()
            code = data.get("code")
            if code == 99991663:
                await self._refresh_token()
                headers = await self._api_headers()
                continue
            if code != 0:
                logger.error(
                    "list_group_members failed: code=%s msg=%s "
                    "（提示：groups 白名单模式需在飞书后台开通群成员读取权限并发布版本）",
                    code, data.get("msg"),
                )
                return None
            for item in data.get("data", {}).get("items", []):
                mid = item.get("member_id", "")
                if mid:
                    members.add(mid)
            if not data.get("data", {}).get("has_more"):
                return members
            page_token = data.get("data", {}).get("page_token", "")
        return members

    async def upload_file(self, file_path: str | Path, file_type: str = "stream") -> str:
        """Upload a file to Feishu and return file_key.

        API: POST /im/v1/files
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}

        with open(path, "rb") as f:
            files = {
                "file": (path.name, f),
            }
            data = {
                "file_type": file_type,
                "file_name": path.name,
            }
            resp = await self._http.post(
                f"{API_BASE}/im/v1/files",
                headers=headers,
                data=data,
                files=files,
            )

        res_data = resp.json()
        if res_data.get("code") != 0:
            logger.error("upload_file failed: %s", res_data)
            if res_data.get("code") == 99991663:
                await self._refresh_token()
                return await self.upload_file(file_path, file_type=file_type)
            raise RuntimeError(f"Upload file failed: code={res_data.get('code')} msg={res_data.get('msg')}")

        return res_data.get("data", {}).get("file_key", "")

    async def send_file(self, receive_id: str, file_key: str, *, is_chat: bool = False) -> dict:
        """Send a file message to a user or chat.

        API: POST /im/v1/messages
        """
        receive_id_type = "chat_id" if is_chat else "open_id"
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("send_file failed: %s", data)
            if data.get("code") == 99991663:
                await self._refresh_token()
                return await self.send_file(receive_id, file_key, is_chat=is_chat)
            raise RuntimeError(f"send_file failed: code={data.get('code')} msg={data.get('msg')}")
        return data

    async def reply_text(self, message_id: str, text: str) -> dict:
        """Reply to a specific message with text."""
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        return resp.json()

    async def reply_card(self, message_id: str, card: dict) -> dict:
        """Reply to a specific message with a card."""
        headers = await self._api_headers()
        resp = await self._http.post(
            f"{API_BASE}/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )
        return resp.json()

    async def update_card(self, message_id: str, card: dict) -> dict:
        """Update an existing card message."""
        headers = await self._api_headers()
        resp = await self._http.patch(
            f"{API_BASE}/im/v1/messages/{message_id}",
            headers=headers,
            json={"content": json.dumps(card)},
        )
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()


feishu_client = FeishuClient()
