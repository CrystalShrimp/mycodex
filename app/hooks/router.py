"""Hook callback endpoints for Claude Code PreToolUse approval.

Approval modes (per session):
  h = 高容忍: all tools auto-allowed (no approval card)
  m = 中风险: high-risk tools need approval card
  l = 低容忍: all tools need approval card

Flow:
  Claude Code → PreToolUse hook script → HTTP POST /hooks/pre_tool_use
  → this handler checks mode → sends Feishu card (if needed) → returns decision
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Request

from app.agent.cli_loop import session_registry
from app.approval.manager import approval_manager
from app.feishu.client import feishu_client
from app.feishu.cards import build_tool_approval_card

logger = logging.getLogger("myclaw.hooks")

router = APIRouter(prefix="/hooks", tags=["hooks"])

# Fallback defaults used when config/approval_rules.json is missing or invalid.
# Keep these in sync with config/approval_rules.json so a missing file degrades
# gracefully to the documented behavior.
_DEFAULT_RULES = {
    "high_risk_tools": ["Write", "Edit", "NotebookEdit"],
    "safe_command_patterns": [
        "ls", "dir", "cat", "head", "tail", "find", "grep", "which", "where",
        "cd", "pwd", "whoami", "echo", "type", "wc", "sort", "uniq", "diff", "file",
        "stat", "du", "df", "uname", "hostname", "date", "env", "printenv",
        "git status", "git log", "git diff", "git branch", "git remote", "git show", "git tag",
        "python --version", "python3 --version", "node --version", "npm --version",
        "pip list", "pip show", "pip --version", "uv --version", "uv run python -c",
        "ollama list", "ollama --version",
        "test ", "test -f", "test -d", "test -e",
    ],
    "high_risk_keywords": [
        "rm ", "rmdir", "del ", "format", "shutdown", "reboot",
        "pip install", "npm install", "yarn add",
        "git push", "git reset", "git checkout",
        "chmod", "chown", "mkfs",
        "curl -X POST", "curl -X PUT", "curl -X DELETE",
        "wget ",
        "> ", ">> ",
        "ssh ", "scp ",
    ],
}


def _load_approval_rules() -> dict:
    """Load approval rules from settings.approval_rules_path.

    Falls back to _DEFAULT_RULES on any error (file missing, JSON invalid,
    settings not yet initialized). Logs a warning so misconfigurations are
    visible without breaking the hook endpoint.
    """
    try:
        from config.settings import settings
        path_str = settings.approval_rules_path
    except Exception:
        path_str = None

    if not path_str:
        # Settings not loaded yet (e.g. import time during module init) — use default.
        path_str = "./config/approval_rules.json"

    try:
        path = Path(path_str)
        if not path.is_absolute():
            # Relative to project root (parent of app/).
            path = Path(__file__).resolve().parent.parent.parent / path_str
        if not path.exists():
            logger.info("Approval rules file %s missing, using built-in defaults", path)
            return _DEFAULT_RULES
        data = json.loads(path.read_text(encoding="utf-8"))
        # Schema sanity check
        for key in ("high_risk_tools", "safe_command_patterns", "high_risk_keywords"):
            if key not in data:
                logger.warning("Approval rules file missing key %r, using defaults", key)
                return _DEFAULT_RULES
        logger.info("Loaded approval rules from %s", path)
        return data
    except Exception as e:
        logger.warning("Failed to load approval rules from %s: %s, using defaults", path_str, e)
        return _DEFAULT_RULES


_RULES = _load_approval_rules()

# Public aliases (keep names stable so other modules / tests can import these).
HIGH_RISK_TOOLS = set(_RULES["high_risk_tools"])
_SAFE_COMMAND_PATTERNS = tuple(_RULES["safe_command_patterns"])
_HIGH_RISK_KEYWORDS = tuple(_RULES["high_risk_keywords"])

# Debug: track recent hook calls
_hook_log: list[str] = []


@router.post("/pre_tool_use")
async def pre_tool_use(request: Request) -> dict:
    """Called by the PreToolUse hook script.

    Input:  {"tool_name": "...", "tool_input": {...}, "session_id": "..."}
    Output: {"decision": "allow"/"deny", "reason": "..."}
    """
    try:
        data = await request.json()
    except Exception:
        return {"permissionDecision": "allow", "permissionDecisionReason": "invalid input, allowing"}

    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {})
    claude_session_id = data.get("session_id", "")

    logger.info(
        "PreToolUse hook: tool=%s session=%s registry_keys=%s",
        tool_name, claude_session_id, list(session_registry.keys()),
    )
    _hook_log.append(f"tool={tool_name} session={claude_session_id} registry={list(session_registry.keys())}")

    # Look up session info
    reg = session_registry.get(claude_session_id)
    if not reg:
        logger.warning("No session registry for %s, allowing", claude_session_id)
        _hook_log.append("→ ALLOW (no registry entry)")
        return {"permissionDecision": "allow", "permissionDecisionReason": "session not tracked"}

    open_id = reg["open_id"]
    mode = reg.get("approval_mode", "m")  # 'h' = high risk (strict), 'm' = medium (balanced), 'l' = low risk (auto)

    # --- Mode l (⚡ 全自动模式 / Low Risk Auto): 100% 自动放行一切工具（包含 Edit, Write, Bash） ---
    if mode == "l":
        logger.info("Mode l (Low Risk Auto): auto-allowing tool %s", tool_name)
        _hook_log.append(f"→ ALLOW ({tool_name} auto-allowed in low-risk mode l)")
        return {"permissionDecision": "allow", "permissionDecisionReason": "low-risk auto mode l"}

    # --- Mode m (⚖️ 平衡模式 / Medium): 自动放行只读/安全工具，仅拦截高风险写操作 ---
    if mode == "m":
        needs_approval = _is_high_risk(tool_name, tool_input)
        if not needs_approval:
            logger.info("Mode m (Balanced): auto-allowing tool %s", tool_name)
            _hook_log.append(f"→ ALLOW ({tool_name} low risk in mode m)")
            return {"permissionDecision": "allow", "permissionDecisionReason": "balanced mode m"}

    # --- Mode h (🛡️ 严格模式 / High Risk) 或 高风险写操作: 发送确认卡片 ---
    approval_id = uuid.uuid4().hex[:12]
    _hook_log.append(f"→ SENDING CARD approval_id={approval_id} to open_id={open_id}")

    async def _send_card(aid: str) -> None:
        card = build_tool_approval_card(
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=claude_session_id,
        )
        try:
            result = await feishu_client.send_card(open_id, card)
            _hook_log.append(f"→ CARD SENT ok, msg_id={result.get('data', {}).get('message_id', '?')}")
        except Exception as e:
            _hook_log.append(f"→ CARD SEND FAILED: {e}")
            raise

    # Use the actual risk analysis result for both audit log and approval tracking
    actual_high_risk = (mode == "h") or _is_high_risk(tool_name, tool_input)

    approved = await approval_manager.request_tool_approval(
        tool_name=tool_name,
        tool_arguments=tool_input,
        risk_level="high" if actual_high_risk else "low",
        send_card_fn=_send_card,
        open_id=open_id,
    )

    if not approved:
        logger.warning("Tool approval denied/expired for user %s, cancelling CLI loop to prevent retry spam.", open_id)
        from app.agent.cli_loop import claude_cli_loop
        claude_cli_loop.cancel_by_user(open_id)

    decision = "allow" if approved else "deny"
    reason = "用户已批准" if approved else "用户已拒绝"
    _hook_log.append(f"→ {decision} ({reason})")
    logger.info("PreToolUse decision: %s for %s (mode=%s)", decision, tool_name, mode)

    return {"permissionDecision": decision, "permissionDecisionReason": reason}


@router.get("/debug/hooks")
async def debug_hooks():
    """Debug endpoint to see recent hook calls."""
    from app.agent.cli_loop import session_registry as sr
    return {
        "session_registry": {k: {kk: vv for kk, vv in v.items() if kk != "task_id"} for k, v in sr.items()},
        "hook_log": _hook_log[-20:],
    }


def _is_high_risk(tool_name: str, arguments: dict | None = None) -> bool:
    """Determine if a tool call is high-risk.

    - Write/Edit/NotebookEdit: always high-risk
    - Bash: analyze the actual command content
    - Everything else: low-risk
    """
    if tool_name in HIGH_RISK_TOOLS:
        return True

    if tool_name == "Bash" and arguments:
        cmd = arguments.get("command", "").strip()
        # Extract first command in a chain
        cmd_first = cmd.split(";")[0].split("&&")[0].split("|")[0].strip()
        cmd_lower = cmd_first.lower()

        # Check high-risk keywords first
        for kw in _HIGH_RISK_KEYWORDS:
            if kw in cmd_lower:
                return True

        # Check safe commands
        for safe in _SAFE_COMMAND_PATTERNS:
            if cmd_lower == safe or cmd_lower.startswith(safe + " ") or cmd_lower.startswith(safe + "\t"):
                return False

        # Also allow: python -c "..." (inline scripts, usually for checks)
        if cmd_lower.startswith("python -c ") or cmd_lower.startswith("python3 -c "):
            return False

        # Unknown command → high-risk by default
        return True

    # Non-Bash, non-Write/Edit tools: low-risk
    return False
