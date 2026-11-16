@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo [FAIL] PowerShell not found.
    pause
    exit /b 1
)

echo.
echo   Xiaomi NAS toolkit - one-click deploy
echo   (close this window to abort at any prompt)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%deploy.ps1" %*

echo.
echo Exit code: %errorlevel%
pause
