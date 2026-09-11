@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo           MyClaw 环境依赖自检与自动安装工具
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
    echo [OK] Node.js 20+ 环境检查通过！
    goto CHECK_VENV
)

echo [!] 警告: 未检测到 Node.js 20+ 环境 (auto_feishu 需要 Node.js v20 或更高版本)。
set /p CHOICE_NODE="[?] 是否自动下载并静默安装 Node.js v20.18.0 LTS？ [Y/N]: "
if /i not "%CHOICE_NODE%"=="Y" if /i not "%CHOICE_NODE%"=="" (
    echo [-] 已跳过 Node.js 安装。
    goto CHECK_VENV
)

echo [!] 正在通过国内镜像下载 Node.js 20.18.0 官方安装包...
set "MSI_PATH=%TEMP%\node_v20.msi"
curl.exe -L -o "%MSI_PATH%" "https://npmmirror.com/mirrors/node/v20.18.0/node-v20.18.0-x64.msi"
if exist "%MSI_PATH%" (
    echo [!] 正在静默安装 Node.js...
    msiexec.exe /i "%MSI_PATH%" /quiet /norestart
    del /f /q "%MSI_PATH%" >nul 2>&1
    set NEED_RESTART_CMD=1
    set "PATH=%ProgramFiles%\nodejs;%PATH%"
    echo [OK] Node.js 20.18.0 已静默安装！
)

:CHECK_VENV
echo.
REM ================= 2. Python .venv 独立环境 =================
if exist ".venv\Scripts\python.exe" (
    echo [OK] Python 独立环境检查通过: .venv\Scripts\python.exe
    goto CHECK_FEISHU
)

