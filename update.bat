@echo off
REM Double-click launcher for update.ps1 (bypasses PowerShell execution policy).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
pause
