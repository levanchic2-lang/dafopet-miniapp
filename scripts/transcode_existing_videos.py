"""
将数据库中已有的视频文件批量转码为 H.264 MP4。
用法（在项目根目录、激活 venv 后执行）：
  python scripts/transcode_existing_videos.py
"""

import subprocess
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s")

from app.database import SessionLocal
from app.models import MediaFile

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".3gp"}


def transcode(src: Path) -> Path:
    # 输出到临时文件，避免与输入同名冲突（HEVC 文件扩展名也是 .mp4 的情况）
    tmp = src.with_name(src.stem + "_h264tmp.mp4")
    final = src.with_suffix(".mp4")
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                str(tmp),
            ],
            timeout=300,
            capture_output=True,
        )
        if r.returncode == 0 and tmp.exists():
            src.unlink(missing_ok=True)   # 删除原始 HEVC 文件
            tmp.rename(final)             # 临时文件改名为最终路径
            return final
        else:
            tmp.unlink(missing_ok=True)
            logging.warning(f"  ffmpeg 失败: {r.stderr.decode(errors='replace')[:500]}")
            return src
    except FileNotFoundError:
        logging.error("ffmpeg 未安装，请先执行：apt install ffmpeg")
        sys.exit(1)
    except Exception as e:
        logging.warning(f"  异常：{e}")
        return src


def main():
    db = SessionLocal()
    try:
        rows = db.query(MediaFile).all()
        video_rows = [m for m in rows if Path(m.stored_path).suffix.lower() in VIDEO_EXTS]
        logging.info(f"共找到 {len(video_rows)} 条视频记录")

        converted = 0
        skipped = 0
        for m in video_rows:
            src = Path(m.stored_path)
            if not src.exists():
                logging.warning(f"  [跳过] 文件不存在: {src}")
                skipped += 1
                continue

            # 检查是否已经是 H.264（用 ffprobe）
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=nk=1:nw=1", str(src)],
                capture_output=True, timeout=30,
            )
            codec = probe.stdout.decode().strip().lower()
            if codec == "h264" and src.suffix.lower() == ".mp4":
                logging.info(f"  [已是H264] {src.name}，跳过")
                skipped += 1
                continue

            logging.info(f"  [转码] {src.name}  (codec={codec or '未知'}) ...")
            dest = transcode(src)
            if dest != src or str(dest) != m.stored_path:
                m.stored_path = str(dest)
                converted += 1
                logging.info(f"    → {dest.name}")

        db.commit()
        logging.info(f"\n完成：转码 {converted} 个，跳过 {skipped} 个")
    finally:
        db.close()


if __name__ == "__main__":
    main()
