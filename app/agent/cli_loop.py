"""Codex CLI subprocess manager — per-message exec with native threads.

Each Feishu message spawns one ``codex exec --json`` process in the user's
workspace. Multi-turn continuity relies on codex's own persisted sessions
(~/.codex/sessions): when the session object carries a thread_id we respawn
via ``codex exec resume <thread_id>``. Auth is inherited from the machine's
ChatGPT login (~/.codex/auth.json) — no API keys involved.

Approval modes map onto codex sandbox policy (via -c overrides so the
same code path works for fresh runs and `exec resume`):
    h → sandbox_mode=read-only                          (writes impossible)
    m → sandbox_mode=workspace-write + approval never   (workspace-scoped writes)
    l → --dangerously-bypass-approvals-and-sandbox      (full access)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from config.settings import settings
from app.feishu.cards import (
    build_progress_card,
    build_error_card,
)
from app.feishu.client import feishu_client
from app.models.schemas import AgentResult, ToolCallRecord
from app.audit.logger import audit_logger
from app.agent.codex_sessions import latest_thread_for_workspace

logger = logging.getLogger("myclaw.cli_loop")

# Map codex_thread_id -> {open_id, approval_mode}
session_registry: dict[str, dict] = {}


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and ALL its descendants.

    Codex spawns deep trees (codex → pwsh → python → ...). On Windows,
    TerminateProcess only kills the direct child, leaving orphans that hold
    GUI dialogs open forever. Use taskkill /T (tree) on Windows or killpg on
    Unix to tear down the whole subtree.
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _summarize_item_args(item: dict) -> str:
    """One-line summary of a codex item, for the progress card."""
    itype = item.get("type", "")
    if itype == "command_execution":
        cmd = (item.get("command") or "").strip()
        if not cmd:
            return ""
        first = cmd.split("&&")[0].split("|")[0].split(";")[0].strip()
        first = first.strip('"').strip("'")
        return f"`{first[:80]}`" if len(first) <= 80 else f"`{first[:77]}…`"
    if itype == "file_change":
        paths = [c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)]
        leafs = [p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] for p in paths if p]
        if not leafs:
            return ""
        joined = ", ".join(leafs[:3]) + (" …" if len(leafs) > 3 else "")
        return f"`{joined[:80]}`"
    return ""


_ITEM_TOOL_NAMES = {
    "command_execution": "Bash",
    "file_change": "Edit",
    "mcp_tool_call": "MCP",
    "web_search": "WebSearch",
}


class CodexCLILoop:
    """Manage one ``codex exec`` process per in-flight message per user.

    Per-user asyncio.Lock serializes whole message executions: a follow-up
    message sent while a task runs simply waits, mirroring the old
    long-lived-stdin queue. Reader task resolves the message future on
    ``turn.completed`` / ``turn.failed`` / process exit.
    """

    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # open_id -> 群聊 chat_id（空 = 私聊）。崩溃告警等后续发送用它路由。
        self._chat_targets: dict[str, str] = {}
        self._exec_locks: dict[str, asyncio.Lock] = {}
        self._reader_tasks: dict[str, asyncio.Task] = {}
        self._last_error: str = ""
        self._last_results: dict[str, AgentResult] = {}
        self._task_history: dict[str, dict[str, AgentResult]] = {}

    # ---- public API ----

    def get_last_result(self, open_id: str) -> AgentResult | None:
        return self._last_results.get(open_id)

    def get_task_result(self, open_id: str, task_id: str) -> AgentResult | None:
        user_history = self._task_history.get(open_id, {})
        if task_id in user_history:
            return user_history[task_id]
        for history in self._task_history.values():
            if task_id in history:
                return history[task_id]
        return None

    def is_running(self, open_id: str) -> bool:
        """True while a codex exec process is alive for this user."""
        proc = self._processes.get(open_id)
        return proc is not None and proc.returncode is None

    def get_thread_id_for_user(self, open_id: str) -> str | None:
        for thread_id, info in session_registry.items():
            if info.get("open_id") == open_id:
                return thread_id
        return None

    async def send_and_wait(
        self,
        prompt: str,
        open_id: str,
        workspace: str,
        model: str | None = None,
        approval_mode: str = "m",
        effort: str = "",
        codex_thread_id: str | None = None,
        resume_thread_id: str | None = None,
        chat_id: str = "",
    ) -> AgentResult:
        """Spawn codex exec for one message and wait for the turn to finish.

        Thread handling:
        - *resume_thread_id* → ``codex exec resume <id>``  (explicit /resume)
        - *codex_thread_id*  → ``codex exec resume <id>``  (persisted thread)
        - "__continue__"     → resolve newest thread in workspace, else fresh
        - neither            → fresh thread
        """
        lock = self._exec_locks.setdefault(open_id, asyncio.Lock())
        async with lock:
            # A previous process may still be registered after a crash exit;
            # make sure stale state is gone before spawning.
            old = self._processes.pop(open_id, None)
            if old is not None and old.returncode is None:
                _kill_process_tree(old)

            workspace_path = Path(workspace)
            if not workspace_path.is_dir():
                return self._error_result(prompt, f"工作区不存在: {workspace}")
            workspace = str(workspace_path.resolve())

            chosen = model or settings.codex_default_model

            # Resolve the "__continue__" sentinel to a concrete thread id.
            effective_thread = resume_thread_id or ""
            if not effective_thread and codex_thread_id == "__continue__":
                found = latest_thread_for_workspace(workspace)
                if found:
                    effective_thread = found[0]
                    logger.info("Resolved __continue__ to thread %s", effective_thread)
            elif not effective_thread and codex_thread_id:
                effective_thread = codex_thread_id

            try:
                proc, stdin_writer = await self._spawn(
                    open_id, workspace, chosen, approval_mode, effort,
                    effective_thread, prompt,
                )
            except FileNotFoundError as exc:
                return self._error_result(prompt, str(exc))
            except NotADirectoryError as exc:
                return self._error_result(prompt, str(exc))

            self._chat_targets[open_id] = chat_id

            msg_state = {
                "card_id": "",
                "model": chosen,
                "step": 0,
                "tool_counts": {},
                "warnings": 0,
                "last_warning": "",
                "started_at": asyncio.get_event_loop().time(),
                "last_patch_at": 0.0,
                "current_tool": "",
                "current_tool_args": "",
                "current_text": "",
                "last_text": "",
                "tools": [],
            }

            try:
                card = build_progress_card(
                    self._model_label(chosen, effort), "running",
                )
                if chat_id:
                    card_msg = await feishu_client.send_card(chat_id, card, is_chat=True)
                else:
                    card_msg = await feishu_client.send_card(open_id, card)
                msg_state["card_id"] = card_msg.get("data", {}).get("message_id", "")
            except Exception as e:
                logger.warning("Failed to create progress card: %s", e)

            future: asyncio.Future[AgentResult] = asyncio.get_event_loop().create_future()

            # Write prompt to stdin then close it — codex reads instructions
            # from stdin when invoked with the `-` positional.
            try:
                stdin_writer.write(prompt.encode("utf-8"))
                await stdin_writer.drain()
                stdin_writer.close()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning("Stdin writer broken for open_id %s: %s", open_id, exc)
                _kill_process_tree(proc)
                return self._error_result(prompt, f"进程通信中断 ({type(exc).__name__}): {exc}")

            task = asyncio.create_task(
                self._read_loop(open_id, proc, approval_mode, future, msg_state),
            )
            self._reader_tasks[open_id] = task

        return await future

    def cancel_by_user(self, open_id: str) -> bool:
        """Kill the running process for a user (synchronous)."""
        proc = self._processes.pop(open_id, None)
        task = self._reader_tasks.pop(open_id, None)
        if task and not task.done():
            task.cancel()
        if proc and proc.returncode is None:
            _kill_process_tree(proc)
            return True
        return False

    async def cancel_and_wait(self, open_id: str) -> bool:
        cancelled = self.cancel_by_user(open_id)
        await asyncio.sleep(0.3)
        return cancelled

    # ---- internal ----

    @staticmethod
    def _model_label(model: str, effort: str) -> str:
        return f"{model} · {effort}" if effort else (model or "codex")

    async def _spawn(
        self, open_id: str, workspace: str, model: str,
        approval_mode: str, effort: str, thread_id: str, prompt: str,
    ) -> tuple[asyncio.subprocess.Process, asyncio.StreamWriter]:
        cli_path = settings.codex_cli_path
        # shutil.which resolves bare names ("codex") to the npm .cmd shim on
        # Windows that CreateProcess alone won't auto-append.
        resolved = shutil.which(cli_path)
        if not resolved:
            raise FileNotFoundError(
                f"Codex CLI not found: {cli_path!r}. "
                f"Install it (`npm i -g @openai/codex`) or set "
                f"CODEX_CLI_PATH to an absolute path."
            )

        # Build args. `codex exec resume` is a subcommand with its OWN flag
        # set: it accepts --json/--skip-git-repo-check/-m/-c but NOT -C/-s/
        # --approve-for-me. Sandbox therefore goes through -c overrides,
        # which work identically on both fresh and resume paths.
        args = [resolved, "exec", "--json", "--skip-git-repo-check"]
        if thread_id:
            args.extend(["resume", thread_id])
        else:
            args.extend(["-C", workspace])
        # Approval mode → sandbox policy (see module docstring).
        if approval_mode == "l":
            args.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval_mode == "h":
            args.extend(["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"'])
        else:
            args.extend(["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"'])
        if model:
            args.extend(["-m", model])
        if effort:
            # -c value is parsed as TOML — quotes make it a string.
            args.extend(["-c", f'model_reasoning_effort="{effort}"'])
        # Prompt comes from stdin (avoids cmdline length/quoting issues).
        args.append("-")

        env = os.environ.copy()
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(key, None)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=env,
            limit=10 * 1024 * 1024,
        )
        self._processes[open_id] = proc
        logger.warning(
            "Codex exec started: open_id=%s workspace=%s model=%s effort=%s mode=%s thread=%s pid=%s",
            open_id, workspace, model, effort, approval_mode, thread_id or "(new)", proc.pid,
        )
        return proc, proc.stdin  # type: ignore[return-value]

    async def _patch_progress(self, msg_state: dict, status: str) -> None:
        """Rebuild the progress card from msg_state and PATCH it.

        Best-effort: any Feishu API error is swallowed since a stale card is
        preferable to killing the task. Throttled to 0.5s except transitions.
        """
        card_id = msg_state.get("card_id")
        if not card_id:
            return
        now = asyncio.get_event_loop().time()
        prev_status = msg_state.get("last_status")
        if status == prev_status and now - msg_state.get("last_patch_at", 0) < 0.5:
            return
        try:
            elapsed = now - msg_state.get("started_at", now)
            last_text = msg_state.get("current_text") or msg_state.get("last_text", "")
            card = build_progress_card(
                msg_state.get("model", ""),
                status,
                step=msg_state.get("step", 0),
                tool_counts=msg_state.get("tool_counts", {}),
                elapsed_s=elapsed,
                warnings=msg_state.get("warnings", 0),
                current_tool=msg_state.get("current_tool", ""),
                current_tool_args=msg_state.get("current_tool_args", ""),
                last_text=last_text if status == "running" else "",
                last_warning=msg_state.get("last_warning", "") if status != "running" else "",
            )
            await feishu_client.update_card(card_id, card)
            msg_state["last_patch_at"] = now
            msg_state["last_status"] = status
        except Exception as e:
            logger.debug("Progress card PATCH failed (non-fatal): %s", e)

    async def _read_loop(
        self, open_id: str, proc: asyncio.subprocess.Process,
        approval_mode: str, future: asyncio.Future, msg_state: dict,
    ) -> None:
        """Read JSONL events from stdout, patch progress, resolve the future."""
        stderr_lines: list[str] = []
        task_id = uuid.uuid4().hex[:12]
        resolved = False
        thread_id = ""

        async def _drain_stderr() -> None:
            pipe = proc.stderr
            if pipe is None:
                return
            while True:
                line = await pipe.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                stderr_lines.append(decoded)
                # codex logs non-fatal ERRORs (e.g. models refresh timeout) —
                # keep them for diagnostics only.
                logger.debug("codex stderr: %s", decoded)

        stderr_task = asyncio.create_task(_drain_stderr())

        def _resolve(result: AgentResult) -> None:
            nonlocal resolved
            if not future.done():
                future.set_result(result)
            resolved = True

        try:
            while True:
                pipe = proc.stdout
                if pipe is None:
                    break
                raw_line = await pipe.readline()
                if not raw_line:
                    break  # process exited
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line: %s", line[:200])
                    continue

                etype = event.get("type", "")

                if etype == "thread.started":
                    thread_id = event.get("thread_id", "")
                    if thread_id:
                        session_registry[thread_id] = {
                            "open_id": open_id,
                            "approval_mode": approval_mode,
                        }
                    await self._patch_progress(msg_state, "running")

                elif etype == "turn.started":
                    await self._patch_progress(msg_state, "running")

                elif etype == "item.started":
                    item = event.get("item", {})
                    name = _ITEM_TOOL_NAMES.get(item.get("type", ""))
                    if name:
                        msg_state["current_tool"] = name
                        msg_state["current_tool_args"] = _summarize_item_args(item) or "…"
                        await self._patch_progress(msg_state, "running")

                elif etype == "item.completed":
                    await self._handle_item(event.get("item", {}), msg_state)

                elif etype == "turn.completed":
                    usage = event.get("usage", {})
                    result = AgentResult(
                        task_id=task_id,
                        prompt="",
                        model=msg_state.get("model", "codex"),
                        status="completed",
                        text=msg_state.get("current_text", "") or msg_state.get("last_text", ""),
                        tools_used=msg_state.get("tools", []),
                        error="",
                        thread_id=thread_id,
                        duration_s=asyncio.get_event_loop().time() - msg_state.get("started_at", 0),
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                    )
                    self._record_result(open_id, result, msg_state, "completed")
                    _resolve(result)

                elif etype == "turn.failed":
                    error = event.get("error", {})
                    message = error.get("message", "") if isinstance(error, dict) else str(error)
                    result = AgentResult(
                        task_id=task_id,
                        prompt="",
                        model=msg_state.get("model", "codex"),
                        status="failed",
                        text=msg_state.get("last_text", ""),
                        tools_used=msg_state.get("tools", []),
                        error=message or "turn failed",
                        thread_id=thread_id,
                        duration_s=asyncio.get_event_loop().time() - msg_state.get("started_at", 0),
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                    )
                    self._record_result(open_id, result, msg_state, "failed")
                    _resolve(result)

                elif etype == "error":
                    msg_state["warnings"] += 1
                    msg_state["last_warning"] = str(event.get("message", ""))[:120]
                    await self._patch_progress(msg_state, "running")

                else:
                    logger.debug("Unhandled codex event: %s", etype)

            # ---- stdout EOF: process exited ----
            await proc.wait()
            exit_code = proc.returncode or 0
            self._last_error = ""
            if stderr_lines:
                self._last_error = f"exit={exit_code}\n" + "\n".join(stderr_lines[-10:])

            # Crash = process died before the turn resolved (turn.completed /
            # turn.failed never arrived). A clean exit after a resolved turn —
            # even non-zero, even with noisy stderr — is NOT a crash: codex
            # logs non-fatal ERRORs (e.g. models refresh timeout) routinely.
            if not resolved:
                reason = self._last_error or f"Codex CLI 提前退出 (code={exit_code})"
                result = self._error_result("", reason)
                result.task_id = task_id
                result.thread_id = thread_id
                self._record_result(open_id, result, msg_state, "failed")
                _resolve(result)
                if msg_state.get("card_id"):
                    try:
                        err_card = build_error_card("Codex CLI 已退出", reason[:3500])
                        await feishu_client.update_card(msg_state["card_id"], err_card)
                    except Exception:
                        pass

                crash_chat_id = self._chat_targets.get(open_id, "")
                hint = (
                    "💡 自愈提示：您可以尝试发送 `/new` 重置会话，或发送 `/cd` 切换到其他可用工作区。"
                )
                text = (
                    f"🚨 **Codex 运行进程异常退出** 🚨\n"
                    f"退出状态码: `{exit_code}`\n"
                    f"错误详情:\n```\n{reason[:1000]}\n```\n{hint}"
                )
                try:
                    if crash_chat_id:
                        asyncio.create_task(
                            feishu_client.send_text(crash_chat_id, text, is_chat=True),
                        )
                    else:
                        asyncio.create_task(feishu_client.send_text(open_id, text))
                except Exception:
                    pass

            logger.info(
                "Codex exec exited: open_id=%s code=%s thread=%s",
                open_id, exit_code, thread_id or "(none)",
            )

        except asyncio.CancelledError:
            _kill_process_tree(proc)
            _resolve(self._error_result("", "process terminated", "cancelled"))
            await proc.wait()
        except Exception as exc:
            logger.exception("Codex read loop error: open_id=%s", open_id)
            _resolve(self._error_result("", str(exc)))
        finally:
            stderr_task.cancel()
            self._cleanup(open_id)

    async def _handle_item(self, item: dict, msg_state: dict) -> None:
        itype = item.get("type", "")

        if itype == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                if msg_state.get("current_text"):
                    msg_state["last_text"] = msg_state["current_text"]
                msg_state["current_text"] = text
            return

        if itype == "error":
            msg_state["warnings"] += 1
            msg_state["last_warning"] = (item.get("message") or "item error")[:120]
            await self._patch_progress(msg_state, "running")
            return

        tool_name = _ITEM_TOOL_NAMES.get(itype)
        if not tool_name:
            # todo_list / reasoning / unknown: count as a generic step.
            if itype in ("todo_list", "reasoning", "mcp_tool_call"):
                msg_state["step"] += 1
            return

        msg_state["step"] += 1
        counts = msg_state["tool_counts"]
        counts[tool_name] = counts.get(tool_name, 0) + 1
        msg_state["current_tool"] = f"{tool_name} #{counts[tool_name]}"
        msg_state["current_tool_args"] = _summarize_item_args(item)

        arguments: dict = {}
        if itype == "command_execution":
            arguments = {"command": item.get("command", "")}
        elif itype == "file_change":
            arguments = {"changes": item.get("changes", [])}
        msg_state.setdefault("tools", []).append(
            ToolCallRecord(tool_name=tool_name, arguments=arguments),
        )
        await self._patch_progress(msg_state, "running")

    def _record_result(
        self, open_id: str, result: AgentResult, msg_state: dict, status: str,
    ) -> None:
        self._last_results[open_id] = result
        user_history = self._task_history.setdefault(open_id, {})
        user_history[result.task_id] = result
        if len(user_history) > 50:
            first_key = next(iter(user_history))
            user_history.pop(first_key, None)

        card_id = msg_state.get("card_id")
        if card_id:
            try:
                elapsed = asyncio.get_event_loop().time() - msg_state.get("started_at", 0)
                final_card = build_progress_card(
                    msg_state.get("model", result.model),
                    status,
                    step=msg_state.get("step", 0),
                    tool_counts=msg_state.get("tool_counts", {}),
                    elapsed_s=elapsed,
                    warnings=msg_state.get("warnings", 0),
                    result_text=result.text,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    error=result.error,
                    thread_id=result.thread_id,
                    task_id=result.task_id,
                )
                asyncio.create_task(feishu_client.update_card(card_id, final_card))
            except Exception:
                pass

        audit_logger.log_agent_result(
            task_id=result.task_id,
            model="codex",
            status=status,
            tools_count=len(result.tools_used),
            text_len=len(result.text),
        )
        logger.info(
            "Codex done: task=%s status=%s time=%.1fs tools=%d",
            result.task_id, status, result.duration_s, len(result.tools_used),
        )

    def _cleanup(self, open_id: str) -> None:
        self._processes.pop(open_id, None)
        self._reader_tasks.pop(open_id, None)

    @staticmethod
    def _error_result(prompt: str, error: str, status: str = "failed") -> AgentResult:
        return AgentResult(
            task_id=uuid.uuid4().hex[:12],
            prompt=prompt,
            model="codex",
            status=status,
            error=error,
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )


# Singleton
codex_cli_loop = CodexCLILoop()
