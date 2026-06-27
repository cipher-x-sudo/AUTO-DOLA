@echo off
setlocal
cd /d "%~dp0"
python "%~dp0auto_hook.py" --launch %*
if errorlevel 1 pause
