@echo off
setlocal
cd /d "%~dp0"

if not exist .env (
    echo [ERROR] .env 文件不存在！
    echo 请先运行 MyCodex-Setup.bat 生成模板，再填入飞书凭据。
    pause
    exit /b 2
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] 未找到 .venv\Scripts\pythonw.exe
    echo 请先运行 MyCodex-Setup.bat（自动执行 uv sync）安装 Python 依赖。
    pause
    exit /b 3
)

echo [INFO] 停止可能存在的旧进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_mycodex.ps1" -CallerPid 0
ping -n 3 127.0.0.1 >nul

echo [INFO] 正在启动 MyCodex 后台服务...
REM 后台进程直接继承环境变量路径，由托盘脚本的窗口进程路径代理（避免双击）。
set "NO_PROXY=open.feishu.cn,.feishu.cn,msg-frontier.feishu.cn,.larksuite.com,.larkoffice.com,localhost,127.0.0.1"

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\tray.pyw"

echo [OK] 启动指令已发出（托盘图标稍后出现，首次启动约 30 秒）。
ping -n 4 127.0.0.1 >nul
