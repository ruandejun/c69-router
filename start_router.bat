@echo off
cd /d D:\Workspace\Python\c69-router
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
"D:\Workspace\Python\c69-router\.venv\Scripts\python.exe" -u -m uvicorn app.main:app --host 0.0.0.0 --port 9000 >> D:\Workspace\Python\c69-router\router_run.log 2>&1
