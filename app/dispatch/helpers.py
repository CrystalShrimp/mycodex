"""工作区 / codex 原生会话读取等平台无关助手（dispatch 层共享）。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.agent.codex_sessions import (
    find_all_codex_workspaces,
    iter_thread_messages,
    latest_thread_for_workspace,
    list_workspace_threads,
)
from config.settings import settings

logger = logging.getLogger("mycodex.dispatch")


# ===== codex native session readers =====


def find_all_codex_projects() -> list[str]:
    """Every workspace that has at least one codex session, newest first."""
    return find_all_codex_workspaces()


def predict_continue_session(workspace: str) -> dict:
    """预判当前工作区将继续恢复的 codex thread 及最后一次对话摘要。"""
    if not workspace:
        return {"can_continue": False, "thread_id": "", "last_summary": ""}
    found = latest_thread_for_workspace(workspace)
    if not found:
        return {"can_continue": False, "thread_id": "", "last_summary": ""}
    return {
        "can_continue": True,
        "thread_id": found[0],
        "last_summary": found[1] or "包含已存在的历史对话",
    }


def list_workspace_sessions(workspace: str) -> list[dict]:
    """枚举当前工作区下的全部 codex thread，按最近活动倒序。"""
    return list_workspace_threads(workspace)


def iter_session_messages(thread_id: str) -> list[dict]:
    """user/assistant messages of one codex thread（含消息数统计口径）。"""
    return iter_thread_messages(thread_id)


# ===== generic helpers =====


def _decode_output(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def get_project_meta(path_str: str) -> dict:
    """获取指定路径的项目关联信息 (Git 分支和 AGENTS.md 状态)"""
    p = Path(path_str)
    meta = {
        "git_branch": "未知 (非 Git 仓库)",
        "agents_md": "不存在",
        "is_exists": p.exists()
    }

    if not p.exists():
        return meta

    if (p / "AGENTS.md").exists():
        meta["agents_md"] = "🟢 存在"
    else:
        meta["agents_md"] = "⚪ 不存在"

    git_dir = p / ".git"
    if git_dir.exists():
        try:
            head_file = git_dir / "HEAD"
            if head_file.exists():
                head_content = head_file.read_text("utf-8").strip()
                if head_content.startswith("ref:"):
                    meta["git_branch"] = f"🌿 {head_content.split('/')[-1]}"
                else:
                    meta["git_branch"] = f"🌿 {head_content[:8]}"
        except Exception:
            pass

    return meta


def _write_env_value(key: str, value: str) -> None:
    """Update one root .env value while preserving all unrelated settings."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    lines = env_path.read_text("utf-8").splitlines() if env_path.exists() else []
    replacement = f"{key}={value}"
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = replacement
            updated = True
            break
    if not updated:
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", "utf-8")


def _scan_workspace_files(workspace_str: str) -> list[dict]:
    """遍历工作区下的普通文件，忽略隐藏目录与代码依赖巨型目录。"""
    ws = Path(workspace_str)
    if not ws.exists() or not ws.is_dir():
        return []

    ignore_dirs = {
        ".git", ".venv", "node_modules", "__pycache__",
        ".sessions", ".claude", ".codex", ".mycodex", ".preferences", ".pytest_cache"
    }

    result = []
    try:
        for p in ws.rglob("*"):
            if not p.is_file():
                continue
            if any(part in ignore_dirs for part in p.parts):
                continue

            try:
                stat = p.stat()
                size_bytes = stat.st_size
                mtime = stat.st_mtime
            except OSError:
                continue

            if size_bytes < 1024:
                size_str = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"

            rel_path = str(p.relative_to(ws))
            result.append({
                "rel_path": rel_path,
                "abs_path": str(p.resolve()),
                "size_bytes": size_bytes,
                "size_str": size_str,
                "mtime": mtime,
            })
    except Exception as e:
        logger.warning("Error scanning workspace files: %s", e)

    result.sort(key=lambda x: -x["mtime"])
    return result


def log_task_failure(task: asyncio.Task) -> None:
    """Retrieve background task exceptions so asyncio does not discard them."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "Message dispatch failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
