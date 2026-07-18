@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup_windows.bat first.
  pause
  exit /b 1
)

start "Public Tip Lab Collector" cmd /k call ".venv\Scripts\python.exe" -m reddit_bet_lab watch
timeout /t 2 /nobreak >nul
".venv\Scripts\python.exe" -m reddit_bet_lab dashboard
