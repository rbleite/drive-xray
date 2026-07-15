@echo off
REM Double-click wrapper for setup_shortcuts.ps1 — bypasses the default
REM PowerShell execution policy for this one script only (no system change).
REM Pass-through args:  setup_shortcuts.bat -Startup   |   -Remove   |
REM                     setup_shortcuts.bat -MediaCatalog "C:\path\to\media-catalog"
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_shortcuts.ps1" %*
pause
