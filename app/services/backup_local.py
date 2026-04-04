"""本地 SQLite + uploads 目录打包备份（zip）。"""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from app.config import settings


def sqlite_db_path() -> Path:
    u = (settings.database_url or "").strip()
    if not u.startswith("sqlite:///"):
        raise RuntimeError("当前仅支持 sqlite:/// 本地文件库的备份")
    return Path(u.replace("sqlite:///", "", 1)).resolve()


def backup_dir() -> Path:
    d = Path(settings.backup_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_sidecar_files(db: Path) -> list[Path]:
    out: list[Path] = []
    if db.exists():
        out.append(db)
    for suffix in ("-wal", "-shm"):
        p = db.parent / (db.name + suffix)
        if p.is_file():
            out.append(p)
    return out


def create_backup_zip() -> Path:
    """生成 tnr_backup_YYYYMMDD_HHMMSS.zip，内含 data/ 下库文件与 uploads/ 下全部文件。"""
    db = sqlite_db_path()
    out_dir = backup_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"tnr_backup_{ts}.zip"

    upload_root = Path(settings.upload_dir).resolve()

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fp in _db_sidecar_files(db):
            arc = "data/" + fp.name
            zf.write(fp, arcname=arc)

        if upload_root.is_dir():
            for f in upload_root.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(upload_root)
                    arc = "uploads/" + rel.as_posix()
                    zf.write(f, arcname=arc)

    return out_path


def list_backup_zips() -> list[dict]:
    """按修改时间倒序，元素：name, size, mtime_iso。"""
    d = backup_dir()
    rows: list[dict] = []
    for p in sorted(d.glob("tnr_backup_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        rows.append(
            {
                "name": p.name,
                "size": st.st_size,
                "mtime_iso": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return rows


def is_safe_backup_filename(name: str) -> bool:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    if not name.startswith("tnr_backup_") or not name.endswith(".zip"):
        return False
    # tnr_backup_YYYYMMDD_HHMMSS.zip
    core = name[len("tnr_backup_") : -len(".zip")]
    if len(core) != 15 or core[8] != "_":
        return False
    for i, c in enumerate(core):
        if i == 8:
            continue
        if not c.isdigit():
            return False
    return True
