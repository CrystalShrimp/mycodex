"""企微回调事件层（薄适配）。

aibot_msg_callback -> 剥 @ -> dispatch.commands（或编号降级选择）
aibot_event_callback -> template_card_event 按钮解析 -> dispatch.selections
"""
from __future__ import annotations

import asyncio
import logging
import re

from app.channel.base import PLATFORM_WECOM, UserTarget
from app.dispatch import commands, selections
from app.dispatch.helpers import log_task_failure
from app.dispatch.selections import SelectionError
from app.dispatch.sessions import skey_for
from app.wecom.channel import WeComChannel, chat_key_of, pending_numbered

logger = logging.getLogger("mycodex.wecom.events")

_CHANNEL: WeComChannel | None = None


def bind_channel(channel: WeComChannel) -> None:
    global _CHANNEL
    _CHANNEL = channel


def _strip_at_mention(text: str) -> str:
    """群聊 @机器人 的消息带 "@机器人名 " 前缀，剥掉再当指令/提示词。

    机器人名从管理台配置无法经 API 获取，按"首个 @token"剥离。
    """
    return re.sub(r"^@\S+\s*", "", text.strip()).strip()


async def on_wecom_message(body: dict, req_id: str) -> None:
    if _CHANNEL is None:
        return
    if body.get("msgtype") != "text":
        # 与飞书现状对齐：只处理文本
        return
    userid = ((body.get("from") or {}).get("userid")) or ""
    chatid = body.get("chatid") or ""
    chattype = body.get("chattype") or "single"
    is_group = chattype == "group"
    if not userid:
        return
    text = ((body.get("text") or {}).get("content") or "").strip()
    if is_group:
        text = _strip_at_mention(text)
    if not text:
        return

    target = UserTarget(
        platform=PLATFORM_WECOM,
        user_id=userid,
        chat_id=chatid if is_group else "",
        is_group=is_group,
    )
    _CHANNEL.note_req_id(chat_key_of(target), req_id)
    logger.info("WeCom from %s (%s): %s", userid, chattype, text[:100])

    # 编号降级选择：纯数字回复命中 pending 列表
    if re.fullmatch(r"\d{1,2}", text):
        handled = await _try_numbered_pick(target, text)
        if handled:
            return

    task = asyncio.get_running_loop().create_task(
        commands.handle_message(target, body.get("msgid", ""), text)
    )
    task.add_done_callback(log_task_failure)


async def _try_numbered_pick(target: UserTarget, text: str) -> bool:
    key = chat_key_of(target)
    entry = pending_numbered.get(key)
    if not entry:
        return False
    import time as _time

    if _time.time() > entry.get("expires", 0):
        pending_numbered.pop(key, None)
        return False
    idx = int(text) - 1
    options = entry.get("options", [])
    if idx < 0 or idx >= len(options):
        return False
    value, _label = options[idx]
    kind = entry.get("kind", "")
    payload = entry.get("payload", {})
    pending_numbered.pop(key, None)

    channel = _CHANNEL
    assert channel is not None
    skey = skey_for(target)
    try:
        ack = await _run_selection(channel, target, skey, kind, payload, value)
    except SelectionError as e:
        await channel.send_text(target, f"❌ {e.message}")
        return True
    if ack:
        await channel.send_text(target, ack)
    return True


async def _run_selection(
    channel: WeComChannel,
    target: UserTarget,
    skey: str,
    kind: str,
    payload: dict,
    value: str,
) -> str | None:
    """执行一种编号选择，返回 ack 文案（None = 已发后续消息，无需 ack）。"""
    if kind == "workspace_selection":
        selections.handle_workspace_pre(target, skey, "pre_switch", value)
        return None
    if kind == "cd_confirm":
        if value:
            selections.handle_workspace_confirm(target, skey, value)
        else:
            selections.handle_workspace_cancel(target)
        return None
    if kind == "model_selection":
        return selections.handle_model_switch(target, skey, value)
    if kind == "session_selection":
        return selections.handle_session_resume(target, skey, value)
    if kind == "file_selection":
        return selections.handle_file_send(target, value)
    return f"未知选择类型: {kind}"


async def on_wecom_event(body: dict, req_id: str) -> None:
    if _CHANNEL is None:
        return
    event = body.get("event") or {}
    etype = event.get("eventtype", "")

    if etype == "disconnected_event":
        logger.warning("WeCom old connection kicked by new subscribe (expected if re-subscribed)")
        return

    if etype == "enter_chat":
        try:
            if _CHANNEL._client is not None:
                # 进入会话欢迎语必须在 5 秒内回复
                await _CHANNEL._client.respond_welcome(
                    req_id, "我是 MyCodex，发送 /help 查看指令，或直接发任务给我。",
                )
        except Exception as e:
            logger.debug("WeCom welcome failed: %s", e)
        return

    if etype == "template_card_event":
        await _handle_card_event(body, req_id)
        return

    logger.debug("WeCom unhandled event type: %s", etype)


async def _handle_card_event(body: dict, req_id: str) -> None:
    assert _CHANNEL is not None
    event = body.get("event") or {}
    # 按钮事件载荷字段名以真机回调为准（文档指向"接收事件"页），
    # 兼容 event_key / eventkey 两种写法
    raw_key = str(event.get("event_key") or event.get("eventkey") or "")
    userid = ((body.get("from") or {}).get("userid")) or ""
    chatid = body.get("chatid") or ""
    chattype = body.get("chattype") or "single"
    is_group = chattype == "group"

    target = UserTarget(
        platform=PLATFORM_WECOM,
        user_id=userid,
        chat_id=chatid if is_group else "",
        is_group=is_group,
    )
    _CHANNEL.note_req_id(chat_key_of(target), req_id)
    logger.info("WeCom card event: user=%s key=%r", userid, raw_key[:80])

    ack = _parse_and_dispatch(target, raw_key)
    if ack:
        try:
            await _CHANNEL.send_text(target, ack)
        except Exception as e:
            logger.warning("WeCom card ack failed: %s", e)


def _parse_and_dispatch(target: UserTarget, raw_key: str) -> str | None:
    """解析 mc:<动作>:<值>:<附属id> 按钮 key 并分发。"""
    parts = raw_key.split(":")
    if len(parts) < 3 or parts[0] != "mc":
        logger.debug("WeCom ignored unknown card key: %r", raw_key[:80])
        return None
    action, value = parts[1], parts[2]
    extra = parts[3] if len(parts) > 3 else ""
    skey = skey_for(target)

    if action == "ap":
        approved = value == "approve"
        return selections.decide_approval(target, extra, approved)
    if action == "md":
        return selections.handle_mode_switch(target, value)
    if action == "ef":
        return selections.handle_effort_switch(target, value)
    if action == "mo":
        return selections.handle_model_switch(target, skey, value)
    if action == "ru":
        return selections.handle_reuse_confirm(target, skey, value == "yes")
    if action == "ws":
        return selections.handle_ws_config_reuse(target, skey, value == "yes")
    logger.debug("WeCom unknown card action: %s", action)
    return None
