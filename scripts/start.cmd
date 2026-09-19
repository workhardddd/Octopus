@echo off
rem Double-click this to start Octopus. The real work is in start.ps1, which
rem documents what it does and why; this file exists so that double-clicking
rem works without typing anything, and so that a failure leaves the window open
rem to be read.
setlocal
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Pause %*
if errorlevel 1 (
    echo.
    pause
)
endlocal
