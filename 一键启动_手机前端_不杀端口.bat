@echo off
REM Same as 一键启动_手机联调_不杀端口.bat (ASCII-safe for cmd.exe)
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8765"

echo ========================================
echo   TNR backend - 0.0.0.0:%PORT% (phone same LAN)
echo   (no taskkill; stop old window with Ctrl+C if port busy)
echo ========================================
echo.
echo Open browser after a few seconds: http://127.0.0.1:%PORT%/
echo Phone: http://YOUR_PC_LAN_IP:%PORT%/
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe not found. Create venv and install deps first.
    pause
    exit /b 1
)

start "" cmd /c "ping -n 5 127.0.0.1 >nul && start http://127.0.0.1:%PORT%/"

"%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
echo.
pause
