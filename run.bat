@echo off
rem Starts Twitch Shorts Studio. Creates a virtual environment on first run.
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
)
rem Install or update dependencies only when requirements.txt has changed.
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    .venv\Scripts\python -m pip install -r requirements.txt
    if errorlevel 1 if exist wheels (
        echo Trying the wheels folder...
        .venv\Scripts\python -m pip install --no-index --find-links wheels -r requirements.txt
    )
    if errorlevel 1 (
        echo Dependency installation failed. Check your internet connection.
        pause
        exit /b 1
    )
    copy /y requirements.txt .venv\requirements.installed >nul
)
if not exist tools\ffmpeg\bin\ffmpeg.exe (
    where ffmpeg >nul 2>nul || .venv\Scripts\python tools\setup_ffmpeg.py
)
.venv\Scripts\python -m app.main %*
if errorlevel 1 pause
