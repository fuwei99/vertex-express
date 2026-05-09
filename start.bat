@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "APP_DIR=%SCRIPT_DIR%app"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "SEED_PYTHON_EXE=%SCRIPT_DIR%..\vertex2api\.venv\Scripts\python.exe"
set "REQ_FILE=%SCRIPT_DIR%requirements.txt"
set "ENV_FILE=%SCRIPT_DIR%.env"
set "HOST=127.0.0.1"
set "PORT=8050"

echo ========================================
echo   Vertex API Local Startup
echo ========================================
echo.

if not exist "%APP_DIR%\main.py" (
    echo [ERROR] Application entry file not found: "%APP_DIR%\main.py"
    exit /b 1
)

set "BASE_PYTHON="
if exist "%PYTHON_EXE%" (
    set "BASE_PYTHON=%PYTHON_EXE%"
) else (
    if exist "%SEED_PYTHON_EXE%" (
        set "BASE_PYTHON=%SEED_PYTHON_EXE%"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 set "BASE_PYTHON=python"
    )
)

if "%BASE_PYTHON%"=="" (
        echo [ERROR] Python was not found in PATH.
        echo [HINT] Install Python 3.11+ and make sure the python command is available.
        exit /b 1
)

echo [INFO] Checking Python version...
"%BASE_PYTHON%" -c "import sys; print('[INFO] Detected Python version: ' + sys.version.split()[0])"
if errorlevel 1 (
    echo [ERROR] Failed to run Python.
    exit /b 1
)

if not exist "%VENV_DIR%" (
    echo [INFO] Creating virtual environment...
    "%BASE_PYTHON%" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        exit /b 1
    )
)

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment Python was not found: "%PYTHON_EXE%"
    exit /b 1
)

if not exist "%REQ_FILE%" (
    echo [ERROR] Requirements file not found: "%REQ_FILE%"
    exit /b 1
)

echo [INFO] Ensuring pip is available...
"%PYTHON_EXE%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pip is not available in the virtual environment.
    exit /b 1
)

echo [INFO] Installing or updating dependencies...
"%PYTHON_EXE%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    exit /b 1
)

if exist "%ENV_FILE%" (
    echo [INFO] Loading environment variables from .env...
    for /f "usebackq tokens=* delims=" %%L in ("%ENV_FILE%") do (
        set "LINE=%%L"
        if defined LINE (
            if not "!LINE:~0,1!"=="#" (
                for /f "tokens=1* delims==" %%A in ("!LINE!") do (
                    if not "%%A"=="" set "%%A=%%B"
                )
            )
        )
    )
) else (
    echo [WARN] .env file not found. The server will rely on existing environment variables.
)

if "%API_KEY%"=="" (
    echo [ERROR] API_KEY is missing.
    echo [HINT] Add API_KEY to .env before starting the server.
    exit /b 1
)

if "%VERTEX_EXPRESS_API_KEY%"=="" (
    echo [ERROR] VERTEX_EXPRESS_API_KEY is missing.
    echo [HINT] Add one or more Vertex Express keys to .env before starting the server.
    exit /b 1
)

echo [INFO] Starting Vertex API on http://%HOST%:%PORT%
echo [INFO] Press Ctrl+C to stop the server.
echo.

pushd "%APP_DIR%"
"%PYTHON_EXE%" -m uvicorn main:app --host %HOST% --port %PORT%
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Vertex API exited with code %EXIT_CODE%.
)

exit /b %EXIT_CODE%
