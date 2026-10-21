@echo off
cd /d "%~dp0"
title x1c-auto  (web + tunnel)

echo ============================================================
echo   1/2  starting web server  (http://127.0.0.1:8080)
echo        a minimized window "x1c-web 8080" will open - keep it
echo ============================================================
start "x1c-web 8080" /min python server.py --no-browser

echo waiting for the server...
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8080/#digital-twin

echo.
echo ============================================================
echo   2/2  starting public tunnel
echo        copy the https://....trycloudflare.com line below
echo        (same-LAN devices can use http://%COMPUTERNAME%:8080 too)
echo ============================================================
echo.

cloudflared.exe tunnel --url http://127.0.0.1:8080 --no-autoupdate 2>&1 | findstr /C:"trycloudflare.com" /C:"ERR" /C:"failed"

echo.
echo Tunnel stopped. The web server is still running in the
echo minimized window "x1c-web 8080" - close it to stop.
pause
