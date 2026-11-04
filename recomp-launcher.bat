@echo off
rem Recomp Launcher (Windows): bootstraps a local venv with PySide6
rem on first run, then opens the launcher. Games are launched from
rem their own folders exactly as on Linux; recording is Linux-only
rem (GPU Screen Recorder has no Windows version - the launcher says
rem so in Settings instead of pretending).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
)
.venv\Scripts\python -c "import PySide6" 2>nul
if errorlevel 1 .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app\main.py %*
