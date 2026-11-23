@echo off
rem insta-roller: starts the app (queue, job runner, browser UI) and opens the UI.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m app %*
if errorlevel 1 pause
