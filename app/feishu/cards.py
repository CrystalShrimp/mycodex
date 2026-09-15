from __future__ import annotations

GROUP_MARK = "【群聊】"


def mark_group_card(card: dict) -> dict:
    """群聊来源的卡片标题加【群聊】前缀，一眼区分群任务与私聊任务。"""
    header = card.get("header")
    if not isinstance(header, dict):
        return card
    title = header.get("title")
    if not isinstance(title, dict):
        return card
    content = title.get("content", "")
    if not content.startswith(GROUP_MARK):
        title["content"] = f"{GROUP_MARK}{content}"
    return card


# ===== Model Selection Card (manual selection, before execution) =====


def build_model_selection_card(
    approval_id: str = "",
    models: dict[str, dict] | None = None,
    current_model: str = "",
) -> dict:
    """Codex 模型选择卡片 — 列出本机登录可用的模型。"""
    models = models or {}
    active = current_model if current_model in models else ""

    model_lines = []
    actions = []
    for idx, (slug, info) in enumerate(models.items()):
        label = info.get("label", slug)
        desc = info.get("description", "")
        marker = " ← 当前" if slug == active else ""
        model_lines.append(f"- **{label}** (`{slug}`){f' — {desc}' if desc else ''}{marker}")
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{'✓ ' if slug == active else ''}{label}"},
            "type": ("default" if slug == active else ("primary" if idx == 0 else "default")),
            "value": {
                "approval_id": approval_id,
                "act": "switch_model",
                "model": slug,
                "type": "model_selection",
            },
        })

    display = "\n".join(model_lines) if model_lines else "未发现可用模型"
    if active:
        display = f"**当前已选：** `{active}`\n" + display

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择 Codex 模型"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": display},
            },
            {"tag": "hr"},
            {"tag": "action", "actions": actions},
        ],
    }


# ===== Effort Selection Card (reasoning effort) =====


def build_effort_selection_card(
    approval_id: str = "",
    current_effort: str = "",
) -> dict:
    """推理强度选择卡片 — low / medium / high。"""
    effort_desc = {
        "low": "Low (轻量/快速)",
        "medium": "Medium (标准/推荐)",
        "high": "High (深度/最强)",
    }
    order = [("low", "secondary"), ("medium", "primary"), ("high", "danger")]
    active = current_effort if current_effort in effort_desc else ""

    actions = []
    for effort, btn_type in order:
        is_active = (effort == active)
        actions.append({
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": f"{'✓ ' if is_active else ''}{effort_desc[effort]}",
            },
            "type": btn_type if not is_active else "default",
            "value": {
                "approval_id": approval_id,
                "act": "switch_effort",
                "effort": effort,
                "type": "effort_selection",
            },
        })

    display = f"**当前已选：** `{active or '未选择'}`\n"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择推理强度 (Reasoning Effort)"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": display},
            },
            {"tag": "hr"},
            {"tag": "action", "actions": actions},
        ],
    }


# ===== Reuse-last-settings Card (after workspace switch) =====


def build_reuse_last_card(
    approval_id: str,
    model_label: str,
    effort: str,
    mode: str,
) -> dict:
    """After /cd, ask whether to reuse last model/effort/mode or re-pick.

    Shown only when the user has prior preferences and just switched workspace.
    """
    mode_desc = {
        "h": "🛡️ 严格模式 (只读沙箱)",
        "m": "⚖️ 平衡模式 (写入限工作区)",
        "l": "⚡ 全自动模式 (无沙箱放行)",
    }.get(mode, mode)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "工作区已切换 — 沿用上次配置？"},
            "template": "turquoise",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "📋 **上次设置：**\n"
                        f"• 模型：`{model_label}`\n"
                        f"• 推理强度：`{effort}`\n"
                        f"• 模式：`{mode_desc}`"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 沿用上次"},
                        "type": "primary",
                        "value": {"approval_id": approval_id, "act": "reuse_yes", "type": "reuse_confirm"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 重新设置"},
                        "type": "default",
                        "value": {"approval_id": approval_id, "act": "reuse_no", "type": "reuse_confirm"},
                    },
                ],
            },
        ],
    }


