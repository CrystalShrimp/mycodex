"""ReplyContext：dispatch 层统一的回复门面（替代原 feishu ReplyChannel）。

text / file / view 三种回复按 target.platform 路由到对应通道，
卡片渲染（kind -> 平台原生卡片/markdown）由各平台 channel 实现负责。
"""
from __future__ import annotations

from pathlib import Path

from app.channel.base import UserTarget
from app.channel.registry import get_channel


class ReplyContext:
    """把回复路由到消息来源：群消息回群，私聊回私。"""

    def __init__(self, target: UserTarget) -> None:
        self.target = target
        self._channel = get_channel(target.platform)

    async def text(self, content: str):
        return await self._channel.send_text(self.target, content)

    async def view(self, kind: str, **payload) -> None:
        await self._channel.send_view(self.target, kind, payload)

    async def file(self, path: Path) -> None:
        await self._channel.send_file(self.target, path)
