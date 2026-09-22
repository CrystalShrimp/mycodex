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


GLOBAL_PREFERENCES_FILE = MYCODEX_ROOT / "config" / "global_preferences.json"


class PreferencesManager:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or (MYCODEX_ROOT / ".preferences")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _get_system_defaults(self) -> UserPreferences:
        from app.profiles import discover_models
        from config.settings import settings
        models = discover_models()
        default_model = getattr(settings, "codex_default_model", "") or "gpt-5.6-terra"
        if default_model not in models and models:
            default_model = next(iter(models.keys()))
        default_level = "medium"
        default_mode = getattr(settings, "approval_mode", "") or "m"
        return UserPreferences(
            model=default_model,
            level=default_level,
            mode=default_mode,
        )

    def get_global(self) -> UserPreferences:
        defaults = self._get_system_defaults()
        if not GLOBAL_PREFERENCES_FILE.exists():
            return defaults
        try:
            data = json.loads(GLOBAL_PREFERENCES_FILE.read_text("utf-8"))
            return UserPreferences(
                model=str(data.get("model", "") or defaults.model),
                level=str(data.get("level", "") or defaults.level),
                mode=str(data.get("mode", "") or defaults.mode),
            )
        except Exception as exc:
            logger.warning("Failed to load global preferences: %s", exc)
            return defaults

    def save_global(self, preferences: UserPreferences) -> None:
        payload = json.dumps(asdict(preferences), indent=2, ensure_ascii=False)
        with self._lock:
            GLOBAL_PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
            GLOBAL_PREFERENCES_FILE.write_text(payload, "utf-8")

    def get(self, open_id: str) -> UserPreferences:
        """获取当前配置。优先读取系统全局配置，保证切换目录及新项目时无需重新配置。"""
        return self.get_global()

    def save(self, open_id: str, preferences: UserPreferences) -> None:
        """保存配置。同步写入全局配置，一处调整全局生效。"""
        self.save_global(preferences)

    def clear(self, open_id: str) -> UserPreferences:
        """重置配置回系统默认值。"""
        defaults = self._get_system_defaults()
        self.save_global(defaults)
        return defaults

    def load_workspace_config(self, workspace: str) -> UserPreferences | None:
        """已废除项目内配置记忆，统一使用全局配置。"""
        return None

    def save_workspace_config(self, workspace: str, preferences: UserPreferences) -> None:
        """已废除项目内配置记忆，不再往项目目录下写入配置。"""
        pass


preferences_manager = PreferencesManager()
