@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo             auto_feishu 环境自检与一键配置
echo ===================================================
echo.

REM === 1. Node.js 20+ 版本检查（必需） ===
set NODE_OK=0
where node >nul 2>&1
if errorlevel 1 goto CHECK_NODE_DONE

for /f "tokens=1 delims=v. " %%a in ('node -v') do set NODE_MAJOR=%%a
if not defined NODE_MAJOR goto CHECK_NODE_DONE
if %NODE_MAJOR% geq 20 set NODE_OK=1

:CHECK_NODE_DONE
if "%NODE_OK%"=="1" (
    echo [OK] Node.js 20+ 检查通过！
    goto DO_NPM_INSTALL
)

echo [!] 警告: 未检测到 Node.js 20+ 环境 (auto_feishu 需要 Node.js v20 或更高版本)。
set /p CHOICE_NODE="[?] 是否自动下载并静默安装 Node.js v20.18.0 LTS？ [Y/N]: "
if /i not "%CHOICE_NODE%"=="Y" if /i not "%CHOICE_NODE%"=="" (
    echo [ERROR] 缺少 Node.js 20+，无法运行 auto_feishu 飞书自动配置。
    pause
    exit /b 1
)

echo [!] 正在通过国内镜像下载 Node.js 20.18.0 官方安装包...
set "MSI_PATH=%TEMP%\node_v20.msi"
curl.exe -L -o "%MSI_PATH%" "https://npmmirror.com/mirrors/node/v20.18.0/node-v20.18.0-x64.msi"
if exist "%MSI_PATH%" (
    echo [!] 正在静默安装 Node.js...
    msiexec.exe /i "%MSI_PATH%" /quiet /norestart
    if errorlevel 1 (
        del /f /q "%MSI_PATH%" >nul 2>&1
        echo [ERROR] Node.js 静默安装失败（通常是因为没有管理员权限）。
        echo 请以管理员身份重开 cmd 再运行本脚本，或手动安装 Node.js 20+ 后重跑。
        pause
        exit /b 1
    )
    del /f /q "%MSI_PATH%" >nul 2>&1
    set "PATH=%ProgramFiles%\nodejs;%PATH%"
    echo [OK] Node.js 20.18.0 安装完成！
    echo [注意] 如后续 node 命令不可用，请重新运行本脚本或重开 cmd 窗口。
) else (
    echo [ERROR] Node.js 安装包下载失败，请检查网络后重试。
    pause
    exit /b 1
)

:DO_NPM_INSTALL
echo.
REM === 2. 锁定版 npm 依赖安装（实跑验证，防半成品 node_modules） ===
if not exist node_modules\.bin\tsx.cmd goto TSX_MISSING
call node_modules\.bin\tsx.cmd --version >nul 2>&1
if errorlevel 1 goto TSX_MISSING
echo [OK] npm 依赖检查通过！
goto CHROMIUM_INSTALL

:TSX_MISSING
echo [INFO] npm 依赖缺失或损坏，正在重装（先清理旧 node_modules）...
if exist node_modules rmdir /s /q node_modules
call npm ci --ignore-scripts
if errorlevel 1 (
    echo [ERROR] npm ci failed.
    pause
    exit /b 1
)
call node_modules\.bin\tsx.cmd --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm ci 完成后 tsx 仍不可用，请把以上输出发给支持人员。
    pause
    exit /b 1
)

:CHROMIUM_INSTALL
REM === 3. Chromium 安装（幂等：playwright 自行校验所需构建版本，已装且匹配则秒过） ===
echo [INFO] Ensuring Playwright Chromium is installed...
call npx playwright install chromium
if errorlevel 1 (
    echo [ERROR] Chromium install failed. Without it Feishu automation cannot run.
    echo   Common causes:
    echo     1. Network / firewall blocked the download
    echo     2. Company proxy required ^(set HTTP_PROXY^)
    echo     3. Disk space issue
    echo   Retry manually:  cd auto_feishu ^& npx playwright install chromium
    pause
    exit /b 1
)

echo.
echo [INFO] Starting Feishu one-click setup...
call npm run feishu:setup
if errorlevel 1 (
    echo [ERROR] Feishu setup failed.
    echo Re-run setup.cmd to resume from the last completed step.
    pause
    exit /b 1
)
echo.
echo [OK] Feishu setup completed.
pause
exit /b 0
