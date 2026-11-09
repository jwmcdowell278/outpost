@echo off
rem Starts Outpost on Windows (set it up first with deploy\windows\setup.ps1).
rem Extra options are passed through, e.g.: run-outpost.cmd --channel ops -v
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Outpost isn't set up yet - run: powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1
    exit /b 1
)
".venv\Scripts\python.exe" scripts\run_client.py --config config\outpost.yaml %*
