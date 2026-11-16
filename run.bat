@echo off
rem Starts Twitch Shorts Studio. Creates a virtual environment on first run.
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist tools\ffmpeg\bin\ffmpeg.exe (
    where ffmpeg >nul 2>nul || .venv\Scripts\python tools\setup_ffmpeg.py
)
.venv\Scripts\python -m app.main %*
if errorlevel 1 pause
