"""PDF 渲染为 PNG 缓存（病历打印嵌入检查报告用）。

依赖 PyMuPDF（fitz）。失败时返回空 → 调用方降级为「显示文件名」。
缓存在 data/exam_pages_cache/{report_id}_p{page}_{mtime}.png。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("pdf_render")

CACHE_DIR = Path("data/exam_pages_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_pdf_page_count(pdf_path: str) -> int:
    """返回 PDF 页数；失败返回 0。"""
    try:
        import fitz  # PyMuPDF
        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as e:
        logger.warning("[pdf_render] open %s failed: %s", pdf_path, e)
        return 0


def render_pdf_page(pdf_path: str, page_index: int, report_id: int, dpi: int = 144) -> Path | None:
    """渲染指定页为 PNG，返回缓存路径。失败返回 None。"""
    src = Path(pdf_path)
    if not src.exists():
        return None
    try:
        mtime = int(src.stat().st_mtime)
        cache_file = CACHE_DIR / f"{report_id}_p{page_index}_{mtime}.png"
        if cache_file.exists():
            return cache_file
        import fitz  # PyMuPDF
        with fitz.open(pdf_path) as doc:
            if page_index < 0 or page_index >= doc.page_count:
                return None
            page = doc.load_page(page_index)
            # dpi 144 ≈ 2× 屏幕分辨率，打印清晰够用
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(cache_file))
        # 清理旧缓存（同 report_id 旧 mtime 的）
        for f in CACHE_DIR.glob(f"{report_id}_p{page_index}_*.png"):
            if f != cache_file:
                try:
                    f.unlink()
                except Exception:
                    pass
        return cache_file
    except Exception as e:
        logger.warning("[pdf_render] page %s of %s failed: %s", page_index, pdf_path, e)
        return None
