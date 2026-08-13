@echo off
rem ===========================================================================
rem install.bat  -  one-shot setup for the UART scanner
rem   * installs the Python dependency (pyserial)
rem   * creates Desktop + Start Menu shortcuts
rem Run by double-clicking, or from a console:  install.bat
rem ===========================================================================
setlocal
cd /d "%~dp0"
title UART scanner - install

echo Installing Python dependency (pyserial)...
where py >nul 2>&1 && (py -m pip install --user -r requirements.txt) || (python -m pip install --user -r requirements.txt)
if errorlevel 1 (
    echo.
    echo [!] pip install failed. Make sure Python 3 is installed and on PATH.
    echo     https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
    echo.
    pause
    exit /b 1
)

echo.
echo Creating shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_shortcuts.ps1"

echo.
echo ---------------------------------------------------------------------------
echo Setup complete. Launch from the "UART Scanner" shortcut, or run uart_scan.bat.
echo ---------------------------------------------------------------------------
pause
endlocal
