#!/usr/bin/env python3
"""macOS 菜单栏常驻（可选，等价 Windows 的 tray.pyw）。

依赖 rumps（仅 macOS 可装）：
    .venv/bin/pip install rumps

行为：菜单栏图标实时反映后端健康状态；点击菜单可查看状态、打开日志、
退出（退出时停止后端子进程）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_port(default: int = 8090) -> int:
    try:
        for line in (ROOT / ".env").read_text("utf-8").splitlines():
            line = line.strip()
            if line.startswith("PORT="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return default


SERVICE_PORT = _env_port()
HEALTH_URL = f"http://127.0.0.1:{SERVICE_PORT}/health"
LOG_PATH = ROOT / "logs" / "mycodex.log"

server: subprocess.Popen | None = None


def healthy() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_server() -> None:
    global server
    if healthy():
        return
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        print(f"[ERROR] 未找到 {python}，请先运行 MyCodex-Setup.command")
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("ab") as log:
        server = subprocess.Popen(
            [str(python), "-m", "app.main"],
            cwd=ROOT,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    # 最多等 30s 健康（冷启动含依赖加载）
    for _ in range(60):
        if healthy():
            return
        if server.poll() is not None:
            break
        time.sleep(0.5)


def main() -> None:
    try:
        import rumps
    except ImportError:
        print("缺少 rumps（macOS 菜单栏框架）。安装：.venv/bin/pip install rumps")
        print("或不使用菜单栏，直接双击 MyCodex.command 以后台方式运行。")
        sys.exit(1)

    start_server()

    class MyCodexMenubar(rumps.App):
        def __init__(self):
            super().__init__("mycodex", title="◐", quit_button=None)
            self.menu = [
                rumps.MenuItem("查看状态", callback=self.on_status),
                rumps.MenuItem("打开日志", callback=self.on_log),
                None,
                rumps.MenuItem("退出 mycodex", callback=self.on_quit),
            ]
            rumps.timer(self.on_tick, 5)

        def _status_detail(self) -> str:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(HEALTH_URL, timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                ws = "🟢 已连接" if data.get("ws_connected") else "🔴 未连接"
                return f"✅ MyCodex 后端运行正常 (端口 {SERVICE_PORT})\n飞书长连接: {ws}"
            except Exception as e:
                return f"❌ 后端未响应 ({SERVICE_PORT} 端口): {e}\n详情请查看 logs/mycodex.log"

        def on_tick(self, _sender):
            self.title = "●" if healthy() else "○"

        def on_status(self, _sender):
            rumps.alert(self._status_detail())

        def on_log(self, _sender):
            if LOG_PATH.exists():
                subprocess.Popen(["open", "-t", str(LOG_PATH)])

        def on_quit(self, _sender):
            if server is not None and server.poll() is None:
                try:
                    import os
                    import signal
                    os.killpg(os.getpgid(server.pid), signal.SIGTERM)
                except Exception:
                    server.terminate()
            rumps.quit_application()

    MyCodexMenubar().run()


if __name__ == "__main__":
    main()
