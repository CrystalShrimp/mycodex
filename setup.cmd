@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==============================================
echo            MyCodex 平台配置向导
echo ==============================================
echo.
echo  1. 配置飞书 - 个人用（仅创建者可用，不改变应用可用范围）
echo  2. 配置飞书 - 公用（可用范围全员，支持群成员一键导入白名单）
echo  3. 飞书群成员一键导入白名单（日常维护工具）
echo  4. 配置企业微信（手动填 + 连通实测）
echo  5. 完整配置（飞书公用 + 企业微信）
echo  0. 退出
echo.
set /p choice=请选择 [0/1/2/3/4/5]: 

if "%choice%"=="1" goto feishu_personal
if "%choice%"=="2" goto feishu_public
if "%choice%"=="3" goto feishu_import
if "%choice%"=="4" goto wecom
if "%choice%"=="5" goto both
goto end

:feishu_personal
call auto_feishu\setup.cmd personal
if errorlevel 1 goto end
echo.
echo [OK] 飞书个人用模式配置完成（可用范围保持仅限创建者）。
goto end

:feishu_public
call auto_feishu\setup.cmd public
if errorlevel 1 goto end

echo.
echo ==============================================
echo           飞书公用模式 - 白名单配置
echo ==============================================
echo 💡 默认模式：ALLOWED_USERS 保持为空，企业内全员均可直接访问。
echo.
set /p IMPORT_CHOICE="[?] 是否需要将特定群聊的所有用户ID一键导入为白名单？[Y/N] (默认 N): "
if /i "%IMPORT_CHOICE%"=="Y" (
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" scripts\import_feishu_group.py
    ) else (
        python scripts\import_feishu_group.py
    )
) else (
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" -c "from scripts.import_feishu_group import read_env, upsert_env; env=read_env(); wecom=[u for u in env.get('ALLOWED_USERS','').split(',') if u.strip().startswith('wecom:')]; upsert_env('ALLOWED_USERS', ','.join(wecom))"
    )
    echo [OK] 已将 ALLOWED_USERS 设为全员开放模式。
)
goto end

:feishu_import
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到 .venv，请先运行 MyCodex-Setup.bat 完成环境安装
    goto end
)
".venv\Scripts\python.exe" scripts\import_feishu_group.py
goto end

:wecom
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到 .venv，请先运行 MyCodex-Setup.bat 完成环境安装
    goto end
)
".venv\Scripts\python.exe" scripts\setup_wecom.py
goto end

:both
call auto_feishu\setup.cmd public
if errorlevel 1 goto end

echo.
echo ==============================================
echo           飞书公用模式 - 白名单配置
echo ==============================================
echo 💡 默认模式：ALLOWED_USERS 保持为空，企业内全员均可直接访问。
echo.
set /p IMPORT_CHOICE="[?] 是否需要将特定群聊的所有用户ID一键导入为白名单？[Y/N] (默认 N): "
if /i "%IMPORT_CHOICE%"=="Y" (
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" scripts\import_feishu_group.py
    ) else (
        python scripts\import_feishu_group.py
    )
) else (
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" -c "from scripts.import_feishu_group import read_env, upsert_env; env=read_env(); wecom=[u for u in env.get('ALLOWED_USERS','').split(',') if u.strip().startswith('wecom:')]; upsert_env('ALLOWED_USERS', ','.join(wecom))"
    )
    echo [OK] 已将 ALLOWED_USERS 设为全员开放模式。
)

echo.
echo 接下来进行企业微信配置...
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到 .venv，请先运行 MyCodex-Setup.bat 完成环境安装
    goto end
)
".venv\Scripts\python.exe" scripts\setup_wecom.py
goto end

:end
echo.
pause
