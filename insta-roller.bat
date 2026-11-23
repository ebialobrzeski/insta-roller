@echo off
rem insta-roller: opens the application window (queue, job runner and UI in one program).
rem No console stays open; problems at start are shown in a message box and written to app.log.
rem With options (for example --browser or --no-browser) it runs in this console instead.
cd /d "%~dp0"
if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" -m app
) else (
  ".venv\Scripts\python.exe" -m app %*
  if errorlevel 1 pause
)
