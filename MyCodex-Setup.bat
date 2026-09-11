@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ===================================================
echo           MyCodex 一键自检和自动安装向导
echo ===================================================
echo.

set NEED_RESTART_CMD=0

REM ================= 1. Node.js 20+ 版本检查 =================
set NODE_OK=0
where node >nul 2>&1
if errorlevel 1 goto CHECK_NODE_DONE

for /f "tokens=1 delims=v. " %%a in ('node -v') do set NODE_MAJOR=%%a
if not defined NODE_MAJOR goto CHECK_NODE_DONE
if %NODE_MAJOR% geq 20 set NODE_OK=1

:CHECK_NODE_DONE
if "%NODE_OK%"=="1" (
    echo [OK] Node.js 20+ 环境自检通过！
    goto CHECK_VENV
)

echo [!] 警告: 未检测到 Node.js 20+ 环境 (auto_feishu 需要 Node.js v20 或更高版本)。
set /p CHOICE_NODE="[?] 是否自动下载并默认安装 Node.js v20.18.0 LTS？ [Y/N]: "
if /i not "%CHOICE_NODE%"=="Y" if /i not "%CHOICE_NODE%"=="" (
    echo [-] 已跳过 Node.js 安装。
    goto CHECK_VENV
)

echo [!] 正在通过国内镜像下载 Node.js 20.18.0 官方安装包...
set "MSI_PATH=%TEMP%\node_v20.msi"
curl.exe -L -o "%MSI_PATH%" "https://npmmirror.com/mirrors/node/v20.18.0/node-v20.18.0-x64.msi"
if exist "%MSI_PATH%" (
    echo [!] 正在静默默认安装 Node.js...
    msiexec.exe /i "%MSI_PATH%" /quiet /norestart
    del /f /q "%MSI_PATH%" >nul 2>&1
    set NEED_RESTART_CMD=1
    set "PATH=%ProgramFiles%\nodejs;%PATH%"
    echo [OK] Node.js 20.18.0 已静默安装！
)

:CHECK_VENV
echo.
REM ================= 2. Python .venv 环境检查 =================
if exist ".venv\Scripts\python.exe" (
    echo [OK] Python 环境自检通过: .venv\Scripts\python.exe
    goto CHECK_FEISHU
)

echo [!] 警告: 未检测到 Python 虚拟环境 (.venv)。
set /p CHOICE_VENV="[?] 是否自动安装 uv 并创建 Python .venv 环境？ [Y/N]: "
if /i not "%CHOICE_VENV%"=="Y" if /i not "%CHOICE_VENV%"=="" (
    echo [-] 已跳过 Python .venv 安装。
    goto CHECK_FEISHU
)

where uv >nul 2>&1
if not errorlevel 1 goto UV_READY

echo [!] 正在自动使用 curl 下载并安装 uv 工具...
set "UV_INSTALLER=%TEMP%\uv_install.ps1"
curl.exe -L -o "%UV_INSTALLER%" "https://astral.sh/uv/install.ps1"
if exist "%UV_INSTALLER%" (
    powershell -NoProfile -ExecutionPolicy Unrestricted -File "%UV_INSTALLER%"
    del /f /q "%UV_INSTALLER%" >nul 2>&1
)
set "PATH=%USERPROFILE%\.cargo\bin;%USERPROFILE%\.local\bin;%PATH%"

:UV_READY
echo [!] 正在执行 uv sync 安装项目依赖...
REM 客户机器可能全局配置过 UV_PROJECT_ENVIRONMENT，导致 uv 缓存同步到别处。
REM 强制 .venv 重新生成（忽略 Resolved/Checked 差异但 .venv 缺失的场景），以适配本机项目。
set "UV_PROJECT_ENVIRONMENT=%~dp0.venv"
call uv sync
set "UV_PROJECT_ENVIRONMENT="
if not exist ".venv\Scripts\python.exe" (
    echo [X] .venv 创建失败。请手动安装 Python 和 uv 后重试。
    pause
    exit /b 1
)
echo [OK] Python 虚拟环境 (.venv) 创建成功！

:CHECK_FEISHU
echo.
REM ================= 3. auto_feishu npm 和 Playwright 检查 =================
if not exist "auto_feishu\package.json" goto CHECK_ENV
if exist "auto_feishu\node_modules" (
    echo [OK] auto_feishu node_modules 环境自检通过！
    goto CHECK_ENV
)

