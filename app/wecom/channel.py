"""企微 Channel 实现：流式进度 + 模板卡片审批 + 编号列表降级交互。"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.channel.base import (
    ApprovalUI,
    ProgressSnap,
    Selection,
    UserTarget,
)
from app.wecom import cards as wc
from app.wecom.client import WeComClient, new_stream_id
from app.wecom.ws import WeComWsClient
from config.settings import settings

logger = logging.getLogger("mycodex.wecom.channel")

# 企微硬限制：会话 30 条/分钟；流式消息 10 分钟内必须 finish
_STREAM_MIN_INTERVAL = 3.0
_STREAM_RATE_LIMIT = 30
_STREAM_RATE_WINDOW = 60.0
_STREAM_ROLL_AFTER = 570.0  # 9.5min 主动换流，留 30s 余量

# 编号降级交互的 pending 登记表：chat_key -> (kind, payload, options, expires)
pending_numbered: dict[str, dict] = {}
_PENDING_TTL = 300.0


def chat_key_of(target: UserTarget) -> str:
    return target.chat_id if (target.is_group and target.chat_id) else target.user_id


class WeComProgressHandle:
    """一条任务消息的流式进度句柄。

    同一 stream.id 推送刷新、finish=true 结束；10 分钟硬超时前自动换流
    续传；30 条/分钟滑动窗口限流，超限丢弃中间帧（终态不受限流保护跳过，
    但也不强行突破平台限制）。加入内容级去重与 3.0s 节流以防客户端闪烁。
    """

    def __init__(self, client: WeComClient, req_id: str, fallback: "WeComChannel", target: UserTarget) -> None:
        self._client = client
        self._req_id = req_id
        self._fallback = fallback
        self._target = target
        self._stream_id = new_stream_id()
        self._stream_started = time.monotonic()
        self._last_push = 0.0
        self._window: deque[float] = deque()
        self._last_status = ""
        self._last_content = ""
        self._finished = False

    def _rate_ok(self, now: float) -> bool:
        while self._window and now - self._window[0] > _STREAM_RATE_WINDOW:
            self._window.popleft()
        return len(self._window) < _STREAM_RATE_LIMIT

    async def _push(self, content: str, *, finish: bool) -> None:
        now = time.monotonic()
        # 换流：10 分钟硬超时前结束当前流、开新流续传
        if now - self._stream_started > _STREAM_ROLL_AFTER and not finish:
            try:
                await self._client.respond_stream(
                    self._req_id, self._stream_id,
                    "⏳ 任务仍在进行，进度转下一条刷新…", finish=True,
                )
            except Exception:
                pass
            self._stream_id = new_stream_id()
            self._stream_started = now
        resp = await self._client.respond_stream(
            self._req_id, self._stream_id, content, finish=finish,
        )
        self._window.append(time.monotonic())
        err = resp.get("errcode", -1)
        if err != 0:
            raise RuntimeError(f"stream push errcode={err}: {resp.get('errmsg')}")

    async def _push_with_fallback(self, content: str, *, finish: bool) -> None:
        try:
            await self._push(content, finish=finish)
        except Exception as e:
            # req_id 过期（>24h）或流失效：降级为主动推送 markdown
            logger.warning("WeCom stream push failed (%s), fallback to markdown", e)
            try:
                await self._fallback._push_markdown(self._target, content)
            except Exception as e2:
                logger.debug("WeCom fallback markdown also failed: %s", e2)

    async def update(self, snap: ProgressSnap) -> None:
        if self._finished:
            return
        now = time.monotonic()
        content = wc.render_progress_text(snap)
        # 内容级去重：若渲染文本与上一帧完全一致，且未到达换流硬超时，直接跳过
        need_roll = now - self._stream_started > _STREAM_ROLL_AFTER
        if content == self._last_content and not need_roll:
            return
        # 最小间隔节流：两次推送至少间隔 3.0 秒
        if now - self._last_push < _STREAM_MIN_INTERVAL:
            return
        if not self._rate_ok(now):
            return
        try:
            await self._push_with_fallback(content, finish=False)
            self._last_push = now
            self._last_status = snap.status
            self._last_content = content
        except Exception as e:
            logger.debug("WeCom progress update failed (non-fatal): %s", e)

    async def finish(self, snap: ProgressSnap) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            content = wc.render_progress_text(snap)
            await self._push_with_fallback(content, finish=True)
            self._last_content = content
        except Exception as e:
            logger.debug("WeCom progress finish failed (non-fatal): %s", e)

    async def error(self, title: str, detail: str) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            await self._push_with_fallback(wc.render_error_text(title, detail), finish=True)
        except Exception as e:
            logger.debug("WeCom progress error failed (non-fatal): %s", e)


class WeComChannel:
    name = "wecom"
    max_file_mb = 20  # 企微临时素材普通文件上限

    def __init__(self) -> None:
        self._ws: WeComWsClient | None = None
        self._client: WeComClient | None = None
        # chat_key -> 最近一次消息回调 req_id（respond 24h 窗口内有效）
        self._req_ids: dict[str, str] = {}

    def bind(self, ws: WeComWsClient, client: WeComClient) -> None:
        self._ws = ws
        self._client = client

    def note_req_id(self, chat_key: str, req_id: str) -> None:
        self._req_ids[chat_key] = req_id

    def _req_id_for(self, target: UserTarget) -> str:
        return self._req_ids.get(chat_key_of(target), "")

    # ---- 发送（回复优先，无 req_id 时主动推送 markdown） ----

    async def _respond_or_push(
        self, target: UserTarget, respond, content: str,
    ) -> None:
        if self._client is None:
            raise RuntimeError("WeCom channel not bound")
        req_id = self._req_id_for(target)
        if req_id and self._client is not None:
            try:
                resp = await respond(req_id, content)
                if resp.get("errcode", -1) == 0:
                    return
                logger.warning("WeCom respond errcode=%s, fallback push", resp.get("errcode"))
            except Exception as e:
                logger.warning("WeCom respond failed (%s), fallback push", e)
        await self._push_markdown(target, content)

    async def _push_markdown(self, target: UserTarget, content: str) -> None:
        if self._client is None:
            raise RuntimeError("WeCom channel not bound")
        chat_type = 2 if (target.is_group and target.chat_id) else 1
        chatid = target.chat_id if chat_type == 2 else target.user_id
        resp = await self._client.send_markdown(chatid, chat_type, content)
        if resp.get("errcode", -1) != 0:
            logger.warning("WeCom push markdown errcode=%s: %s", resp.get("errcode"), resp.get("errmsg"))

    async def send_text(self, target: UserTarget, text: str) -> Any:
        # 企微 aibot_send_msg 不支持纯文本，主动推送统一走 markdown；
        # 回复优先（text 类型保留 \n 原样显示）
        req_id = self._req_id_for(target)
        if req_id and self._client is not None:
            try:
                stream_id = new_stream_id()
                resp = await self._client.respond_stream(req_id, stream_id, text, finish=True)
                if resp.get("errcode", -1) == 0:
                    return resp
                logger.warning("WeCom respond_stream errcode=%s, fallback push", resp.get("errcode"))
            except Exception as e:
                logger.warning("WeCom respond_stream failed (%s), fallback push", e)
        # markdown 里连续换行会被折叠，转成每行一条
        md = text.replace("\n\n", "\n\n&nbsp;\n")
        await self._push_markdown(target, md)
        return None

    async def _send_card(self, target: UserTarget, card: dict) -> None:
        if self._client is None:
            raise RuntimeError("WeCom channel not bound")
        req_id = self._req_id_for(target)
        if req_id:
            try:
                resp = await self._client.respond_template_card(req_id, card)
                if resp.get("errcode", -1) == 0:
                    return
                logger.warning("WeCom respond card errcode=%s, fallback push", resp.get("errcode"))
            except Exception as e:
                logger.warning("WeCom respond card failed (%s), fallback push", e)
        chat_type = 2 if (target.is_group and target.chat_id) else 1
        chatid = target.chat_id if chat_type == 2 else target.user_id
        resp = await self._client.send_template_card(chatid, chat_type, card)
        if resp.get("errcode", -1) != 0:
            logger.warning("WeCom push card errcode=%s: %s", resp.get("errcode"), resp.get("errmsg"))

    # ---- Channel 协议 ----

    async def send_approval(self, target: UserTarget, approval: ApprovalUI) -> None:
        card = wc.approval_card(
            approval.approval_id, approval.tool_name,
            approval.tool_input, approval.session_id,
        )
        await self._send_card(target, card)

    async def open_progress(self, target: UserTarget, snap: ProgressSnap) -> WeComProgressHandle:
        if self._client is None:
            raise RuntimeError("WeCom channel not bound")
        req_id = self._req_id_for(target)
        handle = WeComProgressHandle(self._client, req_id, self, target)
        # 首帧立即推送（建立流式消息）
        try:
            content = wc.render_progress_text(snap)
            await handle._push_with_fallback(content, finish=False)
            handle._last_content = content
            handle._last_push = time.monotonic()
            handle._last_status = snap.status
        except Exception as e:
            logger.warning("WeCom open_progress first push failed: %s", e)
        return handle

    async def send_file(self, target: UserTarget, file_path: Path) -> None:
        if self._client is None:
            raise RuntimeError("WeCom channel not bound")
        req_id = self._req_id_for(target)
        if not req_id:
            raise RuntimeError("企微发送文件需要先在会话中触发消息（无可用回复上下文）")
        media_id = await self._client.upload_media(file_path)
        resp = await self._client.respond_file(req_id, media_id)
        if resp.get("errcode", -1) != 0:
            raise RuntimeError(f"send file errcode={resp.get('errcode')}: {resp.get('errmsg')}")

    async def send_selection(self, target: UserTarget, sel: Selection) -> None:
        raise NotImplementedError

    async def is_allowed(self, target: UserTarget) -> bool:
        allowed_chats = settings.get_wecom_allowed_chats()
        allowed_users = settings.get_wecom_allowed_users()

        # 1. 公用模式：群白名单与用户白名单均为空 -> 企微全员开放
        if not allowed_chats and not allowed_users:
            return True

        # 2. 特定群模式（方案 A）：配置了 WECOM_ALLOWED_CHATS
        if allowed_chats:
            if target.is_group and target.chat_id in allowed_chats:
                # 授权群内的所有成员直接放行，并自动收录进 WECOM_ALLOWED_USERS 使其单聊也可用
                if target.user_id and target.user_id not in allowed_users:
                    if settings.record_wecom_allowed_user(target.user_id):
                        logger.info("WeCom auto-recorded allowed user %s from group %s", target.user_id, target.chat_id)
                return True
            # 单聊或非授权群 -> 仅已收录进 WECOM_ALLOWED_USERS 的成员可用
            return target.user_id in allowed_users

        # 3. 个人模式 / 指定成员模式：仅 WECOM_ALLOWED_USERS 名单内 userid 可用
        return target.user_id in allowed_users

    # ---- 富视图 ----

    async def send_view(self, target: UserTarget, kind: str, payload: dict) -> None:
        key = chat_key_of(target)

        def _numbered(title: str, options: list[tuple[str, str]], footer: str = "") -> None:
            pending_numbered[key] = {
                "kind": kind,
                "payload": payload,
                "options": options,
                "expires": time.time() + _PENDING_TTL,
            }

        prefix = f"{payload['prefix_ack']}\n\n" if payload.get("prefix_ack") else ""

        if kind == "help":
            await self._respond_or_push(target, self._client.respond_markdown, wc.help_markdown())
        elif kind == "model_selection":
            models: dict = payload.get("models", {})
            current_m = payload.get("current_model", "")
            title = payload.get("step_title") or "选择 Codex 模型 (Model)"
            options = [
                (slug, f"{info.get('label', slug)}（`{slug}`）{' [当前]' if slug == current_m else ''}")
                for slug, info in models.items()
            ]
            _numbered(title, options[:20])
            await self._respond_or_push(
                target, self._client.respond_markdown,
                prefix + wc.numbered_markdown(title, options[:20], "或直接发送 `/model <slug>`"),
            )
        elif kind == "effort_selection":
            current_e = payload.get("current_effort", "")
            title = payload.get("step_title") or "选择推理强度 (Effort)"
            effort_items = [
                ("low", "⚡ `low` — 轻量 / 快速"),
                ("medium", "⚙️ `medium` — 标准 / 推荐"),
                ("high", "🧠 `high` — 深度思考"),
                ("xhigh", "🔥 `xhigh` — 超高深度"),
            ]
            options = [
                (lv, f"{desc}{' [当前]' if lv == current_e else ''}")
                for lv, desc in effort_items
            ]
            _numbered(title, options)
            await self._respond_or_push(
                target, self._client.respond_markdown,
                prefix + wc.numbered_markdown(title, options, "或直接发送 `/effort <low|medium|high|xhigh>`"),
            )
        elif kind == "mode_selection":
            active_m = payload.get("active_mode", "")
            title = payload.get("step_title") or "选择执行模式 (Mode)"
            mode_items = [
                ("h", "🛡️ `严格模式 (h)` — 只读沙箱"),
                ("m", "⚖️ `平衡模式 (m)` — 工作区可写（推荐）"),
                ("l", "⚡ `全自动模式 (l)` — 完全权限"),
            ]
            options = [
                (code, f"{desc}{' [当前]' if code == active_m else ''}")
                for code, desc in mode_items
            ]
            _numbered(title, options)
            await self._respond_or_push(
                target, self._client.respond_markdown,
                prefix + wc.numbered_markdown(title, options, "或直接发送 `/mode <h|m|l>`"),
            )
        elif kind == "session_selection":
            sessions: list = payload.get("sessions", [])
            options = []
            for s in sessions[:15]:
                mtime = s.get("mtime", 0)
                try:
                    ts = time.strftime("%m-%d %H:%M", time.localtime(mtime)) if mtime else "?"
                except Exception:
                    ts = "?"
                options.append((s.get("thread_id", ""), f"{s.get('last_summary', '')} · {ts}"))
            _numbered("选择要恢复的会话", options, footer="或直接发送 `/resume <thread_id>`")
            await self._respond_or_push(
                target, self._client.respond_markdown,
                wc.numbered_markdown("选择要恢复的会话", options, "或直接发送 `/resume <thread_id>`"),
            )
        elif kind == "file_selection":
            files: list = payload.get("files", [])
            options = [(f.get("abs_path", ""), f"{f.get('rel_path', '')} ({f.get('size_str', '')})") for f in files[:20]]
            _numbered("选择要发送的文件", options)
            await self._respond_or_push(
                target, self._client.respond_markdown,
                wc.numbered_markdown(f"选择要发送的文件（{payload.get('workspace', '')}）", options[:20]),
            )
        elif kind == "workspace_selection":
            projects: list = payload.get("projects", [])
            options = [(p, f"`{p}`") for p in projects[:15]]
            _numbered("选择工作区", options, footer="或直接发送 `/cd <绝对路径>` 新建/切换")
            await self._respond_or_push(
                target, self._client.respond_markdown,
                wc.numbered_markdown(f"选择工作区（当前: {payload.get('current', '')}）", options[:15], "或直接发送 `/cd <绝对路径>`"),
            )
        elif kind == "cd_confirm":
            _numbered("确认工作区切换", [
                (payload.get("target_path", ""), f"✅ 确认切换到 `{payload.get('target_path', '')}`"),
                ("", "↩️ 取消"),
            ])
            new_tag = "（新建）" if payload.get("is_new") else ""
            desc = (
                f"目标: `{payload.get('target_path', '')}`{new_tag}\n"
                f"Git: {payload.get('git_branch', '-')} | AGENTS.md: {payload.get('agents_md', '-')}"
            )
            if payload.get("warning_running"):
                desc += "\n⚠️ 当前有任务正在运行，切换将中断它"
            await self._respond_or_push(
                target, self._client.respond_markdown,
                wc.numbered_markdown("确认工作区切换", [
                    (payload.get("target_path", ""), "✅ 确认切换"),
                    ("", "↩️ 取消"),
                ], desc),
            )
        elif kind == "reuse_last":
            desc = f"模型 `{payload.get('model_label', '-')}` | 推理强度 `{payload.get('effort', '-')}` | 模式 `{payload.get('mode', '-')}`"
            opts = [("yes", "✅ 沿用上次设置"), ("no", "🔄 重新选择配置")]
            _numbered("沿用上次运行设置？", opts, desc)
            await self._respond_or_push(
                target, self._client.respond_markdown,
                wc.numbered_markdown("沿用上次运行设置？", opts, desc),
            )
        elif kind == "ws_config_reuse":
            desc = f"模型 `{payload.get('model_label', '-')}` | 推理强度 `{payload.get('effort', '-')}` | 模式 `{payload.get('mode', '-')}`"
            opts = [("yes", "✅ 沿用该工作区配置"), ("no", "🔄 重新选择配置")]
            _numbered("该工作区已有完整配置，是否沿用？", opts, desc)
            await self._respond_or_push(
                target, self._client.respond_markdown,
                wc.numbered_markdown("该工作区已有完整配置，是否沿用？", opts, desc),
            )
        else:
            raise ValueError(f"Unknown view kind: {kind}")

    # ---- 生命周期 / 诊断 ----

    def health(self) -> dict:
        return self._ws.health() if self._ws else {"connected": False, "error": "not started"}
