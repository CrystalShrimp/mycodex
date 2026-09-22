"""企业微信智能机器人长连接客户端（纯 JSON cmd 协议，无 SDK）。

wss://openws.work.weixin.qq.com — 订阅(aibot_subscribe)后收
aibot_msg_callback / aibot_event_callback，回复用 req_id 关联。
30s ping 心跳、指数退避重连、msgid 排重、单机器人单连接（新连接会
收到 disconnected_event 被踢，属预期，重连即可）。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from config.settings import settings

logger = logging.getLogger("mycodex.wecom.ws")

WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
_HEARTBEAT_INTERVAL = 30.0
_DEDUP_WINDOW = 2000


class WeComWsError(RuntimeError):
    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"wecom errcode={errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class WeComWsClient:
    """长连接 + 请求/响应关联。"""

    def __init__(
        self,
        bot_id: str,
        secret: str,
        on_message: Callable[[dict, str], Awaitable[None]] | None = None,
        on_event: Callable[[dict, str], Awaitable[None]] | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._secret = secret
        self._on_message = on_message
        self._on_event = on_event
        self._conn = None
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._seen_msgids: deque[str] = deque(maxlen=_DEDUP_WINDOW)
        self._connected_at = 0.0
        self._msg_count = 0
        self._last_msg_time = 0.0
        self._last_msg_type = ""
        self._last_error = ""
        self._stopped = False
        # 诊断：最近一次订阅是否成功
        self._subscribed = False

    # ---- lifecycle ----

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.get_running_loop().create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopped = True
        for t in (self._task, self._ping_task):
            if t and not t.done():
                t.cancel()
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
        self._task = None
        self._ping_task = None

    @property
    def connected(self) -> bool:
        return self._subscribed and self._conn is not None

    def health(self) -> dict:
        return {
            "connected": self.connected,
            "subscribed": self._subscribed,
            "msg_count": self._msg_count,
            "last_msg_time": self._last_msg_time,
            "last_msg_type": self._last_msg_type,
            "last_error": self._last_error,
            "uptime_s": (time.time() - self._connected_at) if self._connected_at else 0,
        }

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopped:
            try:
                async with connect(WECOM_WS_URL, open_timeout=20, ping_interval=None) as conn:
                    self._conn = conn
                    recv_task = asyncio.get_running_loop().create_task(self._recv_loop())
                    ping_task = None
                    try:
                        await self._subscribe()
                        backoff = 1.0
                        self._connected_at = time.time()
                        logger.info("WeCom WS connected and subscribed (bot=%s)", self._bot_id)
                        ping_task = asyncio.get_running_loop().create_task(self._ping_loop())
                        await recv_task
                    finally:
                        if ping_task and not ping_task.done():
                            ping_task.cancel()
                        if recv_task and not recv_task.done():
                            recv_task.cancel()
                        self._subscribed = False
                        self._conn = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                logger.warning("WeCom WS disconnected (%s), reconnect in %.1fs", self._last_error, backoff)
            if self._stopped:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _subscribe(self) -> None:
        resp = await self.request("aibot_subscribe", {
            "bot_id": self._bot_id,
            "secret": self._secret,
        }, timeout=20)
        if resp.get("errcode", -1) != 0:
            raise WeComWsError(resp.get("errcode", -1), resp.get("errmsg", "subscribe failed"))
        self._subscribed = True

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                resp = await self.request("ping", {}, timeout=15)
                if resp.get("errcode", -1) != 0:
                    logger.warning("WeCom ping errcode=%s", resp.get("errcode"))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # ping 失败让 recv 循环自然断开重连；这里只记录
            logger.warning("WeCom ping loop error: %s", e)

    async def _recv_loop(self) -> None:
        assert self._conn is not None
        while True:
            raw = await self._conn.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("WeCom non-JSON frame: %s", raw[:200])
                continue

            cmd = frame.get("cmd", "")
            req_id = (frame.get("headers") or {}).get("req_id", "")
            body = frame.get("body") or {}

            if not cmd:
                # 响应帧：按 req_id 唤醒等待者
                fut = self._pending.pop(req_id, None)
                if fut and not fut.done():
                    fut.set_result(frame)
                continue

            self._msg_count += 1
            self._last_msg_time = time.time()
            self._last_msg_type = cmd

            msgid = str(body.get("msgid", ""))
            if msgid:
                if msgid in self._seen_msgids:
                    logger.info("WeCom duplicate msgid %s dropped", msgid)
                    continue
                self._seen_msgids.append(msgid)

            if cmd == "aibot_msg_callback":
                if self._on_message:
                    await self._on_message(body, req_id)
            elif cmd == "aibot_event_callback":
                if self._on_event:
                    await self._on_event(body, req_id)
            else:
                logger.debug("WeCom unknown cmd %s", cmd)

    # ---- request/response ----

    async def request(
        self, cmd: str, body: dict, timeout: float = 30.0, req_id: str = "",
    ) -> dict:
        """发送命令并等待同 req_id 的响应帧。

        respond 系列命令必须复用消息回调带来的 req_id（协议要求），
        此时传 req_id 参数；其余命令自动生成。同一 req_id 的并发请求
        会互相抢占 pending 槽位，调用方需串行 await。
        """
        if self._conn is None:
            raise ConnectionError("WeCom WS not connected")
        rid = req_id or uuid.uuid4().hex
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = json.dumps({"cmd": cmd, "headers": {"req_id": rid}, "body": body})
        try:
            await self._conn.send(payload)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise
        finally:
            self._pending.pop(rid, None)


def create_wecom_ws(
    on_message: Callable[[dict, str], Awaitable[None]],
    on_event: Callable[[dict, str], Awaitable[None]],
) -> WeComWsClient | None:
    if not settings.wecom_bot_id or not settings.wecom_secret:
        return None
    return WeComWsClient(
        settings.wecom_bot_id,
        settings.wecom_secret,
        on_message=on_message,
        on_event=on_event,
    )
