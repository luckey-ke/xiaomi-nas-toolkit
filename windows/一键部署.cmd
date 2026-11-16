@echo off
rem ============================================================
rem  小米智能存储（RP05）一键部署 —— 双击这个文件就行
rem  中途想取消：直接关掉这个窗口
rem  进阶用法（单步执行 / 卸载）：见 docs\04-运维手册.md
rem ============================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
echo.
pause
