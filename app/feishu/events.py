from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
)

from app.agent.cli_loop import claude_cli_loop, _kill_process_tree
from app.approval.manager import approval_manager
from app.feishu.client import feishu_client
from app.audit.logger import audit_logger
from app.models.schemas import Session, TaskStatus
from app.profiles import discover_profiles, test_profile
from app.state.preferences import preferences_manager
from config.settings import settings

logger = logging.getLogger("myclaw.events")


# ===== Claude native session readers =====
# claude persists the full conversation at
# ~/.claude/projects/<encoded-workspace>/<session_id>.jsonl — we read it
# back directly instead of duplicating the history in .sessions/ files.


def _find_claude_session_file(session_id: str) -> Path | None:
    """Locate the jsonl file for a claude session_id under ~/.claude/projects/.

    The intermediate directory name encodes the workspace path, but its
    exact transformation is claude-internal; we sidestep it by globbing
    on the session_id, which is unique.
    """
    if not session_id:
        return None
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _iter_claude_session_messages(session_id: str) -> list[dict]:
    """Return user/assistant messages from a claude session jsonl.

    Each entry: {"role": "user"|"assistant", "content": str}. Tool-only
    or system rows are skipped. Returns [] if the session file is absent
    or unreadable.
    """
    f = _find_claude_session_file(session_id)
    if f is None:
        return []
    out: list[dict] = []
    try:
        for raw in f.read_text("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("type") not in ("user", "assistant"):
                continue
            msg = row.get("message", {})
            content = msg.get("content", "")
            # claude stores content as a list of blocks for assistant
            # turns; flatten to text.
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(p for p in text_parts if p)
            if not isinstance(content, str):
                content = str(content)
            out.append({"role": row["type"], "content": content})
    except Exception as e:
        logger.warning("Failed to read claude session %s: %s", session_id, e)
    return out


class ReplyChannel:
    """把回复路由到消息来源：群消息回群，私聊回私。"""

    def __init__(self, open_id: str, chat_id: str, is_group: bool) -> None:
        self.open_id = open_id
        self.chat_id = chat_id
        self.is_group = is_group

    async def text(self, content: str) -> dict:
        if self.is_group and self.chat_id:
            return await feishu_client.send_text(self.chat_id, content, is_chat=True)
        return await feishu_client.send_text(self.open_id, content)

    async def card(self, card: dict) -> dict:
        if self.is_group and self.chat_id:
            return await feishu_client.send_card(self.chat_id, card, is_chat=True)
        return await feishu_client.send_card(self.open_id, card)


# ===== Session management =====


class SessionManager:
    """Session manager with file-based persistence for Claude session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}  # session_id -> Session
        self._user_sessions: dict[str, str] = {}  # open_id -> session_id
        self._lock = threading.Lock()
        self._state_dir = Path(settings.audit_log_path).parent / ".sessions"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _state_file(self, open_id: str) -> Path:
        return self._state_dir / f"{open_id}.json"

    def _load_all(self) -> None:
        """Load persisted sessions on startup."""
        if not self._state_dir.exists():
            return
        for f in self._state_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text("utf-8"))
                session = Session(**data)
                with self._lock:
                    self._sessions[session.session_id] = session
                    self._user_sessions[session.user_open_id] = session.session_id
            except Exception:
                pass

    def _save(self, session: Session) -> None:
        """Persist session to disk."""
        try:
            self._state_file(session.user_open_id).write_text(
                session.model_dump_json(indent=2), "utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

    def create_session(
        self,
        user_open_id: str,
        chat_id: str,
        workspace: str | None = None,
    ) -> Session:
        session_id = uuid.uuid4().hex[:12]
        session = Session(
            session_id=session_id,
            user_open_id=user_open_id,
            chat_id=chat_id,
            workspace=workspace or settings.get_default_workspace(),
            workspace_selected=workspace is not None,
        )
        with self._lock:
            self._sessions[session_id] = session
            self._user_sessions[user_open_id] = session_id
        self._save(session)
        return session

    def get_user_session(self, open_id: str) -> Session | None:
        sid = self._user_sessions.get(open_id)
        if sid:
            return self._sessions.get(sid)
        return None

    def save_session(self, session: Session) -> None:
        """Explicitly persist session changes."""
        self._save(session)

    def clean_old_sessions(self, open_id: str) -> None:
        """Remove all session state files except the current user's."""
        if not self._state_dir.exists():
            return
        for f in self._state_dir.glob("*.json"):
            if f.stem != open_id:
                try:
                    f.unlink()
                    logger.info("Cleaned old session: %s", f.name)
                except Exception as e:
                    logger.warning("Failed to clean %s: %s", f.name, e)

    def reset_user_session(self, open_id: str) -> Session:
        old_sid = self._user_sessions.get(open_id)
        if old_sid:
            old = self._sessions.pop(old_sid, None)
            chat_id = old.chat_id if old else ""
            # 继承当前 workspace：/new 语义是"同一项目里开新会话"，不是回到 default。
            # 想换 workspace 用 /pwd。首次没有旧 session 时回落到 default。
            workspace = old.workspace if old else None
        else:
            chat_id = ""
            workspace = None
        session = self.create_session(open_id, chat_id, workspace=workspace)
        self._save(session)
        return session


session_manager = SessionManager()


# ===== Helpers =====


def _is_user_allowed(open_id: str) -> bool:
    allowed = settings.get_allowed_users()
    if not allowed:
        return True
    return open_id in allowed


def _parse_message_text(content: str) -> str:
    try:
        data = json.loads(content)
        return data.get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return content.strip()


def _mode_label(mode: str) -> str:
    return {"h": "高容忍(全允许)", "m": "中风险(高风险审批)", "l": "低容忍(全审批)"}.get(mode, mode)





def _decode_output(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def restore_project_path(encoded_name: str) -> str | None:
    """根据 projects 里的编码文件夹名，还原真实的磁盘绝对路径。"""
    import re
    if len(encoded_name) < 4 or encoded_name[1:3] != "--":
        return None
    drive = encoded_name[0] + ":\\"
    remaining = encoded_name[3:]
    
    def clean(s: str) -> str:
        return re.sub(r'[^a-zA-Z0-9]', '', s).lower()
        
    matched = []
    
    def dfs(current_dir: Path, rem_str: str):
        cleaned_rem = clean(rem_str)
        if not cleaned_rem:
            matched.append(str(current_dir))
            return
            
        try:
            # 过滤掉一些绝对不需要遍历的巨大子目录以保证效率
            subdirs = [
                x for x in current_dir.iterdir() 
                if x.is_dir() and x.name not in (".venv", "node_modules", ".git")
            ]
        except Exception:
            return
            
        for sub in subdirs:
            cleaned_sub = clean(sub.name)
            if not cleaned_sub:
                continue
            if cleaned_rem.startswith(cleaned_sub):
                rest_str = consume_prefix(rem_str, sub.name)
                if rest_str is not None:
                    dfs(sub, rest_str)
                    
    def consume_prefix(rem_str: str, sub_name: str) -> str | None:
        rem_idx = 0
        sub_idx = 0
        while sub_idx < len(sub_name) and rem_idx < len(rem_str):
            c_rem = rem_str[rem_idx].lower()
            c_sub = sub_name[sub_idx].lower()
            
            if c_rem.isalnum() and c_sub.isalnum():
                if c_rem == c_sub:
                    rem_idx += 1
                    sub_idx += 1
                else:
                    return None
            elif not c_rem.isalnum():
                rem_idx += 1
            elif not c_sub.isalnum():
                sub_idx += 1
                
        while sub_idx < len(sub_name):
            if sub_name[sub_idx].isalnum():
                return None
            sub_idx += 1
            
        while rem_idx < len(rem_str) and not rem_str[rem_idx].isalnum():
            rem_idx += 1
            
        return rem_str[rem_idx:]
        
    dfs(Path(drive), remaining)
    return matched[0] if matched else None


def get_project_meta(path_str: str) -> dict:
    """获取指定路径的项目关联信息 (Git 分支和 CLAUDE.md 状态)"""
    p = Path(path_str)
    meta = {
        "git_branch": "未知 (非 Git 仓库)",
        "claude_md": "不存在",
        "is_exists": p.exists()
    }
    
    if not p.exists():
        return meta
        
    if (p / "CLAUDE.md").exists():
        meta["claude_md"] = "🟢 存在"
    else:
        meta["claude_md"] = "⚪ 不存在"
        
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


def _claude_config_path() -> Path | None:
    configured = settings.claude_data_dir.strip()
    if not configured:
        home_claude = (Path.home() / ".claude").resolve()
        return home_claude if home_claude.exists() else None
    path = Path(configured).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        return None
    return path


def _detected_claude_config_path() -> Path:
    return (Path.home() / ".claude").resolve()


def append_to_claude_history(workspace: str, session_id: str, display_text: str) -> None:
    """向 ~/.claude/history.jsonl 追加注册索引，确保 claude --resume 交互菜单能检索展示该 Session。"""
    if not session_id or session_id == "__continue__":
        return
    try:
        home_claude = Path.home() / ".claude"
        home_claude.mkdir(parents=True, exist_ok=True)
        history_file = home_claude / "history.jsonl"

        ws_path = str(Path(workspace).resolve()).rstrip("\\/")
        row = {
            "display": display_text[:200] if display_text else "MyClaw Session",
            "pastedContents": {},
            "timestamp": int(time.time() * 1000),
            "project": ws_path,
            "sessionId": session_id,
        }
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with history_file.open("a", encoding="utf-8") as f:
            f.write(line)
        logger.info("Registered session %s to history.jsonl", session_id)
    except Exception as e:
        logger.warning("Failed to append to claude history.jsonl: %s", e)


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


def find_all_claudecode_projects() -> list[str]:
    """Read every existing workspace recorded under the configured .claude dir."""
    claude_dir = _claude_config_path()
    if claude_dir is None:
        return []

    projects: dict[str, float] = {}

    def add_project(raw_path: object, active_at: object = 0) -> None:
        if not isinstance(raw_path, str) or not raw_path:
            return
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or not path.is_dir():
            return
        resolved = str(path.resolve())
        try:
            timestamp = float(active_at or 0)
        except (TypeError, ValueError):
            timestamp = 0
        projects[resolved] = max(projects.get(resolved, 0), timestamp)

    history_file = claude_dir / "history.jsonl"
    if history_file.is_file():
        try:
            for line in history_file.read_text("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                add_project(row.get("project"), row.get("timestamp", 0))
        except OSError as exc:
            logger.warning("Failed to read Claude history %s: %s", history_file, exc)

    projects_dir = claude_dir / "projects"
    if projects_dir.is_dir():
        for encoded_dir in projects_dir.iterdir():
            if not encoded_dir.is_dir():
                continue
            found_cwd = False
            for session_file in encoded_dir.glob("*.jsonl"):
                try:
                    active_at = session_file.stat().st_mtime
                    with session_file.open("r", encoding="utf-8", errors="replace") as handle:
                        for line_number, line in enumerate(handle):
                            if line_number >= 100:
                                break
                            try:
                                row = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            cwd = row.get("cwd")
                            if isinstance(cwd, str) and cwd:
                                add_project(cwd, active_at)
                                found_cwd = True
                                break
                except OSError:
                    continue
            if not found_cwd:
                restored = restore_project_path(encoded_dir.name)
                add_project(restored)

    return sorted(projects, key=lambda path: (-projects[path], path.lower()))


def predict_continue_session(workspace: str) -> dict:
    """静态预判当前工作区在 `claude --continue` 时将要恢复的 Session ID 及最后一次对话摘要。"""
    if not workspace:
        return {"can_continue": False, "session_id": "", "last_summary": ""}

    ws_resolved = str(Path(workspace).resolve())
    claude_dir = _claude_config_path() or (Path.home() / ".claude")
    encoded_cwd = re.sub(r"[^A-Za-z0-9]", "-", ws_resolved)

    project_dir = claude_dir / "projects" / encoded_cwd
    if not project_dir.is_dir():
        return {"can_continue": False, "session_id": "", "last_summary": ""}

    candidates: list[tuple[float, str, str]] = []

    for f in project_dir.glob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
            sid = f.stem
            last_text = ""
            lines = f.read_text("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("type") in ("user", "assistant"):
                        msg = row.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                            content = " ".join(p for p in parts if p)
                        if isinstance(content, str) and content.strip():
                            last_text = content.strip()[:60]
                            break
                except Exception:
                    continue
            if last_text or len(lines) > 2:
                candidates.append((mtime, sid, last_text))
        except Exception:
            continue

    if not candidates:
        return {"can_continue": False, "session_id": "", "last_summary": ""}

    candidates.sort(key=lambda x: -x[0])
    best_mtime, best_sid, best_summary = candidates[0]
    return {
        "can_continue": True,
        "session_id": best_sid,
        "last_summary": best_summary or "包含已存在的历史对话",
    }


def list_workspace_sessions(workspace: str) -> list[dict]:
    """枚举当前工作区在 `~/.claude/projects/<encoded>/` 下的全部 Claude Session。

    返回按 mtime 倒序排列：[{"session_id", "mtime", "last_summary", "message_count"}]。
    message_count 只统计 user/assistant 文本行（与 /status 的口径一致）。
    """
    if not workspace:
        return []
    ws_resolved = str(Path(workspace).resolve())
    claude_dir = _claude_config_path() or (Path.home() / ".claude")
    encoded_cwd = re.sub(r"[^A-Za-z0-9]", "-", ws_resolved)
    project_dir = claude_dir / "projects" / encoded_cwd
    if not project_dir.is_dir():
        return []

    out: list[dict] = []
    for f in project_dir.glob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
            sid = f.stem
            last_text = ""
            msg_count = 0
            lines = f.read_text("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") not in ("user", "assistant"):
                    continue
                msg_count += 1
                if not last_text:
                    msg = row.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        parts = [
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        content = " ".join(p for p in parts if p)
                    if isinstance(content, str) and content.strip():
                        last_text = content.strip()[:60]
            # 与 predict_continue_session 同口径：跳过几乎空白的 stub 文件
            if not (last_text or len(lines) > 2):
                continue
            out.append({
                "session_id": sid,
                "mtime": mtime,
                "last_summary": last_text or "(无文本)",
                "message_count": msg_count,
            })
        except Exception as e:
            logger.warning("Failed to read session file %s: %s", f, e)
            continue

    out.sort(key=lambda x: -x["mtime"])
    return out


def _claude_dir_selection_card() -> dict:
    from app.feishu.cards import build_claude_dir_selection_card
    return build_claude_dir_selection_card(str(_detected_claude_config_path()))


def _workspace_selection_card(session: Session | None = None) -> dict:
    if session and session.workspace:
        current = session.workspace
    else:
        current = settings.default_workspace.strip() or "尚未设置"
    from app.feishu.cards import build_cd_selection_card
    return build_cd_selection_card(current, find_all_claudecode_projects())


def _scan_workspace_files(workspace_str: str) -> list[dict]:
    """遍历工作区下的普通文件，忽略隐藏目录与代码依赖巨型目录。"""
    ws = Path(workspace_str)
    if not ws.exists() or not ws.is_dir():
        return []

    ignore_dirs = {
        ".git", ".venv", "node_modules", "__pycache__",
        ".sessions", ".claude", ".preferences", ".pytest_cache"
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

async def _send_card_after_callback(open_id: str, card: dict) -> None:
    """Let the WebSocket callback acknowledgement flush before sending a new card."""
    await asyncio.sleep(0.2)
    await feishu_client.send_card(open_id, card)


async def _send_text_after_callback(open_id: str, content: str) -> None:
    await asyncio.sleep(0.2)
    await feishu_client.send_text(open_id, content)

async def _send_claude_setup_result(open_id: str, resolved: str) -> None:
    await asyncio.sleep(0.2)
    await feishu_client.send_text(
        open_id, f"✅ Claude 数据目录已保存：{resolved}"
    )
    await feishu_client.send_card(open_id, _workspace_selection_card())


def _info_toast(content: str) -> P2CardActionTriggerResponse:
    """Build responses exactly as shown in the official Feishu Python sample."""
    return P2CardActionTriggerResponse({
        "toast": {"type": "info", "content": content},
    })

def _log_dispatch_failure(task: asyncio.Task) -> None:
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


async def _check_and_run_pending(open_id: str) -> bool:
    """Check if all initial setup items (Provider, Level, Mode) are complete.
    If complete and pending_prompt exists, trigger _run_claude automatically.
    """
    preferences = preferences_manager.get(open_id)
    profiles = discover_profiles()

    has_provider = preferences.model in profiles
    has_level = preferences.level in ("haiku", "sonnet", "opus")
    has_mode = preferences.mode in ("h", "m", "l")

    if has_provider and has_level and has_mode:
        session = session_manager.get_user_session(open_id)
        if session:
            preferences_manager.save_workspace_config(session.workspace, preferences)

        if session and session.pending_prompt.strip():
            pending = session.pending_prompt.strip()
            session.pending_prompt = ""
            session_manager.save_session(session)

            profile_label = profiles.get(preferences.model, {}).get("label", preferences.model)
            mode_labels = {"h": "🛡️ 严格模式 (h)", "m": "⚖️ 平衡模式 (m)", "l": "⚡ 全自动模式 (l)"}
            mode_lbl = mode_labels.get(preferences.mode, preferences.mode)

            prompt_preview = pending if len(pending) <= 30 else pending[:27] + "..."
            msg_lines = [
                "🎉 初始配置已全部就绪！",
                "",
                "📋 当前运行设置：",
                f"• 供应商 (Provider)：`{profile_label}`",
                f"• 规格 (Model Level)：`{preferences.level}`",
                f"• 审批模式 (Mode)：`{mode_lbl}`",
                "",
                f"正在全自动为您执行暂存的任务：`{prompt_preview}` ..."
            ]
            reply = ReplyChannel(open_id, session.chat_id, bool(session.chat_id))
            await reply.text("\n".join(msg_lines))
            asyncio.get_running_loop().create_task(
                _run_claude(pending, open_id, session, chat_id=session.chat_id)
            )
            return True
    return False

# ===== Event handlers =====


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
    # 群聊仅 @机器人 时才响应；私聊不受影响
    if is_group and not _is_bot_mentioned(msg, text):
        logger.info("From %s (group, not @bot, ignored): %s", open_id, text[:100])
        return
    # 群聊里 @机器人 的消息带 @_user_N 提及标记，剥掉再当指令/提示词
    text = re.sub(r"@_user_\d+", "", text).strip()
    if not text:
        return
    logger.info("From %s (%s): %s", open_id, chat_type or "p2p", text[:100])
    task = asyncio.get_running_loop().create_task(
        _dispatch(open_id, chat_id, message_id, text, is_group=is_group)
    )
    task.add_done_callback(_log_dispatch_failure)


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
    logger.info(
        "Card action: operator=%s type=%s act=%s option=%r form_keys=%s",
        open_id,
        card_type,
        act,
        action.option,
        sorted((action.form_value or {}).keys()),
    )
    
    # First-run setup: locate the Claude data directory before listing workspaces.
    if card_type == "claude_dir_select":
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        act = action.value.get("act", "")
        if act == "set_detected":
            target_path = action.value.get("path", "")
        else:
            target_path = ""
            if action.form_value:
                target_path = action.form_value.get("claude_data_dir") or ""
        path = Path(target_path.strip()).expanduser()
        if not path.is_absolute() or not path.is_dir():
            toast.type = "error"
            toast.content = "请输入存在的 .claude 文件夹绝对路径"
            resp.toast = toast
            return resp
        if not (path / "history.jsonl").is_file() and not (path / "projects").is_dir():
            toast.type = "error"
            toast.content = "该目录中未找到 history.jsonl 或 projects 文件夹"
            resp.toast = toast
            return resp
        resolved = str(path.resolve())
        _write_env_value("CLAUDE_DATA_DIR", resolved)
        settings.claude_data_dir = resolved
        task = asyncio.get_running_loop().create_task(
            _send_claude_setup_result(open_id, resolved)
        )
        task.add_done_callback(_log_dispatch_failure)
        return P2CardActionTriggerResponse({})
    # 拦截并处理工作区选择、确认与返回
    if card_type == "workspace_select":
        act = action.value.get("act", "")
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        
        # 1. 第一阶段预处理：拉起确认卡片
        if act in ("pre_switch", "pre_create"):
            target_path = ""
            if act == "pre_switch":
                target_path = action.option or ""
            elif act == "pre_create":
                if action.form_value:
                    target_path = action.form_value.get("workspace_path") or ""
                if not target_path:
                    target_path = action.option or ""
                    
            target_path = target_path.strip()
            if not target_path:
                toast.type = "error"
                toast.content = "未检测到路径。您可以直接发送指令：/cd <绝对路径>"
                resp.toast = toast
                return resp
                
            p = Path(target_path)
            if not p.is_absolute():
                toast.type = "error"
                toast.content = "错误：请输入绝对路径"
                resp.toast = toast
                return resp
                
            is_new = False
            if not p.exists():
                if act == "pre_create":
                    # 校验父目录是否存在
                    parent = p.parent
                    if not parent.exists():
                        toast.type = "error"
                        toast.content = f"校验失败：父目录 `{parent}` 不存在！"
                        resp.toast = toast
                        return resp
                    is_new = True
                else:
                    toast.type = "error"
                    toast.content = f"错误：路径不存在: `{target_path}`"
                    resp.toast = toast
                    return resp
                    
            # 探测元数据
            meta = get_project_meta(target_path)
            # 检查是否有任务正在运行
            warning_running = claude_cli_loop.is_running(open_id)
            
            from app.feishu.cards import build_cd_confirm_card
            confirm_card = build_cd_confirm_card(
                target_path=str(p.resolve()),
                is_new=is_new,
                git_branch=meta["git_branch"],
                claude_md=meta["claude_md"],
                warning_running=warning_running
            )
            task = asyncio.get_running_loop().create_task(
                _send_card_after_callback(open_id, confirm_card)
            )
            task.add_done_callback(_log_dispatch_failure)
            return P2CardActionTriggerResponse({})
            
        # 2. 第二阶段真正动作：确认切换
        elif act == "confirm_switch":
            target_path = action.value.get("path", "").strip()
            if not target_path:
                toast.type = "error"
                toast.content = "路径信息丢失，请重新选择"
                resp.toast = toast
                return resp
                
            p = Path(target_path)
            is_new = False
            if not p.exists():
                parent = p.parent
                if not parent.exists():
                    toast.type = "error"
                    toast.content = f"创建失败：父目录 `{parent}` 不存在！"
                    resp.toast = toast
                    return resp
                try:
                    p.mkdir(parents=False, exist_ok=True)
                    is_new = True
                except Exception as e:
                    toast.type = "error"
                    toast.content = f"无法创建目录: {e}"
                    resp.toast = toast
                    return resp
                    
            session = session_manager.get_user_session(open_id)
            if not session:
                session = session_manager.create_session(open_id, "", workspace=str(p.resolve()))
            else:
                session.workspace = str(p.resolve())
                session.workspace_selected = True
                session.claude_session_id = "__continue__"  # 切换工作区后全自动带 --continue 恢复该项目最新 Session 历史
                session_manager.save_session(session)

            preferences_manager.clear(open_id)
            resolved_workspace = str(p.resolve())
            _write_env_value("DEFAULT_WORKSPACE", resolved_workspace)
            settings.default_workspace = resolved_workspace
            claude_cli_loop.cancel_by_user(open_id)
            
            pred = predict_continue_session(session.workspace)
            if pred["can_continue"]:
                pred_text = f"\n🔄 **预计关联会话：** 可继续恢复 (`{pred['session_id']}`)\n💬 **上次对话：** {pred['last_summary']}"
            else:
                pred_text = "\n🆕 **预计关联会话：** 纯净项目 (发送首条消息时自动分配新 Session)"

            action_msg = "已在新目录新建并切换" if is_new else "已切换"
            task = asyncio.get_running_loop().create_task(
                _send_text_after_callback(open_id, f"📁 {action_msg}工作区：`{session.workspace}`{pred_text}")
            )
            task.add_done_callback(_log_dispatch_failure)

            ws_config = preferences_manager.load_workspace_config(resolved_workspace)
            if ws_config and ws_config.complete:
                profiles = discover_profiles()
                profile_label = profiles.get(ws_config.model, {}).get("label", ws_config.model)
                from app.feishu.cards import build_workspace_config_reuse_card
                reuse_card = build_workspace_config_reuse_card(
                    approval_id=uuid.uuid4().hex[:12],
                    workspace=resolved_workspace,
                    profile_label=profile_label,
                    level=ws_config.level,
                    mode=ws_config.mode,
                    action_type="switch",
                )
                task2 = asyncio.get_running_loop().create_task(
                    _send_card_after_callback(open_id, reuse_card)
                )
                task2.add_done_callback(_log_dispatch_failure)
            return P2CardActionTriggerResponse({})
            
        # 3. 第二阶段动作：取消并返回第一阶段卡片
        elif act == "cancel_switch":
            task = asyncio.get_running_loop().create_task(
                _send_card_after_callback(open_id, _workspace_selection_card())
            )
            task.add_done_callback(_log_dispatch_failure)
            return P2CardActionTriggerResponse({})

    # 拦截并处理文件发送卡片动作
    if card_type == "file_send_select" and act == "send_file":
        target_path = action.option or ""
        if not target_path and action.value:
            target_path = action.value.get("path", "")

        if not target_path:
            return P2CardActionTriggerResponse({
                "toast": {"type": "error", "content": "错误：未选择任何文件"}
            })

        p = Path(target_path)
        if not p.exists() or not p.is_file():
            return P2CardActionTriggerResponse({
                "toast": {"type": "error", "content": f"文件不存在：{p.name}"}
            })

        if p.stat().st_size > 30 * 1024 * 1024:
            return P2CardActionTriggerResponse({
                "toast": {"type": "error", "content": f"文件过大 ({p.stat().st_size / (1024*1024):.1f}MB)，飞书上限 30MB"}
            })

        async def _upload_and_send_task() -> None:
            try:
                await feishu_client.send_text(open_id, f"⏳ 正在上传并发送文件：`{p.name}` ...")
                file_key = await feishu_client.upload_file(p)
                await feishu_client.send_file(open_id, file_key)
            except Exception as e:
                logger.exception("Failed to send file to user %s: %s", open_id, e)
                await feishu_client.send_text(open_id, f"❌ 发送文件失败：{e}")

        task = asyncio.get_running_loop().create_task(_upload_and_send_task())
        task.add_done_callback(_log_dispatch_failure)

        return P2CardActionTriggerResponse({
            "toast": {"type": "info", "content": f"已开始发送文件：{p.name}"}
        })

    approval_id = action.value.get("approval_id", "")
    act = action.value.get("act", "")
    # Mode selection card handler
    if card_type == "mode_switch" and act == "switch_mode":
        target_mode = action.value.get("mode", "")
        if target_mode not in ("h", "m", "l"):
            target_mode = "m"
        preferences = preferences_manager.get(open_id)
        preferences.mode = target_mode
        preferences_manager.save(open_id, preferences)

        asyncio.get_running_loop().create_task(_check_and_run_pending(open_id))

        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        mode_labels = {"h": "严格模式 (h)", "m": "平衡模式 (m)", "l": "全自动模式 (l)"}
        lbl = mode_labels.get(target_mode, target_mode)
        toast.type = "info"
        toast.content = f"已设置审批模式: {lbl}"
        resp.toast = toast
        return resp

    # Model selection card: switch_model → approve + update session model
    if card_type == "model_selection" and act == "switch_model":
        target_model = action.value.get("model", "")
        if target_model not in ("haiku", "sonnet", "opus"):
            target_model = "sonnet"
        preferences = preferences_manager.get(open_id)
        preferences.level = target_model
        preferences_manager.save(open_id, preferences)
        approval_manager.set_switch_model(approval_id, target_model)
        asyncio.get_running_loop().create_task(
            approval_manager.handle_decision(approval_id, open_id, True)
        )
        asyncio.get_running_loop().create_task(_check_and_run_pending(open_id))

        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = f"已切换模型规格: {target_model}"
        resp.toast = toast
        return resp

    # Provider model selection card.
    if card_type == "profile_switch" and act == "switch_profile":
        profile_name = action.value.get("profile", "")
        profiles = discover_profiles()
        label = profiles.get(profile_name, {}).get("label", profile_name)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        if profile_name not in profiles:
            toast.type = "error"
            toast.content = f"切换失败: 未找到 {label} 配置"
            resp.toast = toast
            return resp

        preferences = preferences_manager.get(open_id)
        preferences.model = profile_name
        preferences_manager.save(open_id, preferences)
        claude_cli_loop.cancel_by_user(open_id)
        ok, detail = test_profile(profile_name)

        if ok:
            asyncio.get_running_loop().create_task(_check_and_run_pending(open_id))

        toast.type = "success" if ok else "error"
        toast.content = (
            f"模型已切换为 {label}"
            if ok else f"模型不可用: {label} ({detail})"
        )
        resp.toast = toast
        return resp

    # Session selection card (/session)
    if card_type == "session_select" and act == "resume_session":
        target_sid = (action.option or "").strip()
        if not target_sid and action.value:
            target_sid = action.value.get("session_id", "").strip()
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        if not target_sid:
            toast.type = "error"
            toast.content = "未选择 Session"
            resp.toast = toast
            return resp

        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, "")
        session.claude_session_id = target_sid
        session.context_tokens = 0
        session_manager.save_session(session)
        # 让下次发消息时用 --resume <id> 启动新进程；旧进程的 cancel 用任务异步等待
        claude_cli_loop.cancel_by_user(open_id)
        asyncio.get_running_loop().create_task(claude_cli_loop.cancel_and_wait(open_id))

        toast.type = "success"
        toast.content = f"已切换到 Session {target_sid}（下一条消息将 --resume）"
        resp.toast = toast
        return resp

    # Reuse-last-settings card after /cd
    if card_type == "reuse_confirm":
        session = session_manager.get_user_session(open_id)
        pending = session.pending_prompt.strip() if session else ""
        if session:
            session.pending_reuse_confirm = False
            session_manager.save_session(session)
        reuse = (act == "reuse_yes")
        if not reuse:
            # User opted to re-pick: wipe preferences so next _run_claude shows setup cards.
            preferences_manager.clear(open_id)
        toast = P2CardActionTriggerResponse()
        toast_msg = "沿用上次设置" if reuse else "已清空，重新选择"
        # Re-trigger the queued prompt with the (preserved or cleared) prefs.
        if pending and session:
            session.pending_prompt = ""
            session_manager.save_session(session)
            asyncio.get_running_loop().create_task(_run_claude(pending, open_id, session))
        # Simple toast for the click.
        resp = P2CardActionTriggerResponse()
        toast_cb = CallBackToast()
        toast_cb.type = "success" if reuse else "info"
        toast_cb.content = toast_msg
        resp.toast = toast_cb
        return resp

    # Workspace config reuse card handler
    if card_type == "workspace_config_reuse":
        session = session_manager.get_user_session(open_id)
        workspace = session.workspace if session else settings.get_default_workspace()
        reuse = (act == "reuse_yes")
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()

        if reuse:
            ws_config = preferences_manager.load_workspace_config(workspace)
            if ws_config and ws_config.complete:
                preferences_manager.save(open_id, ws_config)
                toast.type = "success"
                toast.content = "✅ 已成功沿用工作区配置！"
                resp.toast = toast
                asyncio.get_running_loop().create_task(_check_and_run_pending(open_id))
                return resp

        # Wiped or user chose reset
        preferences_manager.clear(open_id)
        toast.type = "info"
        toast.content = "已重置偏好，请重新选择配置"
        resp.toast = toast
        if session:
            asyncio.get_running_loop().create_task(_run_claude(session.pending_prompt or "", open_id, session))
        return resp


    approved = act == "approve"
    card_label = "工具执行" if card_type == "tool_execution" else "审批"
    asyncio.get_running_loop().create_task(
        approval_manager.handle_decision(approval_id, open_id, approved)
    )

    # Update card to show result (replaces buttons with status text)
    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "success" if approved else "error"
    toast.content = "已允许" if approved else "已拒绝"
    resp.toast = toast
    resp.card = _build_done_card(
        title=f"{'✅' if approved else '❌'} {card_label}已{'允许' if approved else '拒绝'}",
        color="green" if approved else "red",
        approval_id=approval_id,
    )
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


# ===== Core dispatch =====


async def _dispatch(open_id: str, chat_id: str, message_id: str, text: str, is_group: bool = False) -> None:
    reply = ReplyChannel(open_id, chat_id, is_group)
    if not _is_user_allowed(open_id):
        allowed_list = settings.get_allowed_users()
        err_msg = (
            f"🚫 **权限拦截提醒**\n"
            f"• 您的飞书 Open ID: `{open_id or '(空)'}`\n"
            f"• 当前允许的用户列表: `{', '.join(allowed_list) if allowed_list else '(空，未配置白名单)'}`\n\n"
            f"💡 **解决建议**：请在 `.env` 中把您的 Open ID 加入 `ALLOWED_USERS`；"
            f"如需对所有用户开放，请把 `.env` 中的 `ALLOWED_USERS` 设为空。"
        )
        await reply.text( err_msg)
        return

    text = text.strip()
    text_lower = text.lower()

    session = session_manager.get_user_session(open_id)
    if _claude_config_path() is None:
        await reply.card( _claude_dir_selection_card())  # 配置流程：保持私聊
        return

    # --- /stop: interrupt current task ---
    if text_lower in ("/stop", "停止"):
        cancelled = await claude_cli_loop.cancel_and_wait(open_id)
        if cancelled:
            await reply.text( "已中断会话。")
        else:
            await reply.text( "没有正在运行的任务。")
        return

    # --- /reset: reset all setup preferences for testing ---
    if text_lower in ("/reset", "重置"):
        preferences = preferences_manager.get(open_id)
        preferences.model = ""
        preferences.level = ""
        preferences.mode = ""
        preferences_manager.save(open_id, preferences)
        await reply.text(
            "已清空所有初始设置（Provider / Model Level / Mode）。",
        )
        return

    # --- /status ---
    if text_lower == "/status":
        try:
            session = session_manager.get_user_session(open_id)
            preferences = preferences_manager.get(open_id)
            if session:
                # Build context usage info
                ctx_info = ""
                if session.context_tokens > 0:
                    pct = session.context_tokens / session.context_limit * 100
                    ctx_info = (
                        f"上下文用量: `{session.context_tokens:,}` / `{session.context_limit:,}` ({pct:.0f}%)\n"
                    )
                    if pct >= settings.context_critical_percent:
                        if settings.compact_enabled:
                            ctx_info += "⚠️ 上下文即将耗尽，建议使用 `/compact` 压缩或 `/new` 开始新会话\n"
                        else:
                            ctx_info += "⚠️ 上下文即将耗尽，建议使用 `/new` 开始新会话\n"
                    elif pct >= settings.context_warn_percent:
                        if settings.compact_enabled:
                            ctx_info += "⚠️ 上下文用量较高，可以用 `/compact` 压缩上下文\n"
                        else:
                            ctx_info += "⚠️ 上下文用量较高，请注意\n"
                
                csid = session.claude_session_id or "未启动"
                msg_count = 0
                if session.claude_session_id and session.claude_session_id != "__continue__":
                    msg_count = len(_iter_claude_session_messages(session.claude_session_id))
                elif session.claude_session_id == "__continue__":
                    csid = "全自动恢复该项目最新 Session (--continue)"

                await reply.text(
                    f"📁 工作区: `{session.workspace}`\n"
                    f"🔑 Claude Session: `{csid}`\n"
                    f"🤖 模型供应商: `{preferences.model or '未选择'}`\n"
                    f"⚡ 模型规格: `{preferences.level or '未选择'}`\n"
                    f"🛡️ 审批模式: `{preferences.mode or '未选择'}`\n"
                    f"💬 底层推演步数: {msg_count} 步 (含思考/工具调用记录)\n"
                    f"{ctx_info}"
                    f"状态: {session.status.value}",
                )
            else:
                await reply.text( "没有活跃会话。用 /new 创建新会话。")
        except Exception as err:
            logger.exception("Error handling /status for user %s", open_id)
            await reply.text( f"状态获取失败：{err}")
        return

    # --- /help ---
    if text_lower == "/help":
        from app.feishu.cards import build_help_card
        await reply.card( build_help_card())
        return

    # --- /provider [profile]: choose provider profile ---
    if text_lower == "/provider" or text_lower.startswith("/provider "):
        parts = text.split(None, 1)
        profiles = discover_profiles()
        preferences = preferences_manager.get(open_id)
        if not profiles:
            await reply.text( "未发现 config/settings_*.json 模型配置。")
            return
        if len(parts) == 2:
            name = parts[1].strip().lower()
            if name in ("reset", "clear"):
                preferences.model = ""
                preferences.level = ""
                preferences.mode = ""
                preferences_manager.save(open_id, preferences)
                await reply.text(
                    "已清空所有初始设置（Provider / Model Level / Mode）。",
                )
                return
            if name not in profiles:
                await reply.text( f"未知供应商: `{name}`\n可用: {'、'.join(profiles.keys())}",
                )
                return
            preferences.model = name
            preferences_manager.save(open_id, preferences)
            await claude_cli_loop.cancel_and_wait(open_id)
            ok, detail = test_profile(name)
            label = profiles[name].get("label", name)
            await reply.text(
                f"模型供应商已切换为 `{label}`。" if ok else f"模型供应商 `{label}` 不可用: {detail}",
            )
            return

        from app.feishu.cards import build_profile_selection_card
        card = build_profile_selection_card(
            approval_id=uuid.uuid4().hex[:12],
            profiles=profiles,
            active_profile=preferences.model,
        )
        await reply.card( card)
        return

    # --- /model [haiku|sonnet|opus] / /level: choose model capability level ---
    if (
        text_lower == "/model" or text_lower.startswith("/model ")
        or text_lower == "/level" or text_lower.startswith("/level ")
    ):
        parts = text.split(None, 1)
        preferences = preferences_manager.get(open_id)
        profiles = discover_profiles()
        valid_levels = ("haiku", "sonnet", "opus")

        if len(parts) == 2:
            arg = parts[1].strip().lower()
            if arg in valid_levels:
                preferences.level = arg
                preferences_manager.save(open_id, preferences)
                await claude_cli_loop.cancel_and_wait(open_id)
                await reply.text( f"模型规格已切换为 `{preferences.level}`。")
                return
            elif arg in profiles:
                await reply.text(
                    f"⚠️ `{arg}` 是模型供应商 (Provider)。\n"
                    f"切换供应商请使用：`/provider {arg}`\n"
                    f"切换模型规格请使用：`/model haiku|sonnet|opus`",
                )
                return

        await reply.text(
            f"当前模型规格: `{preferences.level or '未选择'}`\n\n"
            "用法: `/model haiku|sonnet|opus` (也可使用 `/level`)",
        )
        return

    # --- /mode [h|m|l]: choose approval mode ---
    if text_lower == "/mode" or text_lower.startswith("/mode "):
        parts = text.split(None, 1)
        preferences = preferences_manager.get(open_id)
        if len(parts) == 2 and parts[1].strip().lower() in ("h", "m", "l"):
            old_mode = preferences.mode
            new_mode = parts[1].strip().lower()
            preferences.mode = new_mode
            preferences_manager.save(open_id, preferences)
            mode_labels = {"h": "🛡️ 严格模式", "m": "⚖️ 平衡模式", "l": "⚡ 全自动模式"}
            mode_name = mode_labels.get(new_mode, new_mode)
            # session_registry 里的 approval_mode 是进程启动时的快照，
            # 复用旧进程会让 hook 仍按旧 mode 走审批分支，必须 teardown。
            if old_mode != new_mode:
                await claude_cli_loop.cancel_and_wait(open_id)
                await reply.text(
                    f"审批模式已切换为 {mode_name} (`{new_mode}`)，已重启 Claude 进程使新模式生效。",
                )
            else:
                await reply.text(
                    f"审批模式仍为 {mode_name} (`{new_mode}`)。",
                )
            await _check_and_run_pending(open_id)
            return

        from app.feishu.cards import build_mode_selection_card
        card = build_mode_selection_card(
            approval_id=uuid.uuid4().hex[:12],
            active_mode=preferences.mode,
        )
        await reply.card( card)
        return
    # --- /pwd: print current workspace path ---
    if text_lower == "/pwd" or text_lower.startswith("/pwd "):
        session = session_manager.get_user_session(open_id)
        ws = session.workspace if session else settings.get_default_workspace()
        await reply.text( f"📁 当前工作目录：`{ws}`")
        return


    # --- /file: select and send workspace files ---
    if text_lower == "/file" or text_lower.startswith("/file "):
        session = session_manager.get_user_session(open_id)
        ws = session.workspace if session else settings.get_default_workspace()
        files = _scan_workspace_files(ws)
        from app.feishu.cards import build_file_selection_card
        card = build_file_selection_card(ws, files)
        await reply.card( card)
        return

    # --- /cd <path>: switch workspace ---
    if text_lower.startswith("/cd"):
        parts = text.split(None, 1)
        
        # 1. 如果不带参数，展示卡片选择已有项目或新建
        if len(parts) < 2 or not parts[1].strip():
            session = session_manager.get_user_session(open_id)
            card = _workspace_selection_card(session)
            await reply.card( card)
            return
            
        # 2. 如果带路径参数，直接校验切换
        new_path = parts[1].strip()
        p = Path(new_path)
        if not p.is_absolute():
            await reply.text( "错误：请使用绝对路径，例如 `/cd D:\\projects\\myapp`",
            )
            return
            
        if not p.exists():
            # 检查父目录是否存在
            parent = p.parent
            if not parent.exists():
                await reply.text( f"❌ 切换失败：父目录 `{parent}` 在磁盘上不存在！"
                )
                return
            
            # 目录不存在但父目录存在 — 弹出前置确认卡片，待用户确认后再建目录切换
            target_path = str(p.resolve())
            meta = get_project_meta(target_path)
            warning_running = claude_cli_loop.is_running(open_id)
            
            from app.feishu.cards import build_cd_confirm_card
            confirm_card = build_cd_confirm_card(
                target_path=target_path,
                is_new=True,
                git_branch=meta["git_branch"],
                claude_md=meta["claude_md"],
                warning_running=warning_running
            )
            await reply.card( confirm_card)
            return
                
        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id, workspace=str(p.resolve()))
        else:
            session.workspace = str(p.resolve())
            session.workspace_selected = True
            session.claude_session_id = "__continue__"  # 切换工作区后全自动带 --continue 恢复该项目最新 Session 历史
            session_manager.save_session(session)

        resolved_workspace = str(p.resolve())
        _write_env_value("DEFAULT_WORKSPACE", resolved_workspace)
        settings.default_workspace = resolved_workspace
        claude_cli_loop.cancel_by_user(open_id)

        pred = predict_continue_session(session.workspace)
        if pred["can_continue"]:
            pred_text = f"\n🔄 **预计关联会话：** 可继续恢复 (`{pred['session_id']}`)\n💬 **上次对话：** {pred['last_summary']}"
        else:
            pred_text = "\n🆕 **预计关联会话：** 纯净项目 (发送首条消息时自动分配新 Session)"

        await reply.text( f"📁 工作区已切换：`{session.workspace}`{pred_text}")

        ws_config = preferences_manager.load_workspace_config(resolved_workspace)
        if ws_config and ws_config.complete:
            profiles = discover_profiles()
            profile_label = profiles.get(ws_config.model, {}).get("label", ws_config.model)
            from app.feishu.cards import build_workspace_config_reuse_card
            reuse_card = build_workspace_config_reuse_card(
                approval_id=uuid.uuid4().hex[:12],
                workspace=resolved_workspace,
                profile_label=profile_label,
                level=ws_config.level,
                mode=ws_config.mode,
                action_type="switch",
            )
            await reply.card( reuse_card)
        else:
            preferences_manager.clear(open_id)
        return

    # --- /session [session_id]: list sessions in current workspace and resume one ---
    if text_lower == "/session" or text_lower.startswith("/session "):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id)
        ws = session.workspace or settings.get_default_workspace()

        # /session <id>: 直接走 /resume 同款路径
        if len(parts) == 2 and parts[1].strip():
            target_id = parts[1].strip()
            await claude_cli_loop.cancel_and_wait(open_id)
            session.claude_session_id = target_id
            session.context_tokens = 0
            session_manager.save_session(session)
            await reply.text(
                f"🔑 已切换到 Claude Session `{target_id}`。\n"
                "下一条消息将通过 `--resume` 在该会话内继续。",
            )
            return

        # /session (无参): 弹卡片选择
        sessions = list_workspace_sessions(ws)
        cur = session.claude_session_id or ""
        if cur == "__continue__":
            cur = ""
        from app.feishu.cards import build_session_selection_card
        card = build_session_selection_card(ws, sessions, current_session_id=cur)
        await reply.card( card)
        return

    # --- /resume <session_id>: switch to one exact native session ---
    if text_lower.startswith("/resume"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(open_id)
        if len(parts) < 2 or not parts[1].strip():
            cur = session.claude_session_id if session else ""
            await reply.text(
                f"当前 Claude Session: `{cur or '无'}`\n\n用法: `/resume <session_id>`",
            )
            return
        if not session:
            session = session_manager.create_session(open_id, chat_id)
        await claude_cli_loop.cancel_and_wait(open_id)
        session.claude_session_id = parts[1].strip()
        session.context_tokens = 0
        session_manager.save_session(session)
        ws_config = preferences_manager.load_workspace_config(session.workspace)
        cur_pref = preferences_manager.get(open_id)
        target_config = ws_config if (ws_config and ws_config.complete) else (cur_pref if cur_pref.complete else None)

        if target_config:
            profiles = discover_profiles()
            profile_label = profiles.get(target_config.model, {}).get("label", target_config.model)
            from app.feishu.cards import build_workspace_config_reuse_card
            reuse_card = build_workspace_config_reuse_card(
                approval_id=uuid.uuid4().hex[:12],
                workspace=session.workspace,
                profile_label=profile_label,
                level=target_config.level,
                mode=target_config.mode,
                action_type="switch",
            )
            await reply.text(
                f"🔑 已切换到 Claude Session `{session.claude_session_id}`。",
            )
            await reply.card( reuse_card)
        else:
            preferences_manager.clear(open_id)
            await reply.text(
                f"🔑 已切换到 Claude Session `{session.claude_session_id}`。\n"
                "运行参数已清空，请依次设置 `/model`、`/level`、`/mode`。",
            )
        return
    # --- /continue [prompt]: resume most recent session via native --continue ---
    if text_lower.startswith("/continue"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id)

        arg = parts[1].strip() if len(parts) > 1 else ""
        # /continue <session_id> 形式不再有意义（claude 总是恢复最近会话），
        # 但仍接受参数当作普通提示处理。
        prompt = arg if arg else "继续上次的任务"

        # Force --continue even if myclaw has no recorded session: claude
        # looks up the most recent session in the workspace itself. Sentinel
        # just makes _start_process append --continue.
        if not session.claude_session_id:
            session.claude_session_id = "__continue__"
            session_manager.save_session(session)
            preferences_manager.clear(open_id)
        await _run_claude(prompt, open_id, session, skip_classify=True)
        return

    # --- /new: reset native session and runtime preferences ---
    if text_lower == "/new":
        await claude_cli_loop.cancel_and_wait(open_id)
        session = session_manager.reset_user_session(open_id)

        # Create a cli-born stub session via PTY so the local claude picker
        # can see this session (SDK-launched sessions get tagged sdk-cli and
        # are filtered out of the picker). On failure, fall back to legacy
        # behavior — first message will launch with sdk-cli.
        stub_session_id: str | None = None
        stub_error: str | None = None
        try:
            from app.agent.pty_stub import create_cli_born_session
            stub_session_id = create_cli_born_session(session.workspace)
        except Exception as e:
            stub_error = f"{type(e).__name__}: {e}"
            logger.warning("PTY stub creation failed: %s", stub_error)

        if stub_session_id:
            session.claude_session_id = stub_session_id
            session_manager.save_session(session)
            session_line = (
                f"🔑 Claude Session: `{stub_session_id}`\n"
                f"✅ 已创建 cli-born stub，本机 picker 可见"
            )
        else:
            session_line = (
                f"🔑 Claude Session: `待生成 (首条消息后以 sdk-cli 启动)`\n"
                f"⚠️ PTY stub 创建失败：{stub_error}\n"
                f"   会话仍可正常使用"
            )

        ws_config = preferences_manager.load_workspace_config(session.workspace)
        if ws_config and ws_config.complete:
            profiles = discover_profiles()
            profile_label = profiles.get(ws_config.model, {}).get("label", ws_config.model)
            from app.feishu.cards import build_workspace_config_reuse_card
            reuse_card = build_workspace_config_reuse_card(
                approval_id=uuid.uuid4().hex[:12],
                workspace=session.workspace,
                profile_label=profile_label,
                level=ws_config.level,
                mode=ws_config.mode,
                action_type="new",
            )
            await reply.text(
                f"✨ 新会话已重置。\n"
                f"📁 工作区: `{session.workspace}`\n"
                f"{session_line}",
            )
            await reply.card( reuse_card)
        else:
            preferences_manager.clear(open_id)
            await reply.text(
                f"✨ 新会话已重置。\n"
                f"📁 工作区: `{session.workspace}`\n"
                f"{session_line}\n"
                "运行参数已清空，请依次设置 `/model`、`/level`、`/mode`。",
            )
        return
    # --- /clean: remove old session files, keep only current ---
    if text_lower == "/clean":
        session = session_manager.get_user_session(open_id)
        if session:
            session_manager.clean_old_sessions(open_id)
            await reply.text(
                f"已清理旧会话文件，当前会话: `{session.session_id}`",
            )
        else:
            await reply.text( "没有活跃会话。")
        return

    # --- /compact: trigger CLI's built-in compact on current session ---
    if text_lower == "/compact":
        if not settings.compact_enabled:
            await reply.text( "压缩功能已禁用。请联系管理员开启。")
            return
        session = session_manager.get_user_session(open_id)
        if not session or not session.claude_session_id:
            await reply.text(
                "没有活跃的 Claude 会话，无法压缩。\n"
                "先发一条消息启动会话，上下文不足时再使用 `/compact`。",
            )
            return
        await reply.text(
            "🧹 已启动上下文压缩 (Compact)，正在为您整理与压缩当前 Session 的上下文历史...",
        )
        await _run_claude("/compact", open_id, session, skip_classify=True)
        return

    # --- /mem: workspace memo in CLAUDE.md ---
    # `/mem`           → show current content
    # `/mem <text>`    → append a line
    # `/mem clear`     → wipe
    if text_lower == "/mem" or text_lower.startswith("/mem "):
        arg = text[4:].strip()  # text after "/mem"
        session = session_manager.get_user_session(open_id)
        workspace = session.workspace if session else settings.get_default_workspace()
        memory_md = Path(workspace) / "CLAUDE.md"

        # `/mem` (no arg) → list current memory
        if not arg:
            try:
                if memory_md.exists():
                    body = memory_md.read_text("utf-8")
                else:
                    await reply.text(
                        f"📝 当前工作区 `{workspace}` 还没有 CLAUDE.md。\n"
                        f"用法：`/mem <内容>` 追加；`/mem clear` 清空。",
                    )
                    return
                char_count = len(body)
                PREVIEW_LIMIT = 4000
                if char_count <= PREVIEW_LIMIT:
                    preview = body
                    trunc_note = ""
                else:
                    preview = body[:PREVIEW_LIMIT]
                    trunc_note = f"\n\n…（共 {char_count} 字符，仅显示前 {PREVIEW_LIMIT}）"
                await reply.text(
                    f"📝 `{workspace}` 的 CLAUDE.md（{char_count} 字符）：\n```\n{preview}```{trunc_note}",
                )
            except Exception as e:
                await reply.text( f"读取失败: {e}")
            return

        # `/mem clear` → wipe memory file
        if arg.lower() == "clear":
            try:
                if memory_md.exists():
                    memory_md.unlink()
                    await reply.text( f"已清空 `{workspace}` 下的 CLAUDE.md。",
                    )
                else:
                    await reply.text( f"`{workspace}` 下没有 CLAUDE.md，无需清空。",
                    )
            except Exception as e:
                await reply.text( f"清空失败: {e}")
            return

        # `/mem <text>` → append
        content = arg
        try:
            if memory_md.exists():
                existing = memory_md.read_text("utf-8").rstrip("\n")
                memory_md.write_text(existing + "\n" + content + "\n", "utf-8")
            else:
                memory_md.write_text(content + "\n", "utf-8")
            await reply.text( f"已记录到 `{workspace}` 下的 CLAUDE.md")
        except Exception as e:
            await reply.text( f"写入失败: {e}")
    # --- /balance [profile]: query API balance for DeepSeek / GLM ---
    if text_lower == "/balance" or text_lower.startswith("/balance "):
        target_profile = text[8:].strip() or None
        from app.balance import get_profile_balance
        from app.feishu.cards import build_balance_card

        await reply.text( "🔍 正在查询 API 供应商余额...")
        balance_res = await get_profile_balance(target_profile)
        card = build_balance_card(balance_res)
        await reply.card( card)
        return

    # --- /notes: write output or text to notes.md ---
    # `/notes last`    → write last execution's final output to notes.md
    # `/notes <task_id>` → write execution output of specific task_id to notes.md
    # `/notes <xxx>`   → write custom text xxx to notes.md
    if (
        text_lower == "/notes"
        or text_lower.startswith("/notes ")
        or text_lower.startswith("/notes:")
        or text_lower.startswith("/notes：")
    ):
        if text_lower.startswith("/notes:") or text_lower.startswith("/notes："):
            raw_arg = text[7:].strip()
        else:
            raw_arg = text[6:].strip()

        clean_arg = raw_arg.lstrip(":：").strip()
        if clean_arg.lower().startswith("task:") or clean_arg.lower().startswith("task_id:"):
            clean_arg = clean_arg.split(":", 1)[1].strip()

        if not clean_arg:
            await reply.text(
                "📝 用法：\n"
                "• `/notes last` — 追加上一次任务执行完成的最终输出到 `notes.md`\n"
                "• `/notes <task_id>` — 追加特定 task_id 的执行完成卡片内容到 `notes.md`\n"
                "• `/notes <内容>` — 将自定义文本追加写入 `notes.md`",
            )
            return

        session = session_manager.get_user_session(open_id)
        workspace = session.workspace if session else settings.get_default_workspace()
        notes_md = Path(workspace) / "notes.md"

        content_to_write = ""
        source_desc = ""

        # 1. Check if 'last'
        if clean_arg.lower() == "last":
            last_res = claude_cli_loop.get_last_result(open_id)
            if not last_res or not last_res.text:
                await reply.text( "⚠️ 未找到上一次执行完成的结果记录。",
                )
                return
            content_to_write = last_res.text
            source_desc = f"上一次任务 (`{last_res.task_id}`)"
        else:
            # 2. Check if clean_arg matches a recorded task_id
            task_res = claude_cli_loop.get_task_result(open_id, clean_arg)
            if task_res and task_res.text:
                content_to_write = task_res.text
                source_desc = f"任务 (`{task_res.task_id}`)"
            elif re.match(r"^[a-fA-F0-9]{12}$", clean_arg):
                await reply.text(
                    f"⚠️ 未找到任务 ID 为 `{clean_arg}` 的历史执行记录。\n"
                    f"💡 提示：服务重启或重置会清空内存记录；您可使用 `/notes last` 追加最近一次任务结果。",
                )
                return
            else:
                # 3. Otherwise treat raw_arg as custom text
                content_to_write = raw_arg
                source_desc = "自定义笔记内容"

        try:
            notes_md.parent.mkdir(parents=True, exist_ok=True)
            if notes_md.exists():
                existing = notes_md.read_text("utf-8")
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                new_content = existing + content_to_write + "\n"
            else:
                new_content = content_to_write + "\n"

            notes_md.write_text(new_content, "utf-8")

            preview = content_to_write[:200] + ("..." if len(content_to_write) > 200 else "")
            await reply.text(
                f"✅ 已将{source_desc}追加写入到工作区下的 `notes.md`：\n\n"
                f"📁 文件：`{notes_md}`\n"
                f"📝 内容预览：\n```\n{preview}\n```",
            )
        except Exception as e:
            logger.exception("Failed to write to notes.md")
            await reply.text( f"❌ 写入 `notes.md` 失败：{e}")
        return

    # --- /sh <command>: execute shell command in workspace ---
    if text_lower.startswith("/sh "):
        cmd = text[4:].strip()
        if not cmd:
            await reply.text( "用法: `/sh <command>`，例如 `/sh mkdir ZhiWang`",
            )
            return
        session = session_manager.get_user_session(open_id)
        workspace = session.workspace if session else settings.get_default_workspace()
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            out = _decode_output(stdout).strip()
            err = _decode_output(stderr).strip()
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            await reply.text( f"⏱️ 命令超时(30s): `{cmd}`")
            return
        except Exception as e:
            await reply.text( f"执行失败: `{e}`")
            return
        parts = [f"📁 `{workspace}`", f"▶ `{cmd}`"]
        if out:
            display = out if len(out) <= 4000 else out[:3950] + "\n... (输出已截断)"
            parts.append(f"```\n{display}\n```")
        if err:
            display = err if len(err) <= 1000 else err[:950] + "\n..."
            parts.append(f"⚠️ stderr:\n```\n{display}\n```")
        parts.append(f"退出码: {proc.returncode}")
        await reply.text( "\n".join(parts))
        return

    # --- Default: run Claude CLI ---
    await _run_claude(text, open_id, chat_id=chat_id if is_group else "")


async def _run_claude(
    prompt: str,
    open_id: str,
    session: Session | None = None,
    skip_classify: bool = False,
    resume_session_id: str | None = None,
    chat_id: str = "",
) -> None:
    """Run Claude CLI with optional model selection card for first message.

    resume_session_id: if set, start claude with ``--resume <id>`` to
    continue a specific session (used by /resume <id>). When None, the
    claude_session_id on the session drives ``--continue``.
    """
    audit_logger.log_command_received(open_id, prompt, "started")
    reply = ReplyChannel(open_id, chat_id, bool(chat_id))
    try:
        if not session:
            session = session_manager.get_user_session(open_id)
        if not session:
            session = session_manager.create_session(open_id, chat_id)
        elif chat_id and session.chat_id != chat_id:
            # 旧 session 可能带空 chat_id（修复前创建），群消息到达时刷新，
            # 保证后续配置卡片/进度卡片回到群里而不是发进无效私聊
            session.chat_id = chat_id
            session_manager.save_session(session)

        preferences = preferences_manager.get(open_id)
        profiles = discover_profiles()

        # After a recent /cd into a new workspace, ask whether to reuse last
        # provider/model/mode before running. Only trigger if prefs are complete
        # (otherwise the existing "first-time setup" path below handles it).
        if session.pending_reuse_confirm and preferences.complete:
            session.pending_prompt = prompt
            session_manager.save_session(session)
            from app.feishu.cards import build_reuse_last_card
            profile_label = profiles.get(preferences.model, {}).get("label", preferences.model)
            await reply.card(
                build_reuse_last_card(
                    approval_id=uuid.uuid4().hex[:12],
                    profile_label=profile_label,
                    level=preferences.level,
                    mode=preferences.mode,
                ),
            )
            return

        need_provider = preferences.model not in profiles
        need_level = preferences.level not in ("haiku", "sonnet", "opus")
        need_mode = preferences.mode not in ("h", "m", "l")

        if need_provider or need_level or need_mode:
            session.pending_prompt = prompt
            session_manager.save_session(session)

            missing_cards = []
            if need_provider:
                from app.feishu.cards import build_profile_selection_card
                missing_cards.append(
                    build_profile_selection_card(
                        approval_id=uuid.uuid4().hex[:12],
                        profiles=profiles,
                        active_profile="",
                    )
                )
            if need_level:
                from app.feishu.cards import build_model_selection_card
                missing_cards.append(
                    build_model_selection_card(
                        approval_id=uuid.uuid4().hex[:12],
                        current_model="",
                    )
                )
            if need_mode:
                from app.feishu.cards import build_mode_selection_card
                missing_cards.append(
                    build_mode_selection_card(
                        approval_id=uuid.uuid4().hex[:12],
                        active_mode="",
                    )
                )

            for card in missing_cards:
                await reply.card( card)

            prompt_preview = prompt if len(prompt) <= 30 else prompt[:27] + "..."
            await reply.text(
                f"💡 任务已安全暂存：`{prompt_preview}`\n"
                f"系统检测到有 {len(missing_cards)} 项初始配置尚未设置。请直接在上方卡片中点选完成，全部设置就绪后系统将全自动重新开始为您执行任务！",
            )
            return

        agent_result = await claude_cli_loop.send_and_wait(
            prompt=prompt,
            open_id=open_id,
            workspace=session.workspace,
            model=preferences.level,
            approval_mode=preferences.mode,
            profile_name=preferences.model,
            claude_session_id=session.claude_session_id or None,
            resume_session_id=resume_session_id,
            chat_id=chat_id,
        )
        # Auto-heal: if process failed because Session ID does not exist on disk
        err_msg = (agent_result.error or "") + (agent_result.text or "")
        if "No conversation found with session ID" in err_msg or "进程通信中断" in err_msg:
            logger.warning("Session ID invalid/lost for user %s, auto-healing...", open_id)
            session.claude_session_id = "__continue__"
            session_manager.save_session(session)
            await reply.text(
                "ℹ️ 检测到历史会话 ID 已在磁盘上失效，已全自动为您重新生成会话并执行任务...",
            )
            await _run_claude(prompt, open_id, session, skip_classify=True)
            return

        # Process was killed (switch/stop/new) — caller already notified user
        if agent_result.status == "cancelled":
            return

        # Preserve the last known-good ID on transient execution failures.
        # /new and workspace changes are the explicit reset operations.
        if agent_result.session_id:
            session.claude_session_id = agent_result.session_id
            append_to_claude_history(session.workspace, agent_result.session_id, prompt)
        if agent_result.input_tokens > 0:
            session.context_tokens = agent_result.input_tokens
        # NOTE: agent_messages intentionally not appended — claude already
        # persists the full conversation in ~/.claude/projects/*/<sid>.jsonl,
        # which _iter_claude_session_messages reads back on demand.

        # Persist session to disk
        session_manager.save_session(session)

        try:
            session.status = TaskStatus(agent_result.status)
        except ValueError:
            session.status = TaskStatus.COMPLETED

        # Result summary is shown on the progress card itself; no extra text message.
        audit_logger.log_command_received(open_id, prompt, agent_result.status)

    except Exception as e:
        logger.exception("Dispatch error for user %s", open_id)
        try:
            await reply.text(
                f"处理指令时出错：{type(e).__name__}: {e}",
            )
        except Exception:
            pass
