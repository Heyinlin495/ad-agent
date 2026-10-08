@echo off
setlocal EnableExtensions

title Cross-border E-commerce Ad Agent - Launcher

rem Resolve script directory
cd /d "%~dp0"

set "VENV_PY=%~dp0backend\.venv\Scripts\python.exe"

echo ============================================================
echo   Cross-border E-commerce Ad Agent - One-click Launcher
echo ============================================================
echo.

rem ---- Check Python virtual environment ----
if not exist "%VENV_PY%" (
    echo [ERROR] Python virtual environment not found:
    echo         %VENV_PY%
    echo.
    echo Please create it first:
    echo         cd backend
    echo         py -3.11 -m venv .venv
    echo         .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem ---- Check .env ----
if not exist "%~dp0.env" (
    echo [WARN] .env file not found. Using mock/default settings.
    echo        Copy .env.example to .env for production configuration.
    echo.
)

rem ---- Start backend ----
echo [1/2] Starting backend  (http://localhost:8000) ...
start "AdAgent-Backend" /D "%~dp0backend" cmd /k ""%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

rem ---- Start web frontend ----
echo [2/2] Starting web frontend (http://localhost:8502) ...
start "AdAgent-Web" /D "%~dp0web" cmd /k ""%VENV_PY%" -m http.server 8502 --bind 0.0.0.0"

echo.
echo ============================================================
echo   Backend  API docs : http://localhost:8000/docs
echo   Web UI            : http://localhost:8502
echo.
echo   Run stop.bat to shut everything down.
echo ============================================================
echo.

endlocal
