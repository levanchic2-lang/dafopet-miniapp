"""
CI / 本地一键测试入口。
- 关掉 18001 上残留的进程
- 重置 _test/test.db 并播种 admin
- 起 uvicorn 子进程
- 跑 integration_test.py
- 关闭子进程并以退出码返回结果

用法： python _test/run_tests.py
"""
import os
import sys
import time
import socket
import subprocess
import signal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18001
DB_PATH = ROOT / "_test" / "test.db"
DB_URL = "sqlite:///./_test/test.db"
ENV = {**os.environ, "DATABASE_URL": DB_URL, "PYTHONIOENCODING": "utf-8"}


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def wait_port(port: int, timeout: float = 20.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.3)
    return False


def reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    # 在子进程里 init + 种 admin，免得污染主进程的 settings 缓存
    code = (
        "from app import models;"
        "from app.database import init_db, SessionLocal;"
        "init_db();"
        "from app.models import AdminUser;"
        "from passlib.hash import bcrypt;"
        "db=SessionLocal();"
        "db.add(AdminUser(username='admin', password_hash=bcrypt.hash('test123456'),"
        " role='superadmin', store=''));"
        "db.commit()"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], env=ENV, cwd=ROOT, capture_output=True, text=True
    )
    if r.returncode != 0:
        print("DB 初始化失败：", r.stderr)
        sys.exit(2)


def main():
    if port_in_use(PORT):
        print(f"[!] 端口 {PORT} 被占用，请手动关闭后重试。")
        sys.exit(3)

    print(f"[1/3] 重置 DB {DB_PATH}")
    reset_db()

    print(f"[2/3] 启动 uvicorn @ 127.0.0.1:{PORT}")
    log = open(ROOT / "_test" / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        env=ENV, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_port(PORT, 25.0):
            print("[x] 服务器启动超时，查 _test/server.log")
            log.close()
            print((ROOT / "_test" / "server.log").read_text(encoding="utf-8", errors="replace"))
            sys.exit(4)

        print(f"[3/3] 跑集成测试")
        result = subprocess.run(
            [sys.executable, str(ROOT / "_test" / "integration_test.py")],
            env=ENV, cwd=ROOT,
        )
        sys.exit(result.returncode)
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()


if __name__ == "__main__":
    main()
