@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo           Configuring MyCodex AutoStart...
echo ===================================================
echo.

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "NEW_LNK=%STARTUP_DIR%\MyCodex.lnk"
set "MYCODEX_BAT=%~dp0..\MyCodex.bat"

if not exist "%MYCODEX_BAT%" (
    echo [ERROR] MyCodex.bat not found in parent directory!
    echo Please run this script from MyCodex scripts directory.
    echo.
    pause
    exit /b 1
)


echo [1/1] Creating new MyCodex startup shortcut...
set "VBS_SCRIPT=%TEMP%\create_mycodex_shortcut.vbs"

echo Set WshShell = CreateObject("WScript.Shell") > "%VBS_SCRIPT%"
echo Set shortcut = WshShell.CreateShortcut("%NEW_LNK%") >> "%VBS_SCRIPT%"
echo shortcut.TargetPath = "%MYCODEX_BAT%" >> "%VBS_SCRIPT%"
echo shortcut.WorkingDirectory = "%~dp0.." >> "%VBS_SCRIPT%"
echo shortcut.WindowStyle = 7 >> "%VBS_SCRIPT%"
echo shortcut.Description = "MyCodex AutoStart Service" >> "%VBS_SCRIPT%"
echo shortcut.Save >> "%VBS_SCRIPT%"

cscript //nologo "%VBS_SCRIPT%"
if exist "%VBS_SCRIPT%" del /f /q "%VBS_SCRIPT%"

echo.
echo ===================================================
echo [SUCCESS] MyCodex AutoStart configured successfully!
echo ===================================================
echo Shortcut Location : %NEW_LNK%
echo Target Program   : %MYCODEX_BAT%
echo.
pause
