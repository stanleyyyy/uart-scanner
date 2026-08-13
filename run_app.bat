@echo off
rem ---------------------------------------------------------------------------
rem run_app.bat  -  inner runner (called by scripts\launch.vbs in a pre-sized,
rem pre-titled window, so it must NOT call `title` or `mode con` itself).
rem Prefers the standalone exe, falls back to the Python script.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
if exist "%~dp0dist\uart_scan.exe" (
    "%~dp0dist\uart_scan.exe" --no-resize %*
) else (
    where py >nul 2>&1 && (py "%~dp0uart_scan.py" --no-resize %*) || (python "%~dp0uart_scan.py" --no-resize %*)
)
echo.
pause
