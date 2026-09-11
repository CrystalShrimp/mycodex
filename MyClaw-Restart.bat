@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] δ�ҵ� .venv\Scripts\python.exe
    echo �������� MyClaw-Setup.bat ��װ������
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] .env �����ڣ�������� MyClaw-Setup.bat��
    pause
    exit /b 1
)

echo [1/3] ֹͣ���� MyClaw ����...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_myclaw.ps1" -CallerPid 0
ping -n 3 127.0.0.1 >nul

echo [2/3] �������� MyClaw...
call "%~dp0MyClaw.bat"

echo [3/3] �ȴ��������ߣ���� 60 �룩...
set /a TRIES=0

:WAIT_LOOP
set /a TRIES+=1
curl.exe -s -m 2 http://127.0.0.1:8090/health 2>nul | find "ok" >nul
if not errorlevel 1 goto ONLINE
if %TRIES% geq 60 goto OFFLINE
ping -n 2 127.0.0.1 >nul
goto WAIT_LOOP

:ONLINE
echo [OK] MyClaw ���������ߣ�
goto END

:OFFLINE
echo [X] 60 ���ڷ���δ���ߣ���鿴 logs\myclaw.log �Ų顣

:END
echo.
pause
