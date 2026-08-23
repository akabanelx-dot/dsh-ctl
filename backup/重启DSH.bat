@echo off
rem ============================================================
rem  DeepSeek Harness Restart Script
rem  Kills the running service on port 3080, starts a fresh
rem  dsh web, waits for it to be ready, then opens the PWA.
rem  Double-click to run.
rem ============================================================
title DeepSeek Harness - Restart

echo [1/4] Stopping existing dsh service...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3080" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo [2/4] Starting dsh web...
set "DSH_CMD=%APPDATA%\npm\dsh.cmd"
set "DSH_LOG=%APPDATA%\DeepSeekHarness\dsh-web.log"
if not exist "%APPDATA%\DeepSeekHarness" mkdir "%APPDATA%\DeepSeekHarness"
if exist "%DSH_CMD%" (
    powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%DSH_CMD%' -ArgumentList 'web' -RedirectStandardOutput '%DSH_LOG%' -RedirectStandardError '%DSH_LOG%.err' -WindowStyle Hidden"
) else (
    powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath 'dsh' -ArgumentList 'web' -RedirectStandardOutput '%DSH_LOG%' -RedirectStandardError '%DSH_LOG%.err' -WindowStyle Hidden"
)

echo [3/4] Waiting for service on port 3080 (up to 30s)...
powershell -NoProfile -Command "for($i=0;$i -lt 30;$i++){try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:3080' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode){exit 0}}catch{}; Start-Sleep -Seconds 1}; exit 1"
if errorlevel 1 (
    echo [FAIL] Service did not become ready in 30s. Check the minimized dsh-web window for errors.
    pause
    exit /b 1
)

echo [4/4] Opening DeepSeek Harness...
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Chrome*\DeepSeek Harness.lnk"
if exist "%LNK%" (
    start "" "%LNK%"
) else (
    start "" "http://127.0.0.1:3080"
)

echo Done. dsh restarted successfully.
exit /b 0
