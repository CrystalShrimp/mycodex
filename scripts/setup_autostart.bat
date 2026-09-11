@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo           Configuring MyClaw AutoStart...
echo ===================================================
echo.

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "NEW_LNK=%STARTUP_DIR%\MyClaw.lnk"
set "MYCLAW_BAT=%~dp0..\MyClaw.bat"

if not exist "%MYCLAW_BAT%" (
    echo [ERROR] MyClaw.bat not found in parent directory!
    echo Please run this script from MyClaw scripts directory.
    echo.
    pause
    exit /b 1
)

echo [1/2] Cleaning up old OpenClaw startup shortcuts...
if exist "%STARTUP_DIR%\OpenClaw.lnk" del /f /q "%STARTUP_DIR%\OpenClaw.lnk"
if exist "%STARTUP_DIR%\OpenClaw.bat.lnk" del /f /q "%STARTUP_DIR%\OpenClaw.bat.lnk"
if exist "%STARTUP_DIR%\OpenClaw-Debug.lnk" del /f /q "%STARTUP_DIR%\OpenClaw-Debug.lnk"

echo [2/2] Creating new MyClaw startup shortcut...
set "VBS_SCRIPT=%TEMP%\create_myclaw_shortcut.vbs"

echo Set WshShell = CreateObject("WScript.Shell") > "%VBS_SCRIPT%"
echo Set shortcut = WshShell.CreateShortcut("%NEW_LNK%") >> "%VBS_SCRIPT%"
echo shortcut.TargetPath = "%MYCLAW_BAT%" >> "%VBS_SCRIPT%"
echo shortcut.WorkingDirectory = "%~dp0.." >> "%VBS_SCRIPT%"
echo shortcut.WindowStyle = 7 >> "%VBS_SCRIPT%"
echo shortcut.Description = "MyClaw AutoStart Service" >> "%VBS_SCRIPT%"
echo shortcut.Save >> "%VBS_SCRIPT%"

cscript //nologo "%VBS_SCRIPT%"
if exist "%VBS_SCRIPT%" del /f /q "%VBS_SCRIPT%"

echo.
echo ===================================================
echo [SUCCESS] MyClaw AutoStart configured successfully!
echo ===================================================
echo Shortcut Location : %NEW_LNK%
echo Target Program   : %MYCLAW_BAT%
echo.
pause
