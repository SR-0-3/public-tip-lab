@echo off
setlocal
cd /d "%~dp0.."

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.12 from python.org, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :failed
python -m pip install -e .
if errorlevel 1 goto :failed

if not exist ".env" copy ".env.example" ".env" >nul
python -m reddit_bet_lab init
if errorlevel 1 goto :failed

echo.
echo Setup complete. Open .env in Notepad and add your Reddit and Odds API credentials.
echo Then run scripts\start_experiment.bat.
pause
exit /b 0

:failed
echo.
echo Setup failed. Read the error above, then try again.
pause
exit /b 1

