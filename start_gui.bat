@echo off
title HabitSync AI - Local Dashboard
echo ========================================================
echo       Launching HabitSync AI GUI Dashboard...
echo ========================================================
echo.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Please run: python -m venv .venv
    pause
    exit /b 1
)

echo Starting server at http://localhost:8000 ...
start http://localhost:8000
.venv\Scripts\python.exe main.py

pause

