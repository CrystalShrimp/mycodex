@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到 .venv\Scripts\python.exe
    echo 请先运行 MyCodex-Setup.bat 安装依赖。
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] .env 不存在，请先运行 MyCodex-Setup.bat。
    pause
    exit /b 1
)

echo [1/3] 停止并清理 MyCodex 进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\stop_mycodex.ps1" -CallerPid 0
ping -n 3 127.0.0.1 >nul

echo [2/3] 正在启动 MyCodex...
call "%~dp0MyCodex.bat"

echo [3/3] 等待服务上线（最长 60 秒）...
set /a TRIES=0

:WAIT_LOOP
set /a TRIES+=1
curl.exe -s -m 2 http://127.0.0.1:8090/health 2>nul | find "ok" >nul
if not errorlevel 1 goto ONLINE
if %TRIES% geq 60 goto OFFLINE
ping -n 2 127.0.0.1 >nul
goto WAIT_LOOP

:ONLINE
echo [OK] MyCodex 已重新上线！
goto END

:OFFLINE
echo [X] 60 秒内服务未上线，请查看 logs\mycodex.log 排查。

:END
echo.
pause
