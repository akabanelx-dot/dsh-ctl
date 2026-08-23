@echo off
rem ============================================================
rem  DeepSeek Harness Stop Script
rem  Kills the dsh web service listening on port 3080 and
rem  verifies it is fully stopped. Double-click to run.
rem  (The PWA window, if open, closes itself once the
rem   service is gone; you can also close it manually.)
rem ============================================================
title DeepSeek Harness - Stop

echo [1/2] Stopping dsh service on port 3080...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3080" ^| findstr "LISTENING"') do (
    set FOUND=1
    echo   killing PID %%p ...
    taskkill /F /T /PID %%p >nul 2>&1
)
if "%FOUND%"=="0" echo   (no dsh service was running)

echo [2/2] Verifying...
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr ":3080" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo Done. dsh is fully stopped.
) else (
    echo [WARN] Port 3080 is still in use. Check manually:
    netstat -ano | findstr ":3080"
)

echo.
echo Note: close the DeepSeek Harness PWA window if it is open.
pause
exit /b 0
