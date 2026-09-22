"""平台无关的消息通道抽象。

MyCodex 支持多个 IM 平台（飞书 / 企业微信）同时在线。每个平台实现
``Channel`` 协议：收消息由各自的 events 层解析后交给共享 dispatch，
发消息（文本 / 进度 / 审批 / 选择 / 文件）统一走本协议。

进度展示的平台差异（飞书=整卡 PATCH，企微=流式消息）被封装在
``ProgressHandle`` 实现里，调用方只投喂 ``ProgressSnap`` 快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

PLATFORM_FEISHU = "feishu"
PLATFORM_WECOM = "wecom"


@dataclass
class UserTarget:
    """消息回复目标：群消息回群，私聊回私。

    user_id 是平台侧真实用户 ID（飞书 open_id / 企微 userid），
    chat_id 为群 ID（空 = 私聊）。
    """

    platform: str
    user_id: str
    chat_id: str = ""
    is_group: bool = False


@dataclass
class SelectionOption:
    value: str
    label: str
    desc: str = ""


@dataclass
class Selection:
    """选择卡语义：模型/档位/会话/文件等列表点选。"""

    kind: str                 # model / profile / mode / session / file / cd / ...
    approval_id: str
    title: str
    options: list[SelectionOption] = field(default_factory=list)
    footer: str = ""


@dataclass
class ApprovalUI:
    """工具审批卡语义。"""

    approval_id: str
    tool_name: str
    tool_input: dict
    session_id: str


@dataclass
class ProgressSnap:
    """一次进度刷新的完整快照（原 build_progress_card 入参拍平）。"""

    model: str = ""
    status: str = "running"   # running / retrying / completed / failed / cancelled
    step: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    current_tool: str = ""
    current_tool_args: str = ""
    last_text: str = ""
    last_warning: str = ""
    warnings: int = 0
    result_text: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str = ""
    task_id: str = ""
    warning_event: bool = False  # 本次刷新由告警事件触发（供实现加重节流）


@runtime_checkable
class ProgressHandle(Protocol):
    """单条任务消息的进度展示句柄。

    节流与平台刷新策略（PATCH 频率、流式换流等）全部下沉到实现；
    调用方只需在事件到来时投喂快照，所有方法均为 best-effort，
    实现内部吞掉发送异常，不允许让任务因此失败。
    """

    async def update(self, snap: ProgressSnap) -> None: ...

    async def finish(self, snap: ProgressSnap) -> None: ...

    async def error(self, title: str, detail: str) -> None: ...


@runtime_checkable
class Channel(Protocol):
    """一个 IM 平台通道。"""

    name: str

    async def send_text(self, target: UserTarget, text: str) -> Any: ...

    async def send_selection(self, target: UserTarget, sel: Selection) -> None: ...

    async def send_approval(self, target: UserTarget, approval: ApprovalUI) -> None: ...

    async def open_progress(self, target: UserTarget, snap: ProgressSnap) -> ProgressHandle: ...

    async def send_file(self, target: UserTarget, file_path: Path) -> None: ...

    async def is_allowed(self, target: UserTarget) -> bool: ...

    async def send_view(self, target: UserTarget, kind: str, payload: dict) -> None:
        """发送一种富视图（帮助/选择卡/确认卡等）。

        kind 与 payload 是 dispatch 层与各平台实现之间的内部契约，
        两个平台都必须实现全部 kind。现有 kind：
        workspace_selection / cd_confirm / model_selection /
        effort_selection / mode_selection /
        session_selection / file_selection / help /
        reuse_last / ws_config_reuse
        """
        ...
