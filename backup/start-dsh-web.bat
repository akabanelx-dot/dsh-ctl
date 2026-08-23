@echo off
rem ============================================================
rem  DeepSeek Harness Launcher (fixed version, ASCII-safe)
rem  Double-click this file to start the harness.
rem ============================================================
title DeepSeek Harness

set "URL=http://127.0.0.1:3080"
set "DSH_CMD=%APPDATA%\npm\dsh.cmd"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Chrome*\DeepSeek Harness.lnk"

rem ---- 1. already running? ----
powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:3080' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode){exit 0}}catch{}; exit 1"
if not errorlevel 1 goto open

rem ---- 2. start dsh web fully hidden (log to file) ----
set "DSH_LOG=%APPDATA%\DeepSeekHarness\dsh-web.log"
if not exist "%APPDATA%\DeepSeekHarness" mkdir "%APPDATA%\DeepSeekHarness"
if exist "%DSH_CMD%" (
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%DSH_CMD%' -ArgumentList 'web' -RedirectStandardOutput '%DSH_LOG%' -RedirectStandardError '%DSH_LOG%.err' -WindowStyle Hidden"
) else (
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath 'dsh' -ArgumentList 'web' -RedirectStandardOutput '%DSH_LOG%' -RedirectStandardError '%DSH_LOG%.err' -WindowStyle Hidden"
)

rem ---- 3. wait up to 30s for the service ----
powershell -NoProfile -Command "for($i=0;$i -lt 30;$i++){try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:3080' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode){exit 0}}catch{}; Start-Sleep -Seconds 1}; exit 1"
if errorlevel 1 echo [WARN] Web service did not become ready in 30s. Check dsh web output.

:open
rem ---- 4. open the DeepSeek Harness PWA ----
if exist "%LNK%" (
  start "" "%LNK%"
) else (
  start "" "http://127.0.0.1:3080"
)
exit /b