def build_workspace_config_reuse_card(
    approval_id: str,
    workspace: str,
    model_label: str,
    effort: str,
    mode: str,
    action_type: str = "switch",
) -> dict:
    """当在工作区检测到 .mycodex/config.json 配置文件时弹出的沿用确认卡片。"""
    mode_desc = {
        "h": "🛡️ 严格模式 (只读沙箱)",
        "m": "⚖️ 平衡模式 (写入限工作区)",
        "l": "⚡ 全自动模式 (无沙箱放行)",
    }.get(mode, mode)

    title = "检测到工作区已有配置 — 确认沿用？" if action_type == "switch" else "新会话重置 — 沿用工作区已有配置？"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "turquoise",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"📁 **项目工作区：** `{workspace}`\n\n"
                        "⚙️ **发现历史配置文件 (.mycodex/config.json)：**\n"
                        f"• 模型 (Model)：`{model_label}`\n"
                        f"• 推理强度 (Effort)：`{effort}`\n"
                        f"• 模式 (Mode)：`{mode_desc}`"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 确认沿用"},
                        "type": "primary",
                        "value": {
                            "approval_id": approval_id,
                            "act": "reuse_yes",
                            "type": "workspace_config_reuse",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 重新设置"},
                        "type": "default",
                        "value": {
                            "approval_id": approval_id,
                            "act": "reuse_no",
                            "type": "workspace_config_reuse",
                        },
                    },
                ],
            },
        ],
    }


# ===== Progress Card (single persistent card per task) =====


_PROGRESS_STATUS = {
    # status       emoji  label-suffix          color
    "running":     ("🔧", "执行中",              "blue"),
    "retrying":    ("🔄", "API 重试中",          "yellow"),
    "awaiting":    ("⏸",  "等待审批",            "grey"),
    "completed":   ("✅", "执行完成",            "green"),
    "failed":      ("❌", "执行失败",            "red"),
    "cancelled":   ("⏹",  "已取消",              "grey"),
}


def _fmt_duration(s: float) -> str:
    """12:34 / 1:23:45 style duration."""
    if s < 0:
        s = 0
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_tool_progress(tool_counts: dict) -> str:
    """`Bash 152 · Read 21 · Edit 7` style breakdown, top 3 + total."""
    if not tool_counts:
        return "0 次"
    items = sorted(tool_counts.items(), key=lambda kv: -kv[1])
    top = " · ".join(f"{name} {cnt}" for name, cnt in items[:3])
    total = sum(tool_counts.values())
    extra = f" +{len(items) - 3}" if len(items) > 3 else ""
    return f"{top}{extra} · 共 {total} 次"


