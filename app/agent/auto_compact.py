"""会话超窗自动压缩：用 codex 自身作为压缩器（与 myclaw 同架构，2026-10-03）。

终态文本命中 PROMPT_TOO_LONG 且本次为 resume/continue 时：
1. 取该会话 rollout 文件末段（约 60-80K token，低于模型窗口）；
2. spawn 一次性只读沙箱 `codex exec`（无线程续接、不落新会话）提炼交接摘要；
3. 以摘要为背景、新线程重放用户请求。
压缩失败回退 TOO_LONG_GUIDANCE 指引文本。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

PROMPT_TOO_LONG = re.compile(
    r"prompt is too long"
    r"|context[_ ]?(?:window|length)[_ ]?(?:exceeded|limit)"
    r"|maximum context length"
    r"|too many (?:input )?tokens"
    r"|exceeds (?:the )?context"
    r"|conversation too long",
    re.IGNORECASE,
)

TOO_LONG_GUIDANCE = (
    "\n\n⚠️ 该会话累计上下文已超过当前模型窗口，自动压缩未成功。可选：\n"
    "1) `/model` 切换模型后重发本条\n"
    "2) `/reset` 开启新会话\n"
    "3) `/resume` 选择较小的历史会话"
)

_TAIL_CHARS = 240_000  # ≈ 60-80K token

_COMPACT_INSTRUCTION = (
    "你是会话压缩器。下面是一个 Codex CLI 超长会话的末段记录（JSONL）。"
    "忽略 JSON 元数据与工具输出噪声，提炼一份交接摘要，1200 字以内，覆盖："
    "1) 任务与背景 2) 关键决策与结论 3) 正在进行的事项 4) 下一步建议 5) 重要约束与坑。"
    "直接输出摘要正文。\n\n<log>\n{tail}\n</log>"
)


def find_transcript(session_id: str) -> Path | None:
    """定位包含 <session_id> 的 rollout 文件（CODEX_SESSION_DIR 优先，回退 ~/.codex/sessions）。"""
    from config.settings import settings

    roots: list[Path] = []
    raw = settings.get_codex_session_dir()
    if raw:
        roots.append(Path(raw).expanduser())
    roots.append(Path.home() / ".codex" / "sessions")
    for root in roots:
        try:
            for hit in root.glob(f"**/*{session_id}*.jsonl"):
                return hit
        except Exception:
            continue
    return None


async def summarize_tail(
    transcript: Path,
    *,
    cli_path: str,
    model: str,
    workspace: str,
    timeout_s: int = 300,
) -> str | None:
    """一次性只读 codex exec 提炼末段摘要；失败/超时/输出异常返回 None。"""
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")[-_TAIL_CHARS:]
    except Exception:
        return None
    if len(text) < 2000:
        return None
    resolved = shutil.which(cli_path) or cli_path
    args = [
        resolved, "exec", "--skip-git-repo-check",
        "-C", workspace,
        "-c", 'sandbox_mode="read-only"',
        "-c", 'approval_policy="never"',
    ]
    if model:
        args.extend(["-m", model])
    args.append("-")

    env = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    from config.settings import settings as _settings
    proxy = _settings.codex_proxy.strip()
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = proxy

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=env,
        )
        out, _err = await asyncio.wait_for(
            proc.communicate(_COMPACT_INSTRUCTION.format(tail=text).encode("utf-8")),
            timeout=timeout_s,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    summary = out.decode("utf-8", errors="replace").strip()
    if len(summary) < 80 or len(summary) > 20_000:
        return None
    return summary
