#!/usr/bin/env python3
"""PreToolUse guard for dev sessions: protect non-UTF-8 files from Edit/Write.

Why this exists (2026-10-01 incident): Edit/Write read and write UTF-8. The
launcher bats MyClaw.bat / MyCodex.bat / *-Restart.bat are GBK-encoded. Editing
one with Edit replaced every Chinese character with U+FFFD (b'\xef\xbf\xbd'),
which shows as mojibake in the cp936 console and can eat line endings so the
final `pause` stops holding (window flash-closes). Prose rules in memory failed
to prevent this; this hook makes the violation mechanically impossible.

Policy: block Edit/Write on an EXISTING .bat/.cmd that is not valid UTF-8.
Also block Write that would create a .bat/.cmd with LF-only line endings
(CRLF is required; LF + `if (...)` blocks have flash-closed twice before).

Fail-open: any unexpected error allows the tool call. This guard must never
take down a session.

Input:  JSON on stdin {"tool_name": "...", "tool_input": {...}}
Output: {"permissionDecision": "deny", "permissionDecisionReason": "..."} to block,
        {"permissionDecision": "allow"} (or empty output) to allow.
"""
import json
import os
import sys


def deny(reason: str):
    print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}, ensure_ascii=False))


def main():
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        try:
            data = json.load(sys.stdin)
        except Exception:
            return  # unparseable input -> allow (fail-open)

        if data.get("tool_name") not in ("Edit", "Write"):
            return

        tool_input = data.get("tool_input") or {}
        file_path = (tool_input.get("file_path") or "").strip()
        if not file_path or not file_path.lower().endswith((".bat", ".cmd")):
            return

        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                raw = f.read()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                deny(
                    "该 .bat/.cmd 是 GBK 等非 UTF-8 编码，Edit/Write 按 UTF-8 读写会把中文毁成 U+FFFD"
                    "（乱码+行尾丢失，2026-10-01 事故）。请改用 python 字节级操作："
                    "读 bytes -> decode('gbk') 校验 -> bytes 替换 -> 写回 CRLF。"
                )
                return

        # LF check applies to Write only: Write emits the content verbatim
        # (2026-08 LF incidents). Edit normalizes to the file's existing CRLF
        # (verified empirically 2026-10-01), so checking its diff would false-block.
        if data.get("tool_name") == "Write":
            content = tool_input.get("content") or ""
            if "\n" in content.replace("\r\n", ""):
                deny("bat/cmd 内容必须 CRLF 行尾（LF 会让 cmd 解析 ( ) 块闪退，已有两次事故）。请用 \\r\\n。")
                return
    except Exception:
        return  # fail-open: never break the session on guard's own error


if __name__ == "__main__":
    main()
    sys.exit(0)
