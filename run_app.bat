@echo off
rem Launch windowless (system tray). Output goes to app.log.
rem If something seems broken, use run_app_debug.bat to see live output.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw app.py
) else (
  python app.py
  pause
)
