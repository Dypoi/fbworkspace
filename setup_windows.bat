@echo off
REM Klik dua kali file ini untuk setup awal FB Workspace di Windows.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
echo.
pause