def build_progress_card(
    model: str,
    status: str = "running",
    *,
    step: int = 0,
    tool_counts: dict | None = None,
    elapsed_s: float = 0.0,
    warnings: int = 0,
    current_tool: str = "",
    current_tool_args: str = "",
    last_text: str = "",
    last_warning: str = "",
    thread_id: str = "",
    task_id: str = "",
    # Final-state-only fields
    result_text: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: str = "",
) -> dict:
    """单卡全生命周期进度卡。

    状态机：running → (retrying/awaiting) → completed/failed/cancelled.
    整个任务期间只 PATCH 这一张卡，不再开新卡。

    status: running | retrying | awaiting | completed | failed | cancelled
    """
    tool_counts = tool_counts or {}
    emoji, label, color = _PROGRESS_STATUS.get(status, _PROGRESS_STATUS["running"])
    sid_hint = f" [{thread_id[:8]}]" if thread_id else ""
    step_hint = f" · 步骤 {step}" if step and status in ("running", "retrying", "awaiting") else ""
    title = f"[{model}] {emoji} {label}{step_hint}{sid_hint}"

    elements: list[dict] = []

    if status in ("running", "retrying", "awaiting"):
        # Progress block
        progress_lines = []
        if current_tool:
            tool_line = f"**当前工具:** {current_tool}"
            if current_tool_args:
                tool_line += f" · {current_tool_args}"
            progress_lines.append(tool_line)
        progress_lines.append(f"**进度:** {_fmt_tool_progress(tool_counts)}")
        warn_line = f"**耗时:** {_fmt_duration(elapsed_s)}"
        if warnings > 0:
            warn_line += f" · ⚠ {warnings} 警告"
        progress_lines.append(warn_line)
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(progress_lines)},
        })

        # Warning snapshot first — problems must stay visible even while a
        # thinking snapshot exists.
        if last_warning:
            elements.append({"tag": "hr"})
            warn_snap = last_warning if len(last_warning) <= 400 else (last_warning[:400] + "…")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**⚠️ 最近警告:**\n> {warn_snap}"},
            })

        # Last thinking snapshot (truncated)
        if last_text:
            snapshot = last_text if len(last_text) <= 600 else (last_text[:600] + "…")
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**最近思考:**\n> {snapshot}"},
            })

    else:  # completed / failed / cancelled
        # Final stats
        stats_lines = [
            f"**耗时:** {_fmt_duration(elapsed_s)} · **工具:** {sum(tool_counts.values())} 次",
        ]
        if input_tokens > 0 or output_tokens > 0:
            stats_lines.append(f"**Tokens:** ↑{input_tokens:,} ↓{output_tokens:,}")
        if thread_id:
            stats_lines.append(f"**Thread:** `{thread_id}`")
        if task_id:
            stats_lines.append(f"**Task ID:** `{task_id}`")
        if warnings > 0:
            stats_lines.append(f"**警告:** {warnings}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": " · ".join(stats_lines[:2]) + ("\n" + "\n".join(stats_lines[2:]) if len(stats_lines) > 2 else "")},
        })

        if error:
            elements.append({"tag": "hr"})
            err_snap = error if len(error) <= 500 else (error[:500] + "…")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**错误:**\n{err_snap}"},
            })

        if result_text:
            elements.append({"tag": "hr"})
            display = result_text if len(result_text) <= 3500 else (result_text[:3500] + f"\n\n… (truncated, total {len(result_text)} chars)")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**结果:**\n{display}"},
            })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }


# ===== Error Card =====


def build_error_card(title: str, detail: str) -> dict:
    """错误卡片。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": detail[:3500]},
            },
        ],
    }


# ===== Simple Text Card =====


def build_simple_text_card(title: str, content: str, color: str = "blue") -> dict:
    """Build a simple text notification card."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            },
        ],
    }


def build_cd_selection_card(
    current_workspace: str,
    projects: list[str],
) -> dict:
    """构建 cd 选择卡片，展示已有的项目列表（按 LRU 排序，只展示文件夹名）以及手动路径输入。"""
    from pathlib import Path
    
    options = []
    # 限制下拉列表最多 90 个，保留一些空间以防超出飞书卡片总长度或选项数上限
    for p in projects[:90]:
        p_path = Path(p)
        label = str(p_path)  # Show the full path to disambiguate same-named projects.
        if len(label) > 100:
            label = "..." + label[-97:]
        options.append({
            "text": {
                "tag": "plain_text",
                "content": label
            },
            "value": p
        })

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"📁 **当前工作区：** {current_workspace}"
            }
        }
    ]

    if options:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "✨ **选择已有项目工作区：**\n在下拉菜单中选择一个电脑上已有的 Codex 项目目录。"
                }
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "点击选择已有的项目目录..."
                        },
                        "value": {
                            "type": "workspace_select",
                            "act": "pre_switch"
                        },
                        "options": options
                    }
                ]
            }
        ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择工作区"},
            "template": "blue",
        },
        "elements": elements
    }
