@echo off
REM Double-click wrapper for install.ps1 -- bypasses the default PowerShell
REM execution policy for this one script only (no system change).
REM Optional args, passed straight through:
REM   install.bat -Path "D:\apps"     install somewhere other than %USERPROFILE%\tools
REM   install.bat -SkipRustEngine     do not download dx.exe
REM   install.bat -Startup            also launch the apps at login
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
