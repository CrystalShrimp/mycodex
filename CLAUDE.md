# MyCodex

本地 Codex CLI 的 IM 网关（飞书 + 企业微信双平台长连接）。入口 `app/main.py`，平台适配 `app/feishu/` + `app/wecom/`，共享分发层 `app/dispatch/`，自动化配置 `auto_feishu/` + `auto_wecom/`（Playwright），安装向导 `scripts/setup_wizard.py`。

## Shell working-directory invariant

The shell working directory is NON-PERSISTENT STATE.

Hard rules:

1. NEVER rely on a previous Bash call's `cd`.
2. NEVER use a persistent directory change as part of the workflow.
3. Every Bash command must be independently executable from the repository root.
4. When a command must run inside a subdirectory:
   - Prefer tools with an explicit directory argument:
     - `git -C <dir> ...`
     - `npm --prefix <dir> ...`
   - Otherwise use an explicit subshell:
     - `(cd <dir> && <command>)`
5. NEVER execute `cd <dir>` and expect later tool calls to remain there.
6. NEVER execute `cd <dir> && <command>` directly in the persistent Claude Bash
   shell — wrap it in a subshell.
7. Hook/script paths MUST NOT depend on the current working directory.
8. A shell command changing cwd must never affect subsequent Edit, Write,
   Read, Grep, Glob, hook, or Bash operations.

Known incident (myclaw, same architecture): persistent `cd launcher_windows`
made a relative-path hook unresolvable and blocked ALL tools.

## File editing rules (encodings are load-bearing)

Launcher bats are MIXED-ENCODED on purpose:

- `MyCodex.bat`, `MyCodex-Restart.bat` — **GBK, no chcp** (they render in
  the default cp936 console of their own fresh window).
- `MyCodex-Setup.bat` — **UTF-8 + `chcp 65001`**.

Hard rules:

1. NEVER call Edit/Write on an existing `.bat`/`.cmd` that is not valid UTF-8 —
   they read/write UTF-8 and replace GBK Chinese with U+FFFD (console mojibake,
   eaten line endings, flash-close). Use python byte-level edits:
   read bytes → `decode('gbk')` to verify → `bytes.replace` → write CRLF.
   A PreToolUse guard (`scripts/hooks/guard_file_edit.py`, registered in
   `.claude/settings.json`) enforces this and fails open.
2. `.bat`/`.cmd` line endings MUST be CRLF. LF + `if (...)` blocks have
   flash-closed twice (2026-08, myclaw). After any bat edit verify: valid decode
   in its encoding, CRLF-only endings, no `\xef\xbf\xbd`.
3. `git show` restores bats with LF endings (autocrlf converts on checkout
   only) — always re-apply CRLF after restoring from git.

Known incident (2026-10-01, myclaw — same architecture): Edit on the GBK
`MyClaw-Restart.bat` corrupted its Chinese into U+FFFD → user-facing mojibake +
restart window flash-close.

