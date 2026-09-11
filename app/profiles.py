"""Codex model registry — discover models available to the local ChatGPT login.

Codex authenticates via ~/.codex/auth.json (machine-level ChatGPT login);
no provider env vars or API keys are injected anywhere. The model list is
read from codex's own models cache, with a static fallback for offline /
missing cache cases.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("myclaw.profiles")

MYCLAW_ROOT = Path(__file__).resolve().parent.parent  # app/profiles.py → app/ → root
CONFIG_DIR = MYCLAW_ROOT / "config"

# Fallback when models_cache.json is missing/unreadable. Keep in sync with
# what the ChatGPT-login codex offers (visibility=list in models cache).
_FALLBACK_MODELS: dict[str, dict] = {
    "gpt-5.6-terra": {
        "name": "gpt-5.6-terra",
        "label": "GPT-5.6-Terra (旗舰)",
        "description": "最强推理，适合复杂任务",
    },
    "gpt-5.6-luna": {
        "name": "gpt-5.6-luna",
        "label": "GPT-5.6-Luna (均衡)",
        "description": "速度与深度均衡，日常推荐",
    },
    "gpt-5.5": {
        "name": "gpt-5.5",
        "label": "GPT-5.5 (轻量)",
        "description": "轻量快速，适合简单任务",
    },
}

# Reasoning effort values accepted as preferences.level / -c model_reasoning_effort.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _models_cache_path() -> Path:
    import os
    home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "models_cache.json"


def discover_models() -> dict[str, dict]:
    """Codex models available to this machine's login.

    Returns:
        Dict mapping model slug to model info:
        {slug: {name, label, description}}
    """
    models: dict[str, dict] = {}
    try:
        data = json.loads(_models_cache_path().read_text("utf-8"))
        for entry in data.get("models", []):
            if entry.get("visibility") not in (None, "list"):
                continue
            slug = entry.get("slug", "")
            if not slug:
                continue
            display = entry.get("display_name") or slug
            models[slug] = {
                "name": slug,
                "label": display,
                "description": (entry.get("description") or "")[:80],
            }
    except Exception as e:
        logger.warning("Failed to read codex models cache: %s", e)
    if not models:
        return dict(_FALLBACK_MODELS)
    return models


def test_codex_auth() -> tuple[bool, str]:
    """Verify the machine's codex login via `codex login status` (no quota use)."""
    import shutil
    resolved = shutil.which("codex")
    if not resolved:
        return False, "未找到 codex CLI"
    try:
        proc = subprocess.run(
            [resolved, "login", "status"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if "Logged in" in output:
            return True, output.strip().splitlines()[0][:120]
        return False, output.strip()[:200] or "未检测到登录态"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