def build_cd_confirm_card(
    target_path: str,
    is_new: bool,
    git_branch: str,
    agents_md: str,
    warning_running: bool = False,
) -> dict:
    """构建 cd 确认切换/新建审批卡片，展示目标项目详情、物理创建权限请求以及可能的中断警告。"""
    if is_new:
        title = "审批请求：新建工作区目录"
        template = "orange"
        type_label = "🆕 待新建物理目录"
        confirm_btn_text = "✅ 允许创建并切换"
        cancel_btn_text = "❌ 拒绝 / 返回"
        extra_note = (
            "\n\n📂 **新建审批说明：**\n"
            f"目标路径 `{target_path}` 在本地磁盘上尚不存在。\n"
            "点击“允许创建”后，系统将在父目录下物理创建文件夹并绑定为当前工作区。"
        )
    else:
        title = "确认切换工作区"
        template = "orange" if warning_running else "blue"
        type_label = "📁 已有目录"
        confirm_btn_text = "确认切换"
        cancel_btn_text = "返回选择"
        extra_note = ""

    details = (
        f"📍 **目标路径：** `{target_path}`\n"
        f"🏷️ **属性：** {type_label}\n"
        f"🌿 **Git 分支：** {git_branch}\n"
        f"📝 **AGENTS.md：** {agents_md}"
        f"{extra_note}"
    )

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": details
            }
        }
    ]

    if warning_running:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "⚠️ **注意：当前正有运行中的 Codex 任务，确认切换工作区将会强行中断当前任务！**"
                }
            }
        ])

    elements.extend([
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": confirm_btn_text
                    },
                    "type": "primary",
                    "value": {
                        "type": "workspace_select",
                        "act": "confirm_switch",
                        "path": target_path
                    }
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": cancel_btn_text
                    },
                    "type": "default",
                    "value": {
                        "type": "workspace_select",
                        "act": "cancel_switch"
                    }
                }
            ]
        }
    ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements
    }


def build_file_selection_card(
    workspace: str,
    files: list[dict],
) -> dict:
    """构建文件选择卡片，类似于 /cd 卡片，展示工作区路径下的文件列表供用户发送。"""
    options = []
    for item in files[:90]:
        rel_path = item.get("rel_path", "")
        size_str = item.get("size_str", "")
        abs_path = item.get("abs_path", "")
        label = f"{rel_path} ({size_str})"
        if len(label) > 100:
            label = "..." + label[-97:]
        options.append({
            "text": {
                "tag": "plain_text",
                "content": label
            },
            "value": abs_path
        })

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"📁 **当前工作区：** `{workspace}`"
            }
        }
    ]

    if options:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"📄 **可发送的文件列表 (共发现 {len(files)} 个文件)：**\n请在下拉菜单中选择您要推送到飞书的文件："
                }
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "点击选择文件进行发送..."
                        },
                        "value": {
                            "type": "file_send_select",
                            "act": "send_file"
                        },
                        "options": options
                    }
                ]
            }
        ])
    else:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "⚠️ **未找到可发送的文件。**\n当前工作区目录下暂无可供发送的普通文件。"
                }
            }
        ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择工作区文件发送"},
            "template": "blue",
        },
        "elements": elements
    }


def build_session_selection_card(
    workspace: str,
    sessions: list[dict],
    current_thread_id: str = "",
) -> dict:
    """构建 /session 选择卡片：列出当前工作区下的所有 Codex 会话供恢复。

    每个 session 项格式：{"thread_id", "mtime", "last_summary", "message_count"}。
    下拉菜单 value 传 thread_id，回调里用它触发 resume。
    """
    import time

    options: list[dict] = []
    now = time.time()
    for item in sessions[:50]:
        tid = item.get("thread_id", "")
        summary = item.get("last_summary", "") or "(无摘要)"
        mtime = item.get("mtime", 0)
        msg_count = item.get("message_count", 0)
        try:
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "?"
        except Exception:
            ts_str = "?"
        ago = ""
        if mtime:
            delta = now - mtime
            if delta < 60:
                ago = f"{int(delta)}秒前"
            elif delta < 3600:
                ago = f"{int(delta // 60)}分钟前"
            elif delta < 86400:
                ago = f"{int(delta // 3600)}小时前"
            else:
                ago = f"{int(delta // 86400)}天前"
        marker = " · 当前" if (current_thread_id and tid == current_thread_id) else ""
        label = f"{ts_str} ({ago}) · {msg_count}步 · {summary}{marker}"
        if len(label) > 100:
            label = label[:97] + "..."
        options.append({
            "text": {"tag": "plain_text", "content": label},
            "value": tid,
        })

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"📁 **当前工作区：** `{workspace}`",
            },
        }
    ]

    if options:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"💬 **可恢复的 Codex 会话 (共 {len(sessions)} 个，按最近活动排序)：**\n"
                        "选择一个会话后，下一条消息将在该线程内继续。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "点击选择要恢复的会话...",
                        },
                        "value": {
                            "type": "session_select",
                            "act": "resume_session",
                        },
                        "options": options,
                    }
                ],
            },
        ])
    else:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "⚠️ **当前工作区下未发现任何 Codex 会话。**\n"
                        "发送一条消息后，Codex 会自动创建新的会话。"
                    ),
                },
            },
        ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择会话恢复"},
            "template": "turquoise",
        },
        "elements": elements,
    }


