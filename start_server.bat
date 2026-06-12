@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   丰源工作台 - Worklog App
echo ============================================
echo.
echo Starting Flask server...
echo Server: http://127.0.0.1:5050
echo Press Ctrl+C to stop
echo.
echo start "" http://127.0.0.1:5050
python app.py
pause
