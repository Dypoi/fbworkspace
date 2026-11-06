@echo off
REM Klik dua kali file ini untuk menjalankan dashboard FB Workspace.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_windows.ps1"
echo.
pause