echo [!] 警告: 未检测到 Python 独立环境 (.venv)。
set /p CHOICE_VENV="[?] 是否自动安装 uv 并创建 Python .venv 环境？ [Y/N]: "
if /i not "%CHOICE_VENV%"=="Y" if /i not "%CHOICE_VENV%"=="" (
    echo [-] 已跳过 Python .venv 创建。
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
echo [!] 正在调用 uv sync 创建项目环境...
REM 客户机可能全局设置过 UV_PROJECT_ENVIRONMENT，导致 uv 把环境同步到别处、
REM 本地 .venv 不生成（现象：Resolved/Checked 极快但 .venv 缺失）。钉死到本项目。
set "UV_PROJECT_ENVIRONMENT=%~dp0.venv"
call uv sync
set "UV_PROJECT_ENVIRONMENT="
if not exist ".venv\Scripts\python.exe" (
    echo [X] .venv 创建失败，请检查 Python 和 uv 的网络连接。
    pause
    exit /b 1
)
echo [OK] Python 独立环境 (.venv) 创建成功！

:CHECK_FEISHU
echo.
REM ================= 3. auto_feishu npm 和 Playwright 依赖 =================
if not exist "auto_feishu\package.json" goto CHECK_ENV
if exist "auto_feishu\node_modules" (
    echo [OK] auto_feishu node_modules 依赖检查通过！
    goto CHECK_ENV
)

echo [!] 警告: 未检测到 auto_feishu 的 Node.js 依赖包 (node_modules)。
set /p CHOICE_NPM="[?] 是否自动安装 auto_feishu 依赖和 Playwright 浏览器？ [Y/N]: "
if /i not "%CHOICE_NPM%"=="Y" if /i not "%CHOICE_NPM%"=="" (
    echo [-] 已跳过 auto_feishu npm 依赖安装。
    goto CHECK_ENV
)

pushd auto_feishu
echo [!] 正在执行 npm install ...
call npm install
echo [!] 正在安装 Playwright Chromium 浏览器内核...
call npx playwright install chromium
popd
echo [OK] auto_feishu 依赖和 Playwright 浏览器安装完成！

:CHECK_ENV
echo.
REM ================= 4. .env 环境文件检查 =================
if exist ".env" (
    echo [OK] 环境文件 .env 已通过检查！
    goto CHECK_CLAUDE
)

if not exist "examples\.env.example" goto CHECK_CLAUDE
echo [!] 警告: 未检测到环境文件 .env。
set /p CHOICE_ENV="[?] 是否自动从 examples\.env.example 创建初始 .env 环境文件？ [Y/N]: "
if /i not "%CHOICE_ENV%"=="Y" if /i not "%CHOICE_ENV%"=="" goto CHECK_CLAUDE
copy "examples\.env.example" ".env" >nul
echo [OK] 初始 .env 环境文件已创建（飞书凭据稍后由 setup.cmd 自动写入）。

REM ================= 5. Claude Code CLI 检查 =================
:CHECK_CLAUDE
echo.
where claude >nul 2>&1
if errorlevel 1 goto CLAUDE_ASK
call claude --version >nul 2>&1
if errorlevel 1 goto CLAUDE_ASK
echo [OK] Claude Code CLI 检查通过！
goto CHECK_PROVIDER

:CLAUDE_ASK
echo [!] 警告: 未检测到 Claude Code CLI (myclaw 依赖它执行任务)。
set /p CHOICE_CLAUDE="[?] 是否自动安装 Claude Code CLI (npm 全局安装，走国内镜像)？ [Y/N]: "
if /i not "%CHOICE_CLAUDE%"=="Y" if /i not "%CHOICE_CLAUDE%"=="" goto CLAUDE_SKIP
echo [!] 正在通过 npmmirror 安装 @anthropic-ai/claude-code ...
call npm install -g @anthropic-ai/claude-code --registry=https://registry.npmmirror.com
if errorlevel 1 (
    echo [ERROR] Claude Code CLI 安装失败。可手动执行: npm install -g @anthropic-ai/claude-code
    pause
    exit /b 1
)
where claude >nul 2>&1
if errorlevel 1 (
    echo [注意] 安装完成但当前窗口找不到 claude 命令，请重开 cmd 后重跑本脚本验证。
) else (
    echo [OK] Claude Code CLI 安装完成！
)
goto CHECK_PROVIDER

:CLAUDE_SKIP
echo [-] 已跳过 Claude Code CLI 安装。

REM ================= 6. 模型供应商 API Key 配置 =================
:CHECK_PROVIDER
echo.
set "ACTIVE_PROFILE="
if exist "config\active_profile" (
    for /f "usebackq delims=" %%p in ("config\active_profile") do set ACTIVE_PROFILE=%%p
)
if exist "config\settings_%ACTIVE_PROFILE%.json" (
    echo [OK] 模型供应商已配置: %ACTIVE_PROFILE%
    goto FINISH
)

echo [!] 尚未配置模型供应商 (没有 API Key 机器人无法对话)。
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv 不存在，无法写入配置。请先完成第 2 步 Python 环境安装。
    pause
    exit /b 1
)
REM 供应商向导：预置三家 + 自定义（名称/地址/Key/三档模型，回车=三档同名），
REM 可连续配置多个，结束时选择当前生效供应商。输入交互全部由 python 端处理。
".venv\Scripts\python.exe" "scripts\setup_provider.py"
if errorlevel 1 (
    echo [ERROR] 供应商配置失败。
    pause
    exit /b 1
)

:FINISH
echo.
echo ===================================================
echo              自检和依赖安装已完成！
if "%NEED_RESTART_CMD%"=="1" (
    echo [注意] 已安装全局系统组件，请重新打开 cmd 窗口让环境变量完全生效。
)
echo.
echo 后续步骤:
echo   1. 双击 auto_feishu\setup.cmd 一键配置飞书机器人 (自动写入 .env 凭据)
echo   2. 双击 MyClaw.bat 启动服务
echo   3. 在 .env 的 ALLOWED_USERS 中加入使用者飞书 Open ID (留空=允许所有人)

if not exist "scripts\setup_autostart.bat" goto END_ALL
echo.
set /p CHOICE_AUTO="[?] 是否设置开机自启动（重启电脑后 MyClaw 自动运行）？ [Y/N]: "
if /i "%CHOICE_AUTO%"=="Y" call "scripts\setup_autostart.bat"

:END_ALL
echo ===================================================
pause
