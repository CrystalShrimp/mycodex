"""飞书 Channel 实现：包装 FeishuClient + 卡片构建。

进度展示沿用"单卡 running → 终态"设计：open_progress 先发一张 running
卡拿到 message_id，之后整卡 PATCH。0.5s 节流（状态变化立即刷、告警
事件 2s 节流）沿用 cli_loop 时代的原始参数。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from app.channel.base import (
    ApprovalUI,
    ProgressSnap,
    Selection,
    UserTarget,
)
from app.feishu.cards import build_progress_card
from app.feishu.client import feishu_client
from config.settings import settings

logger = logging.getLogger("mycodex.feishu.channel")

# groups 模式：群成员列表缓存 chat_id -> (monotonic 时间, 成员集合)
_group_member_cache: dict[str, tuple[float, set[str]]] = {}
_GROUP_CACHE_TTL = 300.0


class FeishuProgressHandle:
    """一张飞书进度卡的刷新句柄（整卡 PATCH + 节流）。"""

    def __init__(self, card_id: str) -> None:
        self._card_id = card_id
        self._last_patch_at = 0.0
        self._last_status = ""

    def _should_skip(self, snap: ProgressSnap, now: float) -> bool:
        if snap.status != self._last_status:
            return False
        # 状态未变时节流：告警风暴 2s，其余 0.5s
        interval = 2.0 if snap.warning_event else 0.5
        return now - self._last_patch_at < interval

    async def update(self, snap: ProgressSnap) -> None:
        if not self._card_id:
            return
        now = asyncio.get_event_loop().time()
        if self._should_skip(snap, now):
            return
        try:
            card = build_progress_card(
                snap.model,
                snap.status,
                step=snap.step,
                tool_counts=snap.tool_counts,
                elapsed_s=snap.elapsed_s,
                warnings=snap.warnings,
                current_tool=snap.current_tool,
                current_tool_args=snap.current_tool_args,
                last_text=snap.last_text if snap.status == "running" else "",
                last_warning=snap.last_warning if snap.status != "running" else "",
            )
            await feishu_client.update_card(self._card_id, card)
            self._last_patch_at = now
            self._last_status = snap.status
        except Exception as e:
            logger.debug("Progress card PATCH failed (non-fatal): %s", e)

    async def finish(self, snap: ProgressSnap) -> None:
        if not self._card_id:
            return
        try:
            final_card = build_progress_card(
                snap.model,
                snap.status,
                step=snap.step,
                tool_counts=snap.tool_counts,
                elapsed_s=snap.elapsed_s,
                warnings=snap.warnings,
                result_text=snap.result_text,
                input_tokens=snap.input_tokens,
                output_tokens=snap.output_tokens,
                error=snap.error,
                thread_id=snap.session_id,
                task_id=snap.task_id,
            )
            await feishu_client.update_card(self._card_id, final_card)
        except Exception as e:
            logger.debug("Final card PATCH failed (non-fatal): %s", e)

    async def error(self, title: str, detail: str) -> None:
        if not self._card_id:
            return
        try:
            from app.feishu.cards import build_error_card

            await feishu_client.update_card(
                self._card_id, build_error_card(title, detail[:3500]),
            )
        except Exception as e:
            logger.debug("Error card PATCH failed (non-fatal): %s", e)


class FeishuChannel:
    name = "feishu"
    max_file_mb = 30  # 飞书文件消息上限

    async def send_text(self, target: UserTarget, text: str) -> Any:
        if target.is_group and target.chat_id:
            return await feishu_client.send_text(target.chat_id, text, is_chat=True)
        return await feishu_client.send_text(target.user_id, text)

    async def send_card(self, target: UserTarget, card: dict) -> Any:
        """发原生飞书卡片（群卡片注入 _chat 标记用于回调路由回群）。"""
        if target.is_group and target.chat_id:
            from app.feishu.cards import stamp_card_chat

            stamp_card_chat(card, target.chat_id)
            return await feishu_client.send_card(target.chat_id, card, is_chat=True)
        return await feishu_client.send_card(target.user_id, card)

    async def open_progress(self, target: UserTarget, snap: ProgressSnap) -> FeishuProgressHandle:
        card = build_progress_card(snap.model, "running")
        card_msg = await self.send_card(target, card)
        card_id = card_msg.get("data", {}).get("message_id", "") if card_msg else ""
        return FeishuProgressHandle(card_id)

    async def send_selection(self, target: UserTarget, sel: Selection) -> None:
        # 选择视图已由 send_view 的 kind 体系覆盖，此方法保留给协议兼容
        raise NotImplementedError

    async def send_file(self, target: UserTarget, file_path: Path) -> None:
        file_key = await feishu_client.upload_file(file_path)
        if target.is_group and target.chat_id:
            await feishu_client.send_file(target.chat_id, file_key, is_chat=True)
        else:
            await feishu_client.send_file(target.user_id, file_key)

    async def send_view(self, target: UserTarget, kind: str, payload: dict) -> None:
        from app.feishu import cards as fc

        if kind == "workspace_selection":
            card = fc.build_cd_selection_card(payload["current"], payload["projects"])
        elif kind == "cd_confirm":
            card = fc.build_cd_confirm_card(
                target_path=payload["target_path"],
                is_new=payload["is_new"],
                git_branch=payload["git_branch"],
                agents_md=payload["agents_md"],
                warning_running=payload["warning_running"],
            )
        elif kind == "model_selection":
            card = fc.build_model_selection_card(
                approval_id=payload.get("approval_id", ""),
                models=payload.get("models", {}),
                current_model=payload.get("current_model", ""),
            )
        elif kind == "effort_selection":
            card = fc.build_effort_selection_card(
                approval_id=payload.get("approval_id", ""),
                current_effort=payload.get("current_effort", ""),
            )
        elif kind == "mode_selection":
            card = fc.build_mode_selection_card(
                approval_id=payload.get("approval_id", ""),
                active_mode=payload.get("active_mode", ""),
            )
        elif kind == "session_selection":
            card = fc.build_session_selection_card(
                payload["workspace"], payload["sessions"],
                current_thread_id=payload.get("current_thread_id", ""),
            )
        elif kind == "file_selection":
            card = fc.build_file_selection_card(payload["workspace"], payload["files"])
        elif kind == "help":
            card = fc.build_help_card()
        elif kind == "reuse_last":
            card = fc.build_reuse_last_card(
                approval_id=payload["approval_id"],
                model_label=payload["model_label"],
                effort=payload["effort"],
                mode=payload["mode"],
            )
        elif kind == "ws_config_reuse":
            card = fc.build_workspace_config_reuse_card(
                approval_id=payload["approval_id"],
                workspace=payload["workspace"],
                model_label=payload["model_label"],
                effort=payload["effort"],
                mode=payload["mode"],
                action_type=payload.get("action_type", "switch"),
            )
        else:
            raise ValueError(f"Unknown view kind: {kind}")
        await self.send_card(target, card)

    # ---- 白名单（groups 模式需查飞书群成员，其余模式纯配置判定）----

    def _is_user_allowed(self, open_id: str) -> bool:
        """同步名单判定（list/legacy/org/creator 四种模式无需联网）。"""
        mode = settings.get_allowed_mode()
        if mode == "org":
            return True
        if mode == "creator":
            return bool(settings.allowed_creator.strip()) and open_id == settings.allowed_creator.strip()
        # list / 默认模式：飞书白名单为空 = 飞书全员开放；非空时仅名单内 open_id 可用
        feishu_allowed = settings.get_allowed_users_for("feishu")
        if not feishu_allowed:
            return True
        return open_id in feishu_allowed

    async def is_allowed(self, target: UserTarget) -> bool:
        open_id = target.user_id
        if settings.get_allowed_mode() != "groups":
            return self._is_user_allowed(open_id)
        groups = settings.get_allowed_group_ids()
        if target.is_group:
            return target.chat_id in groups
        # 私聊：发起人必须是任一白名单群的成员（缓存 5 分钟）
        now = time.monotonic()
        for gid in groups:
            cached = _group_member_cache.get(gid)
            if cached and now - cached[0] < _GROUP_CACHE_TTL:
                if open_id in cached[1]:
                    return True
                continue
            members = await feishu_client.list_group_members(gid)
            if members is None:
                continue  # API 失败（如缺权限）：fail-closed，跳过该群
            _group_member_cache[gid] = (now, members)
            if open_id in members:
                return True
        return False
