@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."

echo ===================================================
echo            MyCodex Windows Setup Launcher
echo ===================================================
echo.

REM 1. Check and install uv if missing
where uv >nul 2>&1
if errorlevel 1 (
    echo [!] uv not found. Installing uv...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
    where uv >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to install uv automatically.
        echo Please install uv manually from https://docs.astral.sh/uv/
        pause
        exit /b 1
    )
)

REM 2. Ensure Python virtual environment (.venv)
if not exist ".venv\Scripts\python.exe" (
    echo [*] Initializing Python virtual environment via uv sync...
    call uv sync
    if errorlevel 1 (
        echo [ERROR] uv sync failed. Please check your internet connection.
        pause
        exit /b 1
    )
)

REM 3. Run cross-platform setup wizard
".venv\Scripts\python.exe" "scripts\setup_wizard.py"
if errorlevel 1 (
    echo.
    echo [!] Setup wizard exited with error.
    pause
    exit /b 1
)
