@echo off
rem Puts build\installer\TwitchShortsStudio-Update-<version>.exe on GitHub Releases,
rem where installed copies find it (Help - Check for Updates).
rem   publish_release.bat          the update package only
rem   publish_release.bat --full   also the full setup (.exe + .bin files)
cd /d "%~dp0"
.venv\Scripts\python tools\publish_release.py %*
