@echo off
setlocal
cd /d "%~dp0"

if /I "%HTTP_PROXY%"=="http://127.0.0.1:6984" set HTTP_PROXY=
if /I "%HTTPS_PROXY%"=="http://127.0.0.1:6984" set HTTPS_PROXY=
if /I "%http_proxy%"=="http://127.0.0.1:6984" set http_proxy=
if /I "%https_proxy%"=="http://127.0.0.1:6984" set https_proxy=

echo ===================================================
echo             auto_feishu Setup and Pre-check
echo ===================================================
echo.

REM === 1. Node.js 20+ check ===
set NODE_OK=0
where node >nul 2>&1
if errorlevel 1 goto CHECK_NODE_DONE

for /f "tokens=1 delims=v. " %%a in ('node -v') do set NODE_MAJOR=%%a
if not defined NODE_MAJOR goto CHECK_NODE_DONE
if %NODE_MAJOR% geq 20 set NODE_OK=1

:CHECK_NODE_DONE
if "%NODE_OK%"=="1" (
    echo [OK] Node.js 20+ detected.
    goto DO_NPM_INSTALL
)

echo [!] Warning: Node.js 20+ not detected (auto_feishu requires Node.js v20+).
set /p CHOICE_NODE="[?] Download and install Node.js v20.18.0 LTS automatically? [Y/N]: "
if /i not "%CHOICE_NODE%"=="Y" if /i not "%CHOICE_NODE%"=="" (
    echo [ERROR] Missing Node.js 20+. Cannot continue auto_feishu setup.
    pause
    exit /b 1
)

echo [*] Downloading Node.js 20.18.0 installer...
set "MSI_PATH=%TEMP%\node_v20.msi"
curl.exe -L -o "%MSI_PATH%" "https://npmmirror.com/mirrors/node/v20.18.0/node-v20.18.0-x64.msi"
if exist "%MSI_PATH%" (
    echo [*] Installing Node.js silently...
    msiexec.exe /i "%MSI_PATH%" /quiet /norestart
    del /f /q "%MSI_PATH%" >nul 2>&1
    set "PATH=%ProgramFiles%\nodejs;%PATH%"
    echo [OK] Node.js 20.18.0 installed.
) else (
    echo [ERROR] Failed to download Node.js installer. Please check your network.
    pause
    exit /b 1
)

:DO_NPM_INSTALL
echo.
REM === 2. npm dependencies check ===
if not exist node_modules\.bin\tsx.cmd goto TSX_MISSING
call node_modules\.bin\tsx.cmd --version >nul 2>&1
if errorlevel 1 goto TSX_MISSING
echo [OK] npm dependencies verified.
goto CHROMIUM_INSTALL

:TSX_MISSING
echo [INFO] npm dependencies missing. Installing (npm ci)...
if exist node_modules rmdir /s /q node_modules
call npm ci --ignore-scripts
if errorlevel 1 (
    echo [ERROR] npm ci failed.
    pause
    exit /b 1
)
call node_modules\.bin\tsx.cmd --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] tsx is still unavailable after npm ci.
    pause
    exit /b 1
)

:CHROMIUM_INSTALL
REM === 3. Playwright Chromium ===
echo [INFO] Ensuring Playwright Chromium is installed...
node -e "const { chromium } = require('playwright'); const p = chromium.executablePath(); process.exit(require('fs').existsSync(p) ? 0 : 1);" >nul 2>nul
if %errorlevel% equ 0 (
    echo [OK] Playwright Chromium browser driver is ready.
) else (
    echo [INFO] Downloading Playwright Chromium via mirror accelerator...
    if exist "%LOCALAPPDATA%\ms-playwright\__dirlock" rd /s /q "%LOCALAPPDATA%\ms-playwright\__dirlock" 2>nul
    set "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright"
    call npx playwright install chromium
    if errorlevel 1 (
        echo [ERROR] Chromium install failed. Without it Feishu automation cannot run.
        echo   Common causes:
        echo     1. Network / firewall blocked the download
        echo     2. Company proxy required ^(set HTTP_PROXY^)
        echo     3. Disk space issue
        echo   Retry manually: cd auto_feishu ^& npx playwright install chromium
        pause
        exit /b 1
    )
)

echo.
if not "%~1"=="" set "FEISHU_DEPLOY_MODE=%~1"
if not defined FEISHU_DEPLOY_MODE (
    echo Select Feishu deployment mode:
    echo   1. Personal (creator only)
    echo   2. Public (organization-wide, supports group member import)
    set /p MODE_INPUT="Enter option [1/2] (default 1): "
    if "%MODE_INPUT%"=="2" (
        set "FEISHU_DEPLOY_MODE=public"
    ) else (
        set "FEISHU_DEPLOY_MODE=personal"
    )
)

echo [INFO] Deployment mode: %FEISHU_DEPLOY_MODE%
echo [INFO] Starting Feishu automation setup...
if /i "%FEISHU_DEPLOY_MODE%"=="personal" (
    call npm run feishu:setup -- --personal
) else (
    call npm run feishu:setup
)
if errorlevel 1 (
    echo [ERROR] Feishu setup failed.
    pause
    exit /b 1
)

echo.
echo [OK] Feishu setup completed.
if "%~1"=="" pause
cd /d "%~dp0.."
exit /b 0
