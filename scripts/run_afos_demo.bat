@echo off
rem AFOS Integration Test Runner and Dashboard Preview (Windows cmd launcher).
rem Runs scripts\run_afos_demo.py using the project's existing virtual
rem environment - never modifies any existing component.

setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo Could not find .venv\Scripts\python.exe - activate/create the project's virtual environment first.
    exit /b 1
)

".venv\Scripts\python.exe" "scripts\run_afos_demo.py"
set EXIT_CODE=%ERRORLEVEL%

endlocal & exit /b %EXIT_CODE%
