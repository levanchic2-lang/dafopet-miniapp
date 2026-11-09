"""只读预演：把旧套餐拆成主品目、次数、赠品和价格。

不会写数据库。正式迁移在超管页面「套餐目录 → 迁移预演」里执行。

用法：
    python tools/package_migration_dry_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.database import SessionLocal
    from app.services.package_migration import build_report, render_text

    db = SessionLocal()
    try:
        report = build_report(db)
        print(render_text(report))
    except Exception as exc:
        message = str(exc).lower()
        if "no such column" in message or "no such table" in message:
            print("表结构还没有这次的新字段。请先部署这一版并启动一次，让启动迁移补上字段，然后再跑预演。")
            return 1
        raise
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
