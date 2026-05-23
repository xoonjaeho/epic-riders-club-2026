@echo off
setlocal
cd /d "%~dp0"
where uvicorn >nul 2>nul
if errorlevel 1 (
    echo [setup] installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [error] dependency install failed
        exit /b 1
    )
)
echo [run] http://127.0.0.1:8804
uvicorn main:app --host 127.0.0.1 --port 8804 %*
endlocal