echo [!] 警告: 未检测到 auto_feishu 的 Node.js 依赖包 (node_modules)。
set /p CHOICE_NPM="[?] 是否自动安装 auto_feishu 依赖并下载 Playwright 浏览器？ [Y/N]: "
if /i not "%CHOICE_NPM%"=="Y" if /i not "%CHOICE_NPM%"=="" (
    echo [-] 已跳过 auto_feishu npm 依赖安装。
    goto CHECK_ENV
)

pushd auto_feishu
echo [!] 正在执行 npm install ...
call npm install
echo [!] 正在安装 Playwright Chromium 内核（稍后）...
call npx playwright install chromium
popd
echo [OK] auto_feishu 依赖和 Playwright 浏览器安装完成！

:CHECK_ENV
echo.
REM ================= 4. .env 配置文件检查 =================
if exist ".env" (
    echo [OK] 配置文件 .env 自检通过！
    goto CHECK_CODEX
)

echo [!] 警告: 未检测到配置文件 .env，正在创建最小模板...
(
    echo # ===== Feishu App Config =====
    echo FEISHU_APP_ID=
    echo FEISHU_APP_SECRET=
    echo FEISHU_VERIFICATION_TOKEN=
    echo FEISHU_ENCRYPT_KEY=
    echo.
    echo # ===== Agent Config =====
    echo # Codex 认证使用本机 ChatGPT 登录态（~/.codex/auth.json），无需 API Key。
    echo DEFAULT_WORKSPACE=%~dp0.
    echo APPROVAL_TIMEOUT=600
    echo.
    echo # ===== Access Control =====
    echo ALLOWED_USERS=
    echo.
    echo # ===== Server Config =====
    echo HOST=0.0.0.0
    echo PORT=8090
    echo INSTANCE_LOCK_PORT=48922
) > ".env"
echo [OK] 已生成 .env 模板，请稍后编辑填入飞书凭据 (FEISHU_APP_ID / FEISHU_APP_SECRET)。

REM ================= 5. Codex CLI 与登录态检查 =================
:CHECK_CODEX
echo.
where codex >nul 2>&1
if errorlevel 1 goto CODEX_ASK
call codex --version >nul 2>&1
if errorlevel 1 goto CODEX_ASK
echo [OK] Codex CLI 自检通过！
goto CODEX_LOGIN

:CODEX_ASK
echo [!] 警告: 未检测到 Codex CLI (mycodex 的任务执行引擎)。
set /p CHOICE_CODEX="[?] 是否自动安装 OpenAI Codex CLI (npm 全局安装，国内镜像)？ [Y/N]: "
if /i not "%CHOICE_CODEX%"=="Y" if /i not "%CHOICE_CODEX%"=="" goto CODEX_SKIP
echo [!] 正在通过 npmmirror 安装 @openai/codex ...
call npm install -g @openai/codex --registry=https://registry.npmmirror.com
if errorlevel 1 (
    echo [ERROR] Codex CLI 安装失败。请手动执行: npm install -g @openai/codex
    pause
    exit /b 1
)
where codex >nul 2>&1
if errorlevel 1 (
    echo [注意] 安装完成但当前窗口还找不到 codex 命令，重开 cmd 后可被脚本自检。
) else (
    echo [OK] Codex CLI 安装完成！
)

:CODEX_LOGIN
codex login status 2>&1 | find /i "Logged in" >nul
if not errorlevel 1 (
    echo [OK] Codex 登录态自检通过（ChatGPT 账号已登录）！
    goto FINISH
)
echo [!] 警告: Codex 未登录。MyCodex 依赖本机已登录的 ChatGPT 账号执行任务。
echo     请手动在终端执行: codex login
echo     完成浏览器授权后再启动 MyCodex。

:CODEX_SKIP

:FINISH
echo.
echo ===================================================
echo              一键自检安装完成！
if "%NEED_RESTART_CMD%"=="1" (
    echo [注意] 已安装全局系统级软件，请重开 cmd 窗口让环境变量完全生效。
)
echo.
echo 接下来请:
echo   1. 双击 auto_feishu\setup.cmd 一键配置飞书机器人 (自动写入 .env 凭据)
echo   2. 确认终端执行 codex login 已完成 ChatGPT 登录
echo   3. 双击 MyCodex.bat 启动服务
echo   4. 在 .env 的 ALLOWED_USERS 中加入使用者飞书 Open ID (留空=不限制)

if not exist "scripts\setup_autostart.bat" goto END_ALL
echo.
set /p CHOICE_AUTO="[?] 是否设置每次开机自动后台运行（托盘常驻，后续可 MyCodex 自管理）？ [Y/N]: "
if /i "%CHOICE_AUTO%"=="Y" call "scripts\setup_autostart.bat"

:END_ALL
echo ===================================================
pause
