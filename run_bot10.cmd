@echo off
rem Launch script for Ozon Price & Stock Tracker (Project 10).
rem Reads TG_TOKEN from the root .env, sets OZON_BOT_TOKEN, runs the bot.
cd /d "%~dp0"

for /f "usebackq tokens=1,* delims==" %%a in ("..\.env") do (
    if "%%a"=="TG_TOKEN" set "OZON_BOT_TOKEN=%%b"
)
if not defined OZON_BOT_TOKEN (
    echo [ERROR] TG_TOKEN not found in ..\.env
    pause
    exit /b 1
)

rem 0 = real Ozon API (needs internet + proxy) | 1 = demo mode (offline)
set "OZON_DEMO_MODE=1"
set "PYTHONIOENCODING=utf-8"

..\.venv\Scripts\python.exe -u bot.py
