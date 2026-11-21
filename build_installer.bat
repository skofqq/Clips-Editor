@echo off
rem Builds the Windows installer: build\installer\TwitchShortsStudio-Setup-<version>.exe
rem Needs Python 3 and internet on the first run (dependencies, FFmpeg, PyInstaller, Inno Setup).
setlocal
cd /d "%~dp0"

if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv || goto :fail
)
echo [1/5] Dependencies...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt "pyinstaller>=6.6" || goto :fail
copy /y requirements.txt .venv\requirements.installed >nul

echo [2/5] FFmpeg...
if not exist tools\ffmpeg\bin\ffmpeg.exe (
    .venv\Scripts\python tools\setup_ffmpeg.py || goto :fail
)

for /f "delims=" %%v in ('.venv\Scripts\python -c "from app.config import APP_VERSION; print(APP_VERSION)"') do set VERSION=%%v
echo Version %VERSION%

echo [3/5] Program files (PyInstaller)...
.venv\Scripts\pyinstaller --noconfirm --clean --distpath build\dist --workpath build\work packaging\twitch_shorts_studio.spec || goto :fail

echo [4/5] Self-test of the built program...
build\dist\TwitchShortsStudio\TwitchShortsStudio.exe --self-test build\self-test.txt
set SELFTEST=%errorlevel%
type build\self-test.txt
if not "%SELFTEST%"=="0" (
    echo The self-test failed, see above.
    goto :fail
)

echo [5/5] Installer (Inno Setup)...
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
endlocal
exit /b 0

:fail
echo.
echo Build failed.
endlocal
exit /b 1
