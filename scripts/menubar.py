#!/usr/bin/env python3
"""macOS 菜单栏常驻（默认集成，等价 Windows 的 tray.pyw）。

依赖 rumps 随 Setup 的 uv sync 自动安装（仅 macOS，pyproject 带 darwin 标记）；
由 restart_mac.sh / start_mac.sh 在服务健康后自动拉起，未装组件时降级为纯后台服务。

行为：菜单栏图标实时反映后端健康状态；点击菜单可查看状态、打开日志、
退出（退出时连同后台服务一起停止）。
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
# 与 scripts/mac_common.sh 的 mac_start_service 同一日志，双击启动与菜单栏
# 启动的日志都汇聚到同一处
LOG_PATH = ROOT / "logs" / "macos-service.log"

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


def stop_service_by_port() -> None:
    """服务由外部（restart_mac.sh）启动时，按端口兜底停止，保持与 Windows 托盘退出一致。"""
    import subprocess

    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{SERVICE_PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        ).stdout
        for pid in out.split():
            if pid.isdigit():
                subprocess.run(["kill", pid], capture_output=True)
    except Exception:
        pass


def _find_icon() -> str | None:
    for name in ("icon.png", "icon.jpg", "icon.ico"):
        p = ROOT / name
        if p.exists():
            return str(p.resolve())
    return None


def main() -> None:
    try:
        import rumps
    except ImportError:
        print("缺少 rumps（macOS 菜单栏框架）。安装：重跑 MyCodex-Setup.command（uv sync 自动安装）。")
        print("或直接双击 MyCodex.command 以后台方式运行。")
        sys.exit(1)

    start_server()
    icon_path = _find_icon()

    class MyCodexMenubar(rumps.App):
        def __init__(self):
            init_title = "●" if icon_path else "MyCodex ●"
            super().__init__(
                "mycodex",
                title=init_title,
                icon=icon_path,
                template=False if icon_path else None,
                quit_button=None,
            )
            self.menu = [
                rumps.MenuItem("查看状态", callback=self.on_status),
                rumps.MenuItem("打开日志", callback=self.on_log),
                None,
                rumps.MenuItem("退出 mycodex", callback=self.on_quit),
            ]
            self._timer = rumps.Timer(self.on_tick, 5)
            self._timer.start()
            self.on_tick(None)

        def _status_detail(self) -> str:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(HEALTH_URL, timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                lines = [f"✅ MyCodex 后端运行正常 (端口 {SERVICE_PORT})"]
                channels = data.get("channels") or {}
                if channels.get("feishu") is not None:
                    lines.append(f"飞书长连接: {'🟢 已连接' if channels['feishu'].get('connected') else '🔴 未连接'}")
                elif data.get("ws_connected"):
                    lines.append("飞书长连接: 🟢 已连接")
                else:
                    lines.append("飞书长连接: 🔴 未连接")
                wecom = channels.get("wecom")
                if wecom is not None:
                    lines.append(f"企微长连接: {'🟢 已连接' if wecom.get('connected') else '🔴 未连接'}")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ 后端未响应 ({SERVICE_PORT} 端口): {e}\n详情请查看 logs/macos-service.log"

        def on_tick(self, _sender):
            ok = healthy()
            if icon_path:
                self.title = "●" if ok else "○"
            else:
                self.title = "MyCodex ●" if ok else "MyCodex ○"

        def on_status(self, _sender):
            rumps.alert(self._status_detail())

        def on_log(self, _sender):
            if LOG_PATH.exists():
                subprocess.Popen(["open", "-t", str(LOG_PATH)])
            else:
                rumps.alert(f"日志尚未生成：{LOG_PATH}")

        def on_quit(self, _sender):
            if server is not None and server.poll() is None:
                try:
                    import os
                    import signal
                    os.killpg(os.getpgid(server.pid), signal.SIGTERM)
                except Exception:
                    server.terminate()
            else:
                stop_service_by_port()
            rumps.quit_application()

    MyCodexMenubar().run()


if __name__ == "__main__":
    main()
