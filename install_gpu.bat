@echo off
rem Installs NVIDIA CUDA libraries (cuBLAS + cuDNN, about 1 GB) so that speech
rem recognition can run on the GPU. Safe to run again.
cd /d "%~dp0"
if not exist .venv (
    echo Run run.bat once first.
    pause
    exit /b 1
)
.venv\Scripts\python -m pip install -r requirements-gpu.txt
if errorlevel 1 (
    echo Installation failed. Check your internet connection.
    pause
    exit /b 1
)
echo.
echo GPU support installed. Restart Twitch Shorts Studio.
pause
