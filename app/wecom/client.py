"""企业微信消息发送封装（回复 / 主动推送 / 流式 / 媒体上传）。"""
from __future__ import annotations

import base64
import logging
import math
import uuid
from pathlib import Path

from app.wecom.ws import WeComWsClient

logger = logging.getLogger("mycodex.wecom.client")

_CHUNK_SIZE = 512 * 1024          # 单分片上限（base64 前）
_UPLOAD_SESSION_TTL_MIN = 30      # 上传会话有效期（文档值）


class WeComClient:
    def __init__(self, ws: WeComWsClient) -> None:
        self._ws = ws

    # ---- 回复（透传触发消息的 req_id，24h 窗口） ----

    async def respond_text(self, req_id: str, text: str) -> dict:
        return await self._ws.request("aibot_respond_msg", {
            "msgtype": "text",
            "text": {"content": text},
        }, req_id=req_id)

    async def respond_markdown(self, req_id: str, content: str) -> dict:
        return await self._ws.request("aibot_respond_msg", {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }, req_id=req_id)

    async def respond_stream(
        self, req_id: str, stream_id: str, content: str, *, finish: bool,
    ) -> dict:
        return await self._ws.request("aibot_respond_msg", {
            "msgtype": "stream",
            "stream": {"id": stream_id, "finish": finish, "content": content},
        }, req_id=req_id)

    async def respond_template_card(self, req_id: str, template_card: dict) -> dict:
        return await self._ws.request("aibot_respond_msg", {
            "msgtype": "template_card",
            "template_card": template_card,
        }, req_id=req_id)

    async def respond_file(self, req_id: str, media_id: str) -> dict:
        return await self._ws.request("aibot_respond_msg", {
            "msgtype": "file",
            "file": {"media_id": media_id},
        }, req_id=req_id)

    async def respond_welcome(self, req_id: str, text: str) -> dict:
        return await self._ws.request("aibot_respond_welcome_msg", {
            "msgtype": "text",
            "text": {"content": text},
        }, req_id=req_id)

    # ---- 主动推送（无需 req_id，仅 template_card / markdown） ----

    async def send_markdown(self, chatid: str, chat_type: int, content: str) -> dict:
        """chat_type: 1=单聊(userid) 2=群聊(chatid)。"""
        return await self._ws.request("aibot_send_msg", {
            "chatid": chatid,
            "chat_type": chat_type,
            "msgtype": "markdown",
            "markdown": {"content": content},
        })

    async def send_template_card(self, chatid: str, chat_type: int, template_card: dict) -> dict:
        return await self._ws.request("aibot_send_msg", {
            "chatid": chatid,
            "chat_type": chat_type,
            "msgtype": "template_card",
            "template_card": template_card,
        })

    # ---- 媒体上传（三步分片，普通文件 ≤20MB） ----

    async def upload_media(self, path: Path, media_type: str = "file") -> str:
        data = path.read_bytes()
        total_chunks = max(1, math.ceil(len(data) / _CHUNK_SIZE))
        md5 = _md5_hex(data)
        init = await self._ws.request("aibot_upload_media_init", {
            "type": media_type,
            "filename": path.name,
            "total_size": len(data),
            "total_chunks": total_chunks,
            "md5": md5,
        })
        if init.get("errcode", -1) != 0:
            raise RuntimeError(f"upload init failed: {init.get('errmsg')}")
        upload_id = (init.get("body") or {}).get("upload_id", "")

        for idx in range(total_chunks):
            chunk = data[idx * _CHUNK_SIZE:(idx + 1) * _CHUNK_SIZE]
            resp = await self._ws.request("aibot_upload_media_chunk", {
                "upload_id": upload_id,
                "chunk_index": str(idx),
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            })
            if resp.get("errcode", -1) != 0:
                raise RuntimeError(f"upload chunk {idx} failed: {resp.get('errmsg')}")

        fin = await self._ws.request("aibot_upload_media_finish", {"upload_id": upload_id})
        if fin.get("errcode", -1) != 0:
            raise RuntimeError(f"upload finish failed: {fin.get('errmsg')}")
        media_id = (fin.get("body") or {}).get("media_id", "")
        if not media_id:
            raise RuntimeError("upload finished but no media_id returned")
        return media_id


def new_stream_id() -> str:
    return uuid.uuid4().hex


def _md5_hex(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()
