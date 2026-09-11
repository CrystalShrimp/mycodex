from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from app.profiles import MYCODEX_ROOT

logger = logging.getLogger("mycodex.preferences")


@dataclass
class UserPreferences:
    """Mycodex runtime choices kept outside codex thread state."""

    model: str = ""  # codex model slug, for example gpt-5.6-terra
    level: str = ""  # reasoning effort: low/medium/high/xhigh/max
    mode: str = ""   # h, m or l

    @property
    def complete(self) -> bool:
        return bool(self.model and self.level and self.mode)


class PreferencesManager:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or (MYCODEX_ROOT / ".preferences")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, open_id: str) -> Path:
        return self._state_dir / f"{open_id}.json"

    def get(self, open_id: str) -> UserPreferences:
        path = self._path(open_id)
        if not path.exists():
            return UserPreferences()
        try:
            data = json.loads(path.read_text("utf-8"))
            return UserPreferences(
                model=str(data.get("model", "")),
                level=str(data.get("level", "")),
                mode=str(data.get("mode", "")),
            )
        except Exception as exc:
            logger.warning("Failed to load preferences for %s: %s", open_id, exc)
            return UserPreferences()

    def save(self, open_id: str, preferences: UserPreferences) -> None:
        payload = json.dumps(asdict(preferences), indent=2, ensure_ascii=False)
        with self._lock:
            self._path(open_id).write_text(payload, "utf-8")

    def clear(self, open_id: str) -> UserPreferences:
        preferences = UserPreferences()
        self.save(open_id, preferences)
        return preferences

    def _workspace_config_path(self, workspace: str) -> Path | None:
        if not workspace:
            return None
        try:
            ws_dir = Path(workspace).resolve()
            if not ws_dir.exists() or not ws_dir.is_dir():
                return None
            mycodex_dir = ws_dir / ".mycodex"
            mycodex_dir.mkdir(parents=True, exist_ok=True)
            return mycodex_dir / "config.json"
        except Exception:
            return None

    def load_workspace_config(self, workspace: str) -> UserPreferences | None:
        """Load workspace-level mycodex_config.json if it exists and is complete."""
        config_path = self._workspace_config_path(workspace)
        if not config_path or not config_path.exists():
            return None
        try:
            data = json.loads(config_path.read_text("utf-8"))
            pref = UserPreferences(
                model=str(data.get("model", "")),
                level=str(data.get("level", "")),
                mode=str(data.get("mode", "")),
            )
            return pref if pref.complete else None
        except Exception as exc:
            logger.warning("Failed to load workspace config from %s: %s", workspace, exc)
            return None

    def save_workspace_config(self, workspace: str, preferences: UserPreferences) -> None:
        """Save complete preferences to workspace/.claude/mycodex_config.json."""
        if not preferences.complete:
            return
        config_path = self._workspace_config_path(workspace)
        if not config_path:
            return
        try:
            payload = json.dumps(asdict(preferences), indent=2, ensure_ascii=False)
            config_path.write_text(payload, "utf-8")
            logger.info("Saved workspace config to %s", config_path)
        except Exception as exc:
            logger.warning("Failed to save workspace config to %s: %s", workspace, exc)


preferences_manager = PreferencesManager()
