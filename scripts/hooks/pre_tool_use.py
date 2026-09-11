#!/usr/bin/env python3
"""PreToolUse hook script for Claude Code.

Claude Code calls this script before executing any tool.
It forwards the tool info to the Python server via HTTP,
which sends a Feishu approval card and blocks until the user responds.

Input:  JSON on stdin  {"tool_name": "...", "tool_input": {...}, "session_id": "..."}
Output: JSON on stdout {"permissionDecision": "allow"/"deny", "permissionDecisionReason": "..."}

Schema note: Claude CLI ≥2.1 expects ``permissionDecision`` (allow/deny/ask) or
``decision`` (approve/block). Older ``decision: "allow"/"deny"`` is invalid and
triggers "Hook JSON output validation failed" warnings on every tool call.
"""
import json
import os
import sys

# Use urllib to avoid needing requests/pip install
import urllib.request
import urllib.error

# Server URL — same host/port as the main FastAPI app
HOST = os.environ.get("MYCLAW_HOST", "localhost")
PORT = os.environ.get("MYCLAW_PORT", "8080")
HOOK_URL = f"http://{HOST}:{PORT}/hooks/pre_tool_use"
TIMEOUT = int(os.environ.get("MYCLAW_HOOK_TIMEOUT", "1800"))


def main():
    # Claude writes hook JSON as UTF-8; Windows otherwise decodes stdin as GBK.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")

    # Read hook input from stdin
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Can't parse — deny for security (fail-close)
        print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": "MyClaw 审批 Hook 解析输入失败"}))
        return

    # Forward to Python server
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        HOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(result, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(json.dumps({
            "permissionDecision": "deny",
            "permissionDecisionReason": f"MyClaw 审批服务异常 (HTTP {e.code})，出于安全保护已拒绝工具执行: {body[:150]}",
        }))
    except Exception as e:
        # Fail-close: deny on connection error or timeout for security
        print(json.dumps({
            "permissionDecision": "deny",
            "permissionDecisionReason": f"MyClaw 审批服务断连 ({type(e).__name__})，出于安全保护已拒绝工具执行",
        }))


if __name__ == "__main__":
    main()
