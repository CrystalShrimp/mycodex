"""通道注册表：platform -> Channel 实例。

消费者（cli_loop / hooks / dispatch）统一通过 ``get_channel(platform)``
拿通道，不直接 import 平台实现。飞书通道在模块导入时注册（无凭据时
构造也无副作用）；企微通道由 main.py 在具备凭据时注册（P5 接入）。
"""
from __future__ import annotations

from app.channel.base import Channel

_channels: dict[str, Channel] = {}


def register_channel(channel: Channel) -> None:
    _channels[channel.name] = channel


def get_channel(platform: str) -> Channel:
    channel = _channels.get(platform)
    if channel is None:
        raise KeyError(f"No channel registered for platform {platform!r}")
    return channel


def all_channels() -> dict[str, Channel]:
    return dict(_channels)


# 飞书通道进程内常驻注册（仅构造轻量对象，不发起网络请求）
from app.feishu.channel import FeishuChannel  # noqa: E402

register_channel(FeishuChannel())
