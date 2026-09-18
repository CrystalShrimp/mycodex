"""飞书事件层（薄适配）。

只做平台解析：消息事件 -> UserTarget + 文本 -> dispatch.commands；
卡片回调 -> dispatch.selections 语义动作。指令语义、会话、偏好与
Codex 调度全部在共享 dispatch 层。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
)

from app.channel.base import PLATFORM_FEISHU, UserTarget
from app.dispatch import commands, selections
from app.dispatch.helpers import log_task_failure
from app.dispatch.selections import SelectionError
from app.dispatch.sessions import group_chats, skey_for
from app.feishu.cards import CARD_CHAT_KEY
from app.feishu.client import feishu_client

logger = logging.getLogger("mycodex.events")


def _parse_message_text(content: str) -> str:
    try:
        data = json.loads(content)
        return data.get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return content.strip()


async def _refetch_bot_open_id() -> None:
    try:
        await feishu_client.fetch_bot_open_id()
    except Exception as e:
        logger.warning("Refetch bot open_id failed: %s", e)


def _is_bot_mentioned(msg, raw_text: str) -> bool:
    """群聊消息是否 @ 了机器人。

    首选：事件 mentions 里出现机器人自身 open_id；
    兜底：open_id 尚未取到时，只要文本带任意 @_user_N 提及就放行，
    并后台补取 open_id 供后续精确判定。
    """
    bot_open_id = feishu_client.bot_open_id
    if not bot_open_id:
        asyncio.get_running_loop().create_task(_refetch_bot_open_id())
        return bool(re.search(r"@_user_\d+", raw_text))
    for m in getattr(msg, "mentions", None) or []:
        m_id = getattr(m, "id", None)
        if m_id is None:
            continue
        mention_open_id = getattr(m_id, "open_id", "") or ""
        if not mention_open_id and isinstance(m_id, dict):
            mention_open_id = m_id.get("open_id", "")
        if mention_open_id == bot_open_id:
            return True
    return False


def on_message_receive(event: P2ImMessageReceiveV1) -> None:
    if not event.event:
        logger.debug("on_message_receive: no event payload")
        return
    msg = event.event.message
    sender = event.event.sender
    if not msg or not sender or not sender.sender_id:
        logger.debug("on_message_receive: missing msg/sender")
        return
    if msg.message_type != "text":
        return
    open_id = sender.sender_id.open_id or ""
    chat_id = msg.chat_id or ""
    message_id = msg.message_id or ""
    text = _parse_message_text(msg.content or "{}")
    if not text:
        return
    chat_type = getattr(msg, "chat_type", "") or ""
    is_group = chat_type == "group"
    # 先登记群 chat_id（含未 @ 机器人的消息），卡片回调据此路由回群
    if is_group and chat_id:
        group_chats.register(chat_id)
    # 群聊仅 @机器人 时才响应；私聊不受影响
    if is_group and not _is_bot_mentioned(msg, text):
        logger.info("From %s (group, not @bot, ignored): %s", open_id, text[:100])
        return
    # 群聊里 @机器人 的消息带 @_user_N 提及标记，剥掉再当指令/提示词
    text = re.sub(r"@_user_\d+", "", text).strip()
    if not text:
        return
    logger.info("From %s (%s): %s", open_id, chat_type or "p2p", text[:100])
    target = UserTarget(
        platform=PLATFORM_FEISHU,
        user_id=open_id,
        chat_id=chat_id if is_group else "",
        is_group=is_group,
    )
    task = asyncio.get_running_loop().create_task(
        commands.handle_message(target, message_id, text)
    )
    task.add_done_callback(log_task_failure)


def _toast(toast_type: str, content: str) -> P2CardActionTriggerResponse:
    """Build responses exactly as shown in the official Feishu Python sample."""
    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = toast_type
    toast.content = content
    resp.toast = toast
    return resp


def _build_done_card(title: str, color: str, approval_id: str) -> object:
    """Build a minimal card that replaces the approval card after decision."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackCard
    card = CallBackCard()
    card.type = "raw"
    card.data = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"审批ID: `{approval_id}`"}},
        ],
    }
    return card


