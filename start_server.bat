@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Worklog App - Fengyuan Workbench
echo ============================================
echo.

REM -- Firewall rule (only effective if running as admin) --
net session >nul 2>&1
if %errorlevel% == 0 (
    echo [1/3] Adding Windows Firewall rule for port 5050...
    netsh advfirewall firewall delete rule name="Worklog 5050" >nul 2>&1
    netsh advfirewall firewall add rule name="Worklog 5050" dir=in action=allow protocol=TCP localport=5050 profile=private >nul
    echo       OK
) else (
    echo [1/3] Skipping firewall rule ^(not admin^). Run setup_firewall.bat once as admin if needed.
)
echo.

REM -- Detect LAN IP (first non-loopback IPv4) --
echo [2/3] Detecting LAN IP...
set "ip="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
    for /f "tokens=*" %%b in ("%%a") do (
        if not defined ip set "ip=%%b"
    )
)
if defined ip (
    echo       LAN IP: %ip%
) else (
    echo       LAN IP: ^(detection failed^)
)
echo.

echo [3/3] Starting Flask server ^(host=0.0.0.0 for LAN access^)...
echo       Local:   http://127.0.0.1:5050
if defined ip echo       Network: http://%ip%:5050
echo       Press Ctrl+C to stop.
echo.

start "" http://127.0.0.1:5050
python app.py
pause