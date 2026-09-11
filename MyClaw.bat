@echo off
setlocal
cd /d "%~dp0"

if not exist .env (
    echo [ERROR] .env 文件不存在！
    echo 请先复制 examples\.env.example 为 .env 并填入飞书凭据。
    pause
    exit /b 2
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] 未找到 .venv\Scripts\pythonw.exe
    echo 请先运行 MyClaw-Setup.bat（或执行 uv sync）安装 Python 环境。
    pause
    exit /b 3
)

echo [INFO] 清理可能存在的残留进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_myclaw.ps1" -CallerPid 0
ping -n 3 127.0.0.1 >nul

echo [INFO] 启动 MyClaw 托盘与服务...
REM 飞书域名直连：服务进程不走路由代理（配合 tray 内的代理剥离双保险）
set "NO_PROXY=open.feishu.cn,.feishu.cn,msg-frontier.feishu.cn,.larksuite.com,.larkoffice.com,localhost,127.0.0.1"

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\tray.pyw"

echo [OK] 已启动。托盘图标稍后出现，首次启动约需 30 秒。
ping -n 4 127.0.0.1 >nul
