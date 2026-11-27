@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0tools\export_source_safe.py" %*
if errorlevel 1 pause
