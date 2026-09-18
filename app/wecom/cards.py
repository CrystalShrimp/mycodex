"""企微侧渲染：进度流式文案、审批/选择模板卡片、markdown 视图。"""
from __future__ import annotations

import json
import time
from typing import Any

from app.channel.base import ProgressSnap

# 按钮 key 编码：mc:<动作>:<值>:<附属id>
# 动作集：ap=审批 md=模式 lv=规格 pf=供应商 ru=沿用上次 ws=工作区配置


def approval_key(approval_id: str, approved: bool) -> str:
    return f"mc:ap:{'approve' if approved else 'reject'}:{approval_id}"


def _btn(action: str, value: str, text: str, style: int, extra: str = "") -> dict:
    return {
        "text": text,
        "style": style,
        "key": f"mc:{action}:{value}:{extra}",
    }


def render_progress_text(snap: ProgressSnap) -> str:
    """进度流式消息内容（纯文本，markdown 支持待真机验证后启用）。"""
    icon = {
        "running": "🔄", "retrying": "🔁",
        "completed": "✅", "failed": "❌", "cancelled": "⏹️",
    }.get(snap.status, "🔄")
    head = {
        "running": "任务执行中", "retrying": "API 重试中",
        "completed": "任务完成", "failed": "任务失败", "cancelled": "已取消",
    }.get(snap.status, snap.status)

    lines = [f"{icon} {head} | {snap.model or '-'} | {snap.elapsed_s:.0f}s"]

    if snap.tool_counts:
        tools = "  ".join(f"{k}×{v}" for k, v in list(snap.tool_counts.items())[:6])
        lines.append(f"工具: {tools} (共{sum(snap.tool_counts.values())}次)")
    if snap.current_tool:
        args = f" {snap.current_tool_args}" if snap.current_tool_args else ""
        lines.append(f"当前: {snap.current_tool}{args}")
    if snap.last_warning and snap.status in ("retrying", "failed"):
        lines.append(f"⚠️ {snap.last_warning}")
    if snap.warnings:
        lines.append(f"告警: {snap.warnings} 条")
    if snap.last_text and snap.status == "running":
        text = snap.last_text.strip().replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "…"
        lines.append(f"💭 {text}")

    if snap.status in ("completed", "failed", "cancelled"):
        if snap.result_text:
            result = snap.result_text.strip()
            if len(result) > 1500:
                result = result[:1500] + "\n…(过长已截断)"
            lines.append("—")
            lines.append(result)
        if snap.error:
            lines.append(f"错误: {snap.error[:300]}")
        tokens = []
        if snap.input_tokens:
            tokens.append(f"入{snap.input_tokens:,}")
        if snap.output_tokens:
            tokens.append(f"出{snap.output_tokens:,}")
        if tokens:
            lines.append(f"Token: {' / '.join(tokens)} | 任务 {snap.task_id}")
    return "\n".join(lines)


def render_error_text(title: str, detail: str) -> str:
    detail = (detail or "").strip()
    if len(detail) > 1200:
        detail = detail[:1200] + "…"
    return f"❌ {title}\n{detail}"


def approval_card(approval_id: str, tool_name: str, tool_input: dict, session_id: str) -> dict:
    args = json.dumps(tool_input, ensure_ascii=False) if isinstance(tool_input, dict) else str(tool_input)
    if len(args) > 600:
        args = args[:600] + "…"
    return {
        "card_type": "button_interaction",
        "source": {"desc": "MyCodex 工具审批"},
        "main_title": {"title": f"🛡️ 工具执行审批: {tool_name}"},
        "sub_title_text": args,
        "button_list": [
            _btn("ap", "approve", "✅ 允许", 1, approval_id),
            _btn("ap", "reject", "❌ 拒绝", 2, approval_id),
        ],
        "task_id": approval_id,
    }


def buttons_card(title: str, desc: str, buttons: list[dict], task_id: str = "") -> dict:
    return {
        "card_type": "button_interaction",
        "source": {"desc": "MyCodex"},
        "main_title": {"title": title},
        "sub_title_text": desc[:800] if desc else "",
        "button_list": buttons,
        "task_id": task_id or title,
    }


def mode_selection_card(approval_id: str, active_mode: str) -> dict:
    labels = {"h": "🛡️ 严格 (h)", "m": "⚖️ 平衡 (m)", "l": "⚡ 全自动 (l)"}
    return buttons_card(
        f"审批模式（当前: {labels.get(active_mode, '未设置')}）",
        "点击切换审批模式",
        [
            _btn("md", "h", labels["h"], 1 if active_mode != "h" else 3),
            _btn("md", "m", labels["m"], 1 if active_mode != "m" else 3),
            _btn("md", "l", labels["l"], 1 if active_mode != "l" else 3),
        ],
    )


def effort_selection_card(approval_id: str, current_effort: str) -> dict:
    labels = {
        "low": "⚡ low", "medium": "⚙️ medium", "high": "🧠 high",
        "xhigh": "🔥 xhigh", "max": "🚀 max",
    }
    order = ["low", "medium", "high", "xhigh", "max"]
    return buttons_card(
        f"推理强度（当前: {current_effort or '未设置'}）",
        "点击切换推理强度",
        [
            _btn("ef", lv, labels[lv], 1 if current_effort != lv else 3, approval_id)
            for lv in order
        ],
    )


def confirm_card(title: str, desc: str, yes_action: str, no_action: str, extra: str = "") -> dict:
    return buttons_card(
        title, desc,
        [
            _btn(yes_action, "yes", "✅ 确认", 1, extra),
            _btn(no_action, "no", "↩️ 取消/重选", 2, extra),
        ],
    )


# ---- markdown 视图 ----


def help_markdown() -> str:
    return (
        "# MyCodex 指令帮助\n"
        "**会话**: `/new` `/stop` `/continue` `/resume <id>` `/session` `/clean`\n"
        "**配置**: `/model` `/level` `/mode` `/reset`\n"
        "**工作区**: `/cd <绝对路径>` `/pwd` `/file`\n"
        "**工具**: `/status` `/mem` `/notes` `/sh` `/help`\n\n"
        "普通文本直接作为任务发给 Codex 执行。"
    )


def balance_markdown(result: Any) -> str:
    try:
        if isinstance(result, dict):
            lines = ["# API 余额查询", ""]
            items = result.get("results") or result.get("profiles") or [result]
            for it in items:
                if not isinstance(it, dict):
                    continue
                name = it.get("profile") or it.get("name") or it.get("label") or "-"
                bal = it.get("balance") or it.get("balance_str") or it.get("detail") or "?"
                lines.append(f"- **{name}**: {bal}")
            if len(lines) <= 2:
                lines.append(f"```json\n{json.dumps(result, ensure_ascii=False, indent=2)[:1500]}\n```")
            return "\n".join(lines)
        return f"```\n{str(result)[:1500]}\n```"
    except Exception:
        return f"```\n{str(result)[:1500]}\n```"


def numbered_markdown(title: str, options: list[tuple[str, str]], footer: str = "") -> str:
    """编号列表视图（>3 个选项时的降级交互：回复编号选择）。"""
    lines = [f"# {title}", ""]
    now = time.strftime("%H:%M")
    for idx, (_value, label) in enumerate(options, 1):
        lines.append(f"{idx}. {label}")
    lines.append("")
    lines.append(f"_回复编号选择（{now} 起 5 分钟内有效）_")
    if footer:
        lines.append(footer)
    return "\n".join(lines)
