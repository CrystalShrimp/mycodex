@echo off
cd /d "%~dp0.."
echo Running ruff (F821/F811/F823 fatal symbol checks)...
".venv\Scripts\ruff.exe" check app/ scripts/
if errorlevel 1 (
    echo.
    echo [LINT FAILED] Fix the errors above before committing.
    exit /b 1
)
echo.
echo [LINT OK] No fatal symbol errors.
