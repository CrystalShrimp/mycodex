"""会话与群聊注册表（平台无关）。

从 app/feishu/events.py 原样搬迁；会话 key 规则（平台前缀时代）：
飞书 f:g:<chat_id>:<open_id> / f:p:<open_id>；企微 w:g:... / w:p:...。
群聊同群每人独立。落盘文件名会把 ":" 替换为 "_"（Windows 不允许冒号）。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

from app.channel.base import PLATFORM_FEISHU, UserTarget
from app.models.schemas import Session
from config.settings import settings

logger = logging.getLogger("mycodex.sessions")


def _skey(platform: str, open_id: str, chat_id: str = "", is_group: bool = False) -> str:
    """会话 key：f:/w: 平台前缀 + 群聊 g:<chat_id>:<open_id>（同群每人独立）/ 私聊 p:<open_id>。"""
    p = platform[0]
    if is_group and chat_id:
        return f"{p}:g:{chat_id}:{open_id}"
    return f"{p}:p:{open_id}"


def skey_for(target: UserTarget) -> str:
    return _skey(target.platform, target.user_id, target.chat_id, target.is_group)


class GroupChatRegistry:
    """记录哪些 chat_id 是群聊，重启后从磁盘恢复。

    飞书侧用于把卡片回调路由回原群（回调可能不带 chat_type）；
    企微回调自带 chatid，但注册表同样适用。
    """

    def __init__(self, state_dir: Path) -> None:
        self._file = state_dir / "group_chats.json"
        self._chats: set[str] = set()
        try:
            data = json.loads(self._file.read_text("utf-8"))
            self._chats = {c for c in data.get("chats", []) if isinstance(c, str)}
        except Exception:
            pass

    def register(self, chat_id: str) -> None:
        if not chat_id or chat_id in self._chats:
            return
        self._chats.add(chat_id)
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps({"chats": sorted(self._chats)}, ensure_ascii=False), "utf-8",
            )
        except Exception as e:
            logger.warning("Failed to persist group chats: %s", e)

    def is_group(self, chat_id: str) -> bool:
        return bool(chat_id) and chat_id in self._chats


group_chats = GroupChatRegistry(
    Path(settings.audit_log_path).parent / ".sessions"
)


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
        # Windows 文件名不允许 ":"，而私聊 skey 是 "p:ou_..."（会被 Path 当成
        # p 盘符导致写盘静默失败），必须替换成 "_"
        safe = open_id.replace(":", "_")
        return self._state_dir / f"{safe}.json"

    def _load_all(self) -> None:
        """Load persisted sessions on startup."""
        if not self._state_dir.exists():
            return
        for f in self._state_dir.glob("*.json"):
            if f.name == "group_chats.json":
                continue
            try:
                data = json.loads(f.read_text("utf-8"))
                session = Session(**data)
                key = session.user_open_id
                dirty = False
                # 旧版私聊文件键是裸 open_id（p: 前缀引入前写的），迁移到
                # 现行键并改存新文件名；群聊文件（oc_ 开头）不受影响
                if key.startswith("ou_"):
                    key = f"p:{key}"
                    session.user_open_id = key
                    dirty = True
                # 平台前缀时代：存量 p:/g: 键全部是飞书会话，统一补 f: 前缀
                if key.startswith(("p:", "g:")) and not key.startswith(("f:", "w:")):
                    key = f"f:{key}"
                    session.user_open_id = key
                    dirty = True
                # 隔离时代之前群聊操作会把群 chat_id 写进私聊会话（化石数据，
                # 已无读取方但会误导诊断），加载时清除
                if key.startswith(("p:", "f:p:")) and session.chat_id:
                    session.chat_id = ""
                    dirty = True
                if dirty:
                    self._save(session)
                    if f.name != f"{key.replace(':', '_')}.json":
                        try:
                            f.unlink()
                        except OSError:
                            pass
                with self._lock:
                    self._sessions[session.session_id] = session
                    self._user_sessions[key] = session.session_id
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
        """Remove stale state files of the caller's own context only.

        不能再删其他会话的文件——群聊/私聊/其他用户的状态互相独立，
        一个上下文里的 /clean 无权清理别人。
        """
        if not self._state_dir.exists():
            return
        prefix = open_id.replace(":", "_")
        for f in self._state_dir.glob(f"{prefix}*.json"):
            if f.name == "group_chats.json" or f.stem == prefix:
                continue
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
