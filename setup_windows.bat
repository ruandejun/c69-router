@echo off
title GenRouter - Thiet lap may Windows moi

:: Tu kiem tra va xin quyen Administrator neu chua co
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Dang yeu cau quyen Administrator...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"

echo.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
