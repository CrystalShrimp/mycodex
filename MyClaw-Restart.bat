@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到 .venv\Scripts\python.exe
    echo 请先运行 MyClaw-Setup.bat 安装环境。
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] .env 不存在，请先完成 MyClaw-Setup.bat。
    pause
    exit /b 1
)

echo [1/3] 停止现有 MyClaw 进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_myclaw.ps1" -CallerPid 0
ping -n 3 127.0.0.1 >nul

echo [2/3] 重新启动 MyClaw...
call "%~dp0MyClaw.bat"

echo [3/3] 等待服务上线（最多 60 秒）...
set /a TRIES=0

:WAIT_LOOP
set /a TRIES+=1
curl.exe -s -m 2 http://127.0.0.1:8080/health 2>nul | find "ok" >nul
if not errorlevel 1 goto ONLINE
if %TRIES% geq 60 goto OFFLINE
ping -n 2 127.0.0.1 >nul
goto WAIT_LOOP

:ONLINE
echo [OK] MyClaw 服务已上线！
goto END

:OFFLINE
echo [X] 60 秒内服务未上线，请查看 logs\myclaw.log 排查。

:END
echo.
pause