def build_help_card() -> dict:
    """构建 /help 指令手册交互卡片，采用 lark_md 渲染高亮且精美的指令菜单。"""
    help_md = (
        "🤖 **MyCodex 飞书机器人指令手册** (Codex 驱动)\n\n"
        "**⚙️ 模型设置**\n"
        "- `/model [slug]` : 切换 Codex 模型 (如 gpt-5.6-terra, 不带参数弹卡片)\n"
        "- `/level [low|medium|high]` : 切换推理强度 (同 `/effort`)\n"
        "- `/mode [h|m|l]` : 切换执行模式 (`h` 只读沙箱 / `m` 写入限工作区 / `l` 无沙箱全自动)\n\n"
        "**📁 工作区与文件管理**\n"
        "- `/pwd` : 查看当前关联的项目工作区绝对路径\n"
        "- `/cd [path]` : 切换或新建工作区 (不带路径则弹出交互选择卡片)\n"
        "- `/file` : 选择并直接将工作区下的文件发送到飞书\n\n"
        "**💬 会话与控制**\n"
        "- `/status` : 查看当前会话详情与上下文用量\n"
        "- `/new` : 重置并开启全新会话 (保留当前工作区)\n"
        "- `/stop` (或 `停止`) : 强制中断当前正在运行的任务\n"
        "- `/continue [prompt]` : 恢复并继续上次的对话\n"
        "- `/session [thread_id]` : 列出当前工作区的所有 Codex 会话供选择恢复 (不带参数弹卡片)\n"
        "- `/resume <thread_id>` : 恢复指定的历史会话\n"
        "- `/clean` : 清理旧会话数据\n\n"
        "**🛠️ 实用工具**\n"
        "- `/notes` : 追加写入工作区 `notes.md`（`/notes last` / `/notes <task_id>` / `/notes <内容>`）\n"
        "- `/mem` : 显示当前工作区 AGENTS.md 内容；`/mem <内容>` 追加；`/mem clear` 清空\n"
        "- `/sh <command>` : 在当前工作区执行一条终端命令 (30秒超时)\n"
        "- `/help` : 显示本帮助手册"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "MyCodex 指令手册"},
            "template": "purple",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": help_md},
            }
        ],
    }

def build_mode_selection_card(approval_id: str = "", active_mode: str = "") -> dict:
    """Build a selection card for Execution Mode (h: read-only sandbox, m: workspace-write, l: full access)."""
    options = [
        ("h", "🛡️ 严格模式 (h)", "只读沙箱：禁止一切写入", "warning"),
        ("m", "⚖️ 平衡模式 (m)", "写入限制在工作区内", "primary"),
        ("l", "⚡ 全自动模式 (l)", "无沙箱：完全放行", "success"),
    ]
    actions = []
    for mode_code, title, desc, btn_type in options:
        is_active = (mode_code == active_mode)
        actions.append({
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": f"{'✓ ' if is_active else ''}{title}"
            },
            "type": btn_type if not is_active else "default",
            "value": {
                "type": "mode_switch",
                "act": "switch_mode",
                "mode": mode_code,
                "approval_id": approval_id,
            }
        })

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "请选择 **执行模式 (Mode)**：\n- **🛡️ 严格模式 (h)**：只读沙箱，禁止一切写入。\n- **⚖️ 平衡模式 (m)**：写入限制在工作区内。\n- **⚡ 全自动模式 (l)**：无沙箱，完全放行。"
            }
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": actions
        }
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择执行模式 (Mode)"},
            "template": "orange",
        },
        "elements": elements
    }





