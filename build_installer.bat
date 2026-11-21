@echo off
rem Builds the Windows installer: build\installer\TwitchShortsStudio-Setup-<version>.exe
rem Needs Python 3 and internet on the first run (dependencies, FFmpeg, models, PyInstaller, Inno Setup).
rem Speech models to put in the installer (each one is a checkbox there):
rem   build_installer.bat large-v3-russian-int8 small large-v3-turbo
rem With no names: large-v3-russian-int8 and small. "none" = no models.
setlocal
cd /d "%~dp0"
set MODELS=%*
if "%MODELS%"=="" set MODELS=large-v3-russian-int8 small

if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv || goto :fail
)
echo [1/6] Dependencies...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt "pyinstaller>=6.6" || goto :fail
copy /y requirements.txt .venv\requirements.installed >nul

echo [2/6] FFmpeg...
if not exist tools\ffmpeg\bin\ffmpeg.exe (
    .venv\Scripts\python tools\setup_ffmpeg.py || goto :fail
)

echo [3/6] Models: %MODELS%
if /i "%MODELS%"=="none" (
    if exist build\models rmdir /s /q build\models
) else (
    .venv\Scripts\python tools\fetch_models.py %MODELS% || goto :fail
)

for /f "delims=" %%v in ('.venv\Scripts\python -c "from app.config import APP_VERSION; print(APP_VERSION)"') do set VERSION=%%v
echo Version %VERSION%

echo [4/6] Program files (PyInstaller)...
.venv\Scripts\pyinstaller --noconfirm --clean --distpath build\dist --workpath build\work packaging\twitch_shorts_studio.spec || goto :fail

echo [5/6] Self-test of the built program...
build\dist\TwitchShortsStudio\TwitchShortsStudio.exe --self-test build\self-test.txt
set SELFTEST=%errorlevel%
type build\self-test.txt
if not "%SELFTEST%"=="0" (
    echo The self-test failed, see above.
    goto :fail
)

echo [6/6] Installer (Inno Setup)...
set ISCC=
for %%p in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
    if exist %%p set ISCC=%%p
)
if not defined ISCC (
    echo Inno Setup is not installed: installing it with winget...
    winget install -e --id JRSoftware.InnoSetup --scope user --accept-package-agreements --accept-source-agreements
    for %%p in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
        if exist %%p set ISCC=%%p
    )
)
if not defined ISCC (
    echo Could not find Inno Setup 6. Install it from https://jrsoftware.org/isdl.php and run this again.
    goto :fail
)
%ISCC% /Qp /DAppVersion=%VERSION% packaging\installer.iss || goto :fail

echo.
echo Done: build\installer\TwitchShortsStudio-Setup-%VERSION%.exe
echo Give the .exe together with the .bin files next to it (they hold the models).
endlocal
exit /b 0

:fail
echo.
echo Build failed.
endlocal
exit /b 1
