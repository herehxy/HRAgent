@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "TP_USER="
set /p TP_USER=请输入要重置的账号（直接回车 = admin）:
if not defined TP_USER set "TP_USER=admin"
echo 正在把账号 %TP_USER% 的密码重置为 change-me ...
"HRAgent工作台.exe" --reset-password "%TP_USER%"
echo.
pause
