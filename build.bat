@echo off
rem ===========================================================================
rem build.bat  -  build a standalone single-file uart_scan.exe with PyInstaller
rem Output: dist\uart_scan.exe  (no Python needed on target machines)
rem ===========================================================================
setlocal
cd /d "%~dp0"
title UART scanner - build

set PY=python
where py >nul 2>&1 && set PY=py

echo Installing build/runtime deps...
%PY% -m pip install --user -r requirements.txt pyinstaller || goto :err

echo.
echo Building single-file exe...
%PY% -m PyInstaller --onefile --console --name uart_scan ^
    --hidden-import serial.tools.list_ports ^
    uart_scan.py || goto :err

echo.
echo ---------------------------------------------------------------------------
echo Built: %~dp0dist\uart_scan.exe
echo ---------------------------------------------------------------------------
pause
exit /b 0

:err
echo.
echo [!] Build failed. See the messages above.
pause
exit /b 1
