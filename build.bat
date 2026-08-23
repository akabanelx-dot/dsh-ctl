@echo off
rem ============================================================
rem  build.bat - build dsh-ctl.exe with PyInstaller
rem  Produces dist\dsh-ctl.exe and copies it to the real Desktop
rem  (resolved via [Environment]::GetFolderPath('Desktop'),
rem   so OneDrive redirection is handled correctly).
rem ============================================================
cd /d "%~dp0"

echo [1/4] Ensuring build dependencies (versions pinned, no drift)...
rem Install only if missing: upgrading PyInstaller between builds changes the
rem icon PNG encoding and would trip the icon-protection gate for no reason.
rem pyinstaller is HARD-PINNED: 6.22.1's new onefile parent-process check
rem (GHSA-9fxf-4qw3-ghmr) false-positives for windowed exes under non-ASCII
rem user-profile paths ("Security validation failure"), see upstream #9507/#9508.
python -m pip install --quiet pystray pillow "pyinstaller==6.22.0"
if errorlevel 1 (
    echo [FAIL] pip install failed
    pause
    exit /b 1
)

echo [2/4] Building exe (onefile, no console)...
rem ============================================================
rem  Fix stale TCL_LIBRARY/TK_LIBRARY: when this build runs from a shell
rem  spawned by a PyInstaller onefile app (dsh-ctl itself), the inherited
rem  TCL_LIBRARY/TK_LIBRARY point at that app's now-deleted _MEI temp dir,
rem  which makes tkinter.Tcl() fail and PyInstaller silently EXCLUDES
rem  tkinter ("tkinter installation is broken"). Point both variables at
rem  the real Python tcl data dir before building.
rem ============================================================
for /f "delims=" %%p in ('python -c "import sys; print(sys.prefix)"') do set "PYPREFIX=%%p"
set "TCL_LIBRARY=%PYPREFIX%\tcl\tcl8.6"
set "TK_LIBRARY=%PYPREFIX%\tcl\tk8.6"
echo TCL_LIBRARY=%TCL_LIBRARY%
echo TK_LIBRARY=%TK_LIBRARY%
rem ============================================================
rem  Icon protection: the program icon (DeepSeek Harness.ico) and the
rem  tray icon (app_icon.png) are protected assets. Before building we
rem  record their SHA-256 (missing asset aborts the build), and after
rem  building we run verify_icon.py as a GATE: the desktop exe is only
rem  replaced when the built exe's icon matches the PWA icon.
rem ============================================================
for %%f in ("DeepSeek Harness.ico" "app_icon.png") do (
    if not exist %%f (
        echo [FAIL] protected icon asset missing: %%~f
        pause
        exit /b 1
    )
)
certutil -hashfile "DeepSeek Harness.ico" SHA256 > build-icon-hashes.txt 2>nul
certutil -hashfile "app_icon.png" SHA256 >> build-icon-hashes.txt 2>nul
if errorlevel 1 (
    echo [FAIL] cannot hash protected icon assets
    pause
    exit /b 1
)
rem Program icon: --icon (via dsh-ctl.spec icon=). Tray icons: bundled via
rem the spec's datas= so the exe finds them at runtime (onefile extracts to
rem _MEIPASS; tray.py looks them up by name). Building through the spec file
rem keeps the datas declarations structured and stable.
python -m PyInstaller --noconfirm --clean dsh-ctl.spec
if errorlevel 1 (
    echo [FAIL] PyInstaller build failed
    pause
    exit /b 1
)

echo [3/4] Verifying built exe icon (gate before desktop copy)...
python verify_icon.py > build-icon-check.txt 2>&1
if errorlevel 1 (
    echo [FAIL] icon verification error - desktop exe NOT replaced
    type build-icon-check.txt
    pause
    exit /b 1
)
findstr /C:"MATCH" build-icon-check.txt >nul
if errorlevel 1 (
    echo [FAIL] icon verification failed - desktop exe NOT replaced
    type build-icon-check.txt
    pause
    exit /b 1
)
type build-icon-check.txt

echo [4/4] Copying to Desktop...
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set DESKTOP=%%d
if "%DESKTOP%"=="" set "DESKTOP=%USERPROFILE%\Desktop"

echo [4/4] Copying to Desktop...
copy /Y "dist\dsh-ctl.exe" "%DESKTOP%\dsh-ctl.exe" >nul
if errorlevel 1 (
    echo [FAIL] copy to desktop failed
    pause
    exit /b 1
)

echo.
echo [OK] dsh-ctl.exe deployed to: %DESKTOP%\dsh-ctl.exe
pause
