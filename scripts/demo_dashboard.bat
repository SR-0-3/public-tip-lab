@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup_windows.bat first.
  pause
  exit /b 1
)
if not exist "data\demo.sqlite3" ".venv\Scripts\python.exe" -m reddit_bet_lab --db data\demo.sqlite3 seed-demo
".venv\Scripts\python.exe" -m reddit_bet_lab --db data\demo.sqlite3 dashboard

