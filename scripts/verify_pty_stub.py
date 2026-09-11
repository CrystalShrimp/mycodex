"""Verify whether PTY-spawning claude produces a `cli`-born session stub.

Goal (single): launch interactive claude (NO SDK flags) via Windows PTY,
submit one uniquely-marked user message, then confirm the corresponding
JSONL record has entrypoint == "cli".

Run:
    .venv\\Scripts\\python.exe scripts\\verify_pty_stub.py <real_workspace>
    .venv\\Scripts\\python.exe scripts\\verify_pty_stub.py    # temp workspace (may hit trust UI)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Windows console defaults to GBK; force UTF-8 so TUI output doesn't crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from winpty import PTY


# --- 1. Resolve appname + cmdline for PTY.spawn ---

def resolve_pty_command() -> tuple[str, str]:
    """Return (appname, cmdline) for PTY.spawn()."""

    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("shutil.which('claude') returned None")

    claude_path = Path(claude).resolve()
    suffix = claude_path.suffix.lower()

    if suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not comspec:
            raise RuntimeError("Cannot locate cmd.exe via COMSPEC or PATH")
        appname = str(Path(comspec).resolve())
        cmdline = f'"{appname}" /d /s /c ""{claude_path}""'
        return appname, cmdline

    if suffix == ".exe":
        appname = str(claude_path)
        cmdline = f'"{claude_path}"'
        return appname, cmdline

    raise RuntimeError(
        f"Unsupported claude launcher: {claude_path} (suffix={suffix!r})"
    )


def spawn_claude_pty(workspace: Path) -> PTY:
    appname, cmdline = resolve_pty_command()
    print(f"[*] PTY appname: {appname}")
    print(f"[*] PTY cmdline: {cmdline}")
    print(f"[*] PTY cwd:     {workspace}")

    pty = PTY(120, 40)
    pty.spawn(appname, cmdline, str(workspace))
    return pty


# --- 2. PTY drain (non-blocking) ---

def drain_pty(pty: PTY, duration: float, *, echo: bool = True) -> str:
    deadline = time.monotonic() + duration
    chunks: list[str] = []

    while time.monotonic() < deadline:
        try:
            data = pty.read(blocking=False)
        except Exception as exc:
            if not pty.isalive() or pty.iseof():
                break
            print(f"[!] PTY.read failed: {type(exc).__name__}: {exc}")
            time.sleep(0.05)
            continue

        if data:
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            chunks.append(data)
            if echo:
                print(data, end="", flush=True)
        else:
            time.sleep(0.05)

    return "".join(chunks)


# --- 3. Find JSONL by marker, not by mtime alone ---

def find_session_containing_marker(
    projects_root: Path, marker: str, *, started_at: float
) -> Path | None:
    marker_bytes = marker.encode("utf-8")
    for path in projects_root.rglob("*.jsonl"):
        try:
            stat = path.stat()
            if stat.st_mtime < started_at - 2:
                continue
            with path.open("rb") as fh:
                if marker_bytes in fh.read():
                    return path
        except (OSError, PermissionError):
            continue
    return None


# --- 4. Parse JSONL line-by-line, locate marker record ---

def inspect_marker_record(
    jsonl_path: Path, marker: str
) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
    all_entrypoints: list[str] = []
    marker_record: dict[str, Any] | None = None
    head_records: list[dict[str, Any]] = []

    with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_number, line in enumerate(fh, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            ep = record.get("entrypoint")
            if isinstance(ep, str):
                all_entrypoints.append(ep)

            if len(head_records) < 5:
                head_records.append(record)

            if marker in line and marker_record is None:
                marker_record = record
                print(f"[*] Marker found on JSONL line {line_number}")

    return marker_record, all_entrypoints, head_records


# --- 5. PTY shutdown (graceful then forced) ---

def stop_pty(pty: PTY) -> None:
    if not pty.isalive():
        return
    try:
        pty.write("\x03")
    except Exception:
        pass
    drain_pty(pty, 1.0, echo=False)
    if pty.isalive():
        try:
            pty.write("/exit\r")
        except Exception:
            pass
        drain_pty(pty, 2.0, echo=False)


def force_kill_pty_tree(pty: PTY) -> None:
    if not pty.isalive():
        return
    pid = pty.pid
    print(f"[!] Force-killing PTY process tree, PID={pid}")
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    try:
        pty.cancel_io()
    except Exception:
        pass


# --- 6. ANSI strip for readable tail output ---

_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


def readable_tail(buf: str, max_bytes: int = 8192) -> str:
    raw = buf.encode("utf-8", errors="replace")[-max_bytes:]
    raw = _ANSI_RE.sub(b"", raw)
    return raw.decode("utf-8", errors="replace")


# --- 7. Main flow ---

def main() -> int:
    # Workspace
    if len(sys.argv) > 1:
        workspace = Path(sys.argv[1]).resolve()
    else:
        workspace = Path(tempfile.gettempdir()) / "pty_stub_test"
        workspace.mkdir(parents=True, exist_ok=True)
        print(f"[!] No workspace arg given; using temp dir {workspace}")
        print("[!] If Claude shows a trust / login screen, re-run with a real workspace:")

    if not workspace.is_dir():
        print(f"[FAIL-SPAWN] workspace is not a directory: {workspace}")
        return 10

    claude_which = shutil.which("claude")
    print(f"[*] shutil.which('claude') = {claude_which!r}")

    projects_root = Path.home() / ".claude" / "projects"

    marker = f"MYCLAW_PTY_STUB_VERIFY_{uuid.uuid4().hex}"
    prompt = f"{marker}: reply only OK"
    print(f"[*] marker = {marker}")

    started_at = time.time()
    pty: PTY | None = None
    pty_output_buffer: list[str] = []

    try:
        try:
            pty = spawn_claude_pty(workspace)
        except Exception as exc:
            print(f"[FAIL-SPAWN] {type(exc).__name__}: {exc}")
            return 10
        print(f"[+] PTY spawned, pid={pty.pid}, alive={pty.isalive()}")

        # Drain startup (don't depend on specific text). Claude TUI can take a
        # while to be ready for input (MCP init, splash render, etc).
        print("[*] Draining startup output for 10s...")
        startup = drain_pty(pty, 10.0, echo=False)
        pty_output_buffer.append(startup)

        # Send whole prompt in one write; char-by-char produced per-line artifacts.
        print(f"[*] Sending prompt via PTY: {prompt!r}")
        try:
            pty.write(prompt)
            time.sleep(0.5)
            pty.write("\r")
        except Exception as exc:
            print(f"[!] write failed: {type(exc).__name__}: {exc}")

        # Poll for JSONL while continuing to drain PTY
        session_path: Path | None = None
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            chunk = drain_pty(pty, 0.2, echo=False)
            if chunk:
                pty_output_buffer.append(chunk)

            session_path = find_session_containing_marker(
                projects_root, marker, started_at=started_at
            )
            if session_path:
                break

            if not pty.isalive():
                print("[!] Claude exited before marker appeared in JSONL")
                break

            time.sleep(0.1)

        if not session_path:
            print("[FAIL-NO-JSONL] No JSONL containing the unique marker appeared.")
            print(f"  claude_path = {claude_which!r}")
            print(f"  appname/cmdline printed above")
            print(f"  cwd = {workspace}")
            if pty is not None:
                print(f"  pty.pid = {pty.pid}")
                print(f"  pty.isalive() = {pty.isalive()}")
                print(f"  pty.iseof() = {pty.iseof()}")
            print("  --- last 8 KB PTY output (ANSI stripped) ---")
            print(readable_tail("".join(pty_output_buffer)))
            return 20

        print(f"[+] Marker found in: {session_path}")
        marker_record, all_entrypoints, head_records = inspect_marker_record(
            session_path, marker
        )

        if marker_record is None:
            print("[FAIL-NO-MARKER] JSONL appeared but marker line not parsed.")
            return 30

        session_id = session_path.stem
        print(f"[+] session_id = {session_id}")
        print(f"[+] marker record type        = {marker_record.get('type')!r}")
        print(f"[+] marker record parentUuid  = {marker_record.get('parentUuid')!r}")
        print(f"[+] marker record entrypoint  = {marker_record.get('entrypoint')!r}")
        print(f"[+] all distinct entrypoints  = {sorted(set(all_entrypoints))}")
        print("[*] head records (type/entrypoint):")
        for i, rec in enumerate(head_records, 1):
            print(f"    [{i}] type={rec.get('type')!r} entrypoint={rec.get('entrypoint')!r}")

        ep = marker_record.get("entrypoint")
        if ep == "cli":
            print()
            print("[PASS] PTY-created session is CLI-born")
            print(f"session_id={session_id}")
            print("entrypoint=cli")
            print(f"jsonl={session_path}")
            return 0
        else:
            print()
            print(f"[FAIL-ENTRYPOINT] marker record entrypoint = {ep!r} (expected 'cli')")
            return 40

    finally:
        if pty is not None:
            stop_pty(pty)
            force_kill_pty_tree(pty)


if __name__ == "__main__":
    sys.exit(main())
