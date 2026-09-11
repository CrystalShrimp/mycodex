import os
import subprocess
import time
import urllib.request
import json
from pathlib import Path


def _service_port(default: int = 8090) -> int:
    """Read PORT from the project .env — must stay in sync with the backend."""
    root = Path(__file__).resolve().parent.parent
    try:
        for line in (root / ".env").read_text("utf-8").splitlines():
            line = line.strip()
            if line.startswith("PORT="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return default


SERVICE_PORT = _service_port()


def stop_existing_processes(current_pid: int) -> None:
    """清理本项目关联的所有 Python 进程（不分 .venv / Anaconda / 系统）与服务端口占用。

    进程清杀委托给 scripts/stop_myclaw.ps1（-File 方式调用），该脚本只按
    项目根路径匹配，不会误杀同机其他 MyClaw 系部署。
    """
    print("Stopping existing MyCodex processes and child workers...")
    ps1 = Path(__file__).resolve().parent / "stop_myclaw.ps1"
    try:
        res = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ps1), "-CallerPid", str(current_pid),
            ],
            capture_output=True, text=True,
        )
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if line:
                print("Killed MyCodex process:", line)
        if res.returncode != 0:
            print("stop_myclaw.ps1 warning:", (res.stderr or "").strip()[:200])
    except Exception as e:
        print("Error stopping processes:", e)

    # 兜底：确保本项目服务端口的占用者被释放
    try:
        netstat_cmd = f'netstat -ano | findstr :{SERVICE_PORT}'
        res = subprocess.run(netstat_cmd, shell=True, capture_output=True, text=True)
        if res.stdout.strip():
            lines = res.stdout.strip().splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in line:
                    port_pid = parts[-1]
                    if port_pid.isdigit() and int(port_pid) != current_pid:
                        subprocess.run(f"taskkill /F /PID {port_pid}", shell=True, capture_output=True)
                        print(f"Killed process {port_pid} holding port {SERVICE_PORT}")
    except Exception as port_err:
        print(f"Error clearing port {SERVICE_PORT}:", port_err)


def verify_port_released() -> None:
    """确认服务端口已完全被 Windows 内核释放"""
    for _ in range(5):
        res = subprocess.run(f'netstat -ano | findstr :{SERVICE_PORT}', shell=True, capture_output=True, text=True)
        if not res.stdout.strip():
            return
        time.sleep(0.5)

def main():
    current_pid = os.getpid()
    stop_existing_processes(current_pid)
    verify_port_released()

    time.sleep(1)

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    bat_path = os.path.join(root_dir, "MyClaw.bat")

    print(f"Launching desktop application via MyClaw.bat: {bat_path}")

    # 使用 PowerShell 在当前用户 Active Session 下原生启动 MyClaw.bat
    # list 形式传参，避免 shell 字符串在 bash/cmd/powershell 多层转义下被改写。
    bat_abs = str(Path(bat_path).resolve()).replace("'", "''")
    root_abs = str(Path(root_dir).resolve()).replace("'", "''")

    subprocess.run(
        [
            "powershell", "-ExecutionPolicy", "Bypass", "-NoProfile", "-Command",
            f"Start-Process -FilePath '{bat_abs}' -WorkingDirectory '{root_abs}'",
        ],
    )
    print("Successfully triggered desktop launcher.")

    print("Verifying service startup via /health...")
    success = False
    last_detail = ""
    for i in range(30):
        time.sleep(1)
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{SERVICE_PORT}/health", timeout=2)
            if req.status == 200:
                data = json.loads(req.read().decode('utf-8'))
                last_detail = f"ws_connected={data.get('ws_connected')}"
                print(f"Service health check OK: {last_detail}")
                success = True
                break
        except Exception as exc:
            print(f"Waiting for service startup... ({i+1}/30) {exc}")

    if success:
        print("SUCCESS: MyCodex backend is up. Tray icon may take a few more seconds; check system tray.")
    else:
        print("WARNING: /health did not return 200 within 30s. Check logs/myclaw.log and myclaw-tray-error.log.")

if __name__ == "__main__":
    main()
