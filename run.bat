@echo off
REM drive-xray launcher (Windows). Double-click or run from a terminal.
cd /d "%~dp0"
if exist ".venv\Scripts\streamlit.exe" (
    ".venv\Scripts\streamlit.exe" run app.py --server.port 8501
) else (
    python -m streamlit run app.py --server.port 8501
)
pause
