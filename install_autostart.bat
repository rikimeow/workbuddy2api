@echo off
rem ===========================================================================
rem  workbuddy2api resident install (RUN AS ADMINISTRATOR)
rem ---------------------------------------------------------------------------
rem  1. Register scheduled task "workbuddy2api": start at boot (ONSTART,
rem     SYSTEM account, no user logon required), auto-restart on failure.
rem  2. Start it now (no need to reboot).
rem  3. Print verification info.
rem
rem  Uninstall:  schtasks /Delete /TN "workbuddy2api" /F
rem ===========================================================================

set TASK=workbuddy2api
set ROOT=C:\Users\riki\Documents\workbuddy2api\workbuddy2api-master
set PS=powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%\scripts\run_daemon.ps1"

echo [1/3] Registering scheduled task %TASK% ...
schtasks /Create /TN "%TASK%" /TR "%PS%" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
    echo   FAILED. Make sure this window is "Run as administrator".
    exit /b 1
)

echo [2/3] Configuring auto-restart ...
schtasks /Change /TN "%TASK%" /RI 1 >nul 2>&1

echo [3/3] Starting now ...
schtasks /Run /TN "%TASK%"

echo.
echo Done. Verify with:
echo   schtasks /Query /TN "%TASK%" /V /FO LIST
echo   powershell -File "%ROOT%\scripts\run_daemon.ps1" -Status
echo Uninstall with:
echo   schtasks /Delete /TN "%TASK%" /F
pause
