@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PORT=8765"

echo ========================================
echo   大风动物医院 TNR（手机联调 0.0.0.0，不自动结束端口进程）
echo ========================================
echo.
echo 说明：不会 taskkill 任何进程。若 %PORT% 已被占用，请先在上一次运行的窗口按 Ctrl+C 结束服务。
echo 本机首页（几秒后自动打开浏览器）：http://127.0.0.1:%PORT%/
echo 手机访问本机 IP 同端口，例如 http://192.168.x.x:%PORT%/
echo.

if not exist ".venv\Scripts\python.exe" (
    echo 未找到 .venv，请先运行「一键启动.bat」完成初始化。
    pause
    exit /b 1
)

rem 延迟几秒再打开首页，等 uvicorn 就绪（端口与下方命令一致，改 PORT 只改这一处即可）
start "" cmd /c "ping -n 5 127.0.0.1 >nul && start http://127.0.0.1:%PORT%/"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
echo.
pause
