@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   大风动物医院 TNR（本机 127.0.0.1，不自动结束端口进程）
echo ========================================
echo.
echo 说明：不会 taskkill 任何进程。若 8765 已被占用，请先在上一次运行的窗口按 Ctrl+C 结束服务。
echo.

if not exist ".venv\Scripts\python.exe" (
    echo 未找到 .venv，请先运行「一键启动.bat」完成初始化。
    pause
    exit /b 1
)

start "" cmd /c "ping -n 4 127.0.0.1 >nul && start http://127.0.0.1:8765/"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
echo.
pause
