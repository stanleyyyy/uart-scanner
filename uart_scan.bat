@echo off
rem ---------------------------------------------------------------------------
rem Clickable launcher for uart_scan.py
rem Runs the UART scanner in this same console window (needed for the arrow-key
rem TeraTerm menu) and keeps the window open afterwards.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title UART scanner

rem Keep the window compact (cols x lines).
mode con: cols=100 lines=40

rem Prefer the Python launcher; fall back to python on PATH.
where py >nul 2>&1 && (py uart_scan.py %*) || (python uart_scan.py %*)

echo.
echo ---------------------------------------------------------------------------
pause
endlocal
