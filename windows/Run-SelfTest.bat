@echo off
REM Double-click this. It runs the full Windows self-test and writes selftest-report.txt
REM in the repo folder. Nothing outside .\selftest-data is created or changed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0SelfTest.ps1"
echo.
echo ==========================================================
echo  Done. selftest-report.txt is in the folder above windows\
echo ==========================================================
pause
