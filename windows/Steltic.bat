@echo off
REM Double-clickable entry point. Keeps no console window open once the app is up.
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0Steltic.ps1" %*