def on_card_action(event: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    if not event.event:
        return P2CardActionTriggerResponse()
    action = event.event.action
    operator = event.event.operator
    if not action or not action.value or not operator:
        return P2CardActionTriggerResponse()

    card_type = action.value.get("type", "")
    act = action.value.get("act", "")
    open_id = operator.open_id or ""
    # 来源会话还原，优先级：卡片自带 _chat 标记 > 回调 context.open_chat_id >
    # 群聊注册表。实测飞书 WS 卡片回调不带 context.open_chat_id，所以群卡片
    # 在发送时已由 stamp_card_chat 注入 _chat 标记，这里读回。
    ctx = getattr(event.event, "context", None)
    card_chat_id = (getattr(ctx, "open_chat_id", "") or "").strip()
    stamped_chat = str(action.value.get(CARD_CHAT_KEY, "") or "").strip()
    if stamped_chat:
        card_chat_id = stamped_chat
        group_chats.register(stamped_chat)
        is_group = True
    else:
        is_group = group_chats.is_group(card_chat_id)
    chat_id = card_chat_id if is_group else ""
    target = UserTarget(
        platform=PLATFORM_FEISHU,
        user_id=open_id,
        chat_id=chat_id,
        is_group=is_group,
    )
    skey = skey_for(target)
    logger.info(
        "Card action: operator=%s type=%s act=%s option=%r form_keys=%s chat=%s group=%s",
        open_id,
        card_type,
        act,
        action.option,
        sorted((action.form_value or {}).keys()),
        chat_id or "(p2p)",
        is_group,
    )

    # 工作区选择、确认与返回
    if card_type == "workspace_select":
        try:
            if act in ("pre_switch", "pre_create"):
                target_path = ""
                if act == "pre_switch":
                    target_path = action.option or ""
                elif act == "pre_create":
                    if action.form_value:
                        target_path = action.form_value.get("workspace_path") or ""
                    if not target_path:
                        target_path = action.option or ""
                selections.handle_workspace_pre(target, skey, act, target_path)
                return P2CardActionTriggerResponse({})
            elif act == "confirm_switch":
                selections.handle_workspace_confirm(
                    target, skey, action.value.get("path", ""),
                )
                return P2CardActionTriggerResponse({})
            elif act == "cancel_switch":
                selections.handle_workspace_cancel(target)
                return P2CardActionTriggerResponse({})
        except SelectionError as e:
            return _toast("error", e.message)

    # 文件发送卡片动作
    if card_type == "file_send_select" and act == "send_file":
        target_path = action.option or ""
        if not target_path and action.value:
            target_path = action.value.get("path", "")
        try:
            msg = selections.handle_file_send(target, target_path)
        except SelectionError as e:
            return _toast("error", e.message)
        return _toast("info", msg)

    approval_id = action.value.get("approval_id", "")
    act = action.value.get("act", "")

    # Mode selection card handler
    if card_type == "mode_switch" and act == "switch_mode":
        msg = selections.handle_mode_switch(target, action.value.get("mode", ""))
        return _toast("info", msg)

    # Codex model selection card
    if card_type == "model_selection" and act == "switch_model":
        msg = selections.handle_model_switch(target, skey, action.value.get("model", ""))
        return _toast("info", msg)

    # Reasoning effort selection card
    if card_type == "effort_selection" and act == "switch_effort":
        msg = selections.handle_effort_switch(target, action.value.get("effort", ""))
        return _toast("info", msg)

    # Session selection card (/session)
    if card_type == "session_select" and act == "resume_session":
        target_tid = (action.option or "").strip()
        if not target_tid and action.value:
            target_tid = action.value.get("thread_id", "").strip()
        if not target_tid and action.value:
            target_tid = action.value.get("session_id", "").strip()
        try:
            msg = selections.handle_session_resume(target, skey, target_tid)
        except SelectionError as e:
            return _toast("error", e.message)
        return _toast("success", msg)

    # Reuse-last-settings card after /cd
    if card_type == "reuse_confirm":
        reuse = act == "reuse_yes"
        msg = selections.handle_reuse_confirm(target, skey, reuse)
        return _toast("success" if reuse else "info", msg)

    # Workspace config reuse card handler
    if card_type == "workspace_config_reuse":
        reuse = act == "reuse_yes"
        msg = selections.handle_ws_config_reuse(target, skey, reuse)
        return _toast("success" if reuse else "info", msg)

    # 审批卡 允许/拒绝（回调内同步改卡）
    approved = act == "approve"
    card_label = "工具执行" if card_type == "tool_execution" else "审批"
    msg = selections.decide_approval(target, approval_id, approved)

    resp = _toast("success" if approved else "error", msg)
    resp.card = _build_done_card(
        title=f"{'✅' if approved else '❌'} {card_label}已{'允许' if approved else '拒绝'}",
        color="green" if approved else "red",
        approval_id=approval_id,
    )
    return resp
