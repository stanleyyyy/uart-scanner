@echo off
rem ===========================================================================
rem build_installer.bat  -  build the shareable Setup exe with Inno Setup
rem   1. ensures the standalone app exists (dist\uart_scan.exe)
rem   2. compiles installer\uart-scanner.iss -> installer\Output\UART-Scanner-Setup.exe
rem Requires Inno Setup (iscc) on PATH:  scoop install inno-setup
rem ===========================================================================
setlocal
cd /d "%~dp0"
title UART scanner - build installer

if not exist "..\dist\uart_scan.exe" (
    echo dist\uart_scan.exe not found -- building the app first...
    call "..\build.bat"
)

where iscc >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Inno Setup compiler ^(iscc^) not found.
    echo     Install it with:  scoop install inno-setup
    echo     or from https://jrsoftware.org/isdl.php
    echo.
    pause
    exit /b 1
)

echo Compiling installer...
iscc "uart-scanner.iss" || goto :err

echo.
echo ---------------------------------------------------------------------------
echo Installer built: %~dp0Output\UART-Scanner-Setup.exe
echo Share that single file. Recipients just run it -- no Python needed.
echo ---------------------------------------------------------------------------
pause
exit /b 0

:err
echo.
echo [!] Installer build failed. See messages above.
pause
exit /b 1
