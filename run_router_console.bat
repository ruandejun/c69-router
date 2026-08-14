@echo off
title C69-Router Interactive Console
chcp 65001 >nul
cd /d "D:\Workspace\Python\c69-router"
echo ===================================================
echo   C69-ROUTER INTERACTIVE LIVE CONSOLE LOGS
echo ===================================================
echo.
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
"D:\Workspace\Python\c69-router\.venv\Scripts\python.exe" -u -m uvicorn app.main:app --host 0.0.0.0 --port 9000
echo.
echo Application stopped. Press any key to exit.
pause >nul
