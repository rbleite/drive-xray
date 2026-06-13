@echo off
REM drive-xray launcher for Windows
REM Run this once to install, then again to launch.

cd /d "%~dp0"

IF NOT EXIST ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    echo Installing dependencies...
    .venv\Scripts\pip install -r requirements.txt
    echo.
    echo Setup complete. Launching drive-xray...
)

echo Starting drive-xray UI at http://localhost:8501
.venv\Scripts\streamlit run app.py
