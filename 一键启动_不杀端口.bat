@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo   TNR backend - localhost 127.0.0.1:8765
echo   (no taskkill; stop old window with Ctrl+C if port busy)
echo ========================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe not found. Create venv and install deps first.
    pause
    exit /b 1
)

start "" cmd /c "ping -n 4 127.0.0.1 >nul && start http://127.0.0.1:8765/"

"%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
echo.
pause
