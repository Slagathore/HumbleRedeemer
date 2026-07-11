@echo off
rem Console launcher: keeps a window open so you can watch logs / see errors.
cd /d "%~dp0"
set APP_TRAY=0
python app.py
pause
