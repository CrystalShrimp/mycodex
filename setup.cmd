@echo off
setlocal
cd /d "%~dp0"

echo ==============================================
echo            MyClaw 平台配置
echo ==============================================
echo.
echo  1. 配置飞书（Playwright 自动配置）
echo  2. 配置企业微信（手动向导 + 连通实测）
echo  3. 两者都配（先飞书后企微）
echo  0. 退出
echo.
set /p choice=请选择 [0/1/2/3]: 

if "%choice%"=="1" goto feishu
if "%choice%"=="2" goto wecom
if "%choice%"=="3" goto both
goto end

:feishu
call auto_feishu\setup.cmd
goto end

:wecom
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到 .venv，请先运行 MyClaw-Setup.bat 完成环境安装
    goto end
)
".venv\Scripts\python.exe" scripts\setup_wecom.py
goto end

:both
call auto_feishu\setup.cmd
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到 .venv，请先运行 MyClaw-Setup.bat 完成环境安装
    goto end
)
".venv\Scripts\python.exe" scripts\setup_wecom.py
goto end

:end
echo.
pause
