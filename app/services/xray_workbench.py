"""X-ray workbench file preparation and annotation rendering.

The browser stores vector annotations in image-pixel coordinates. DICOM files
remain private under data/, while a de-identified 8-bit preview is exposed to
the authenticated report page through the existing uploads mount.
"""
from __future__ import annotations

import json
import math
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


_ROOT = Path.cwd().resolve()
_DATA_ROOT = (_ROOT / "data" / "xray_studies").resolve()
_PREVIEW_ROOT = (_ROOT / "uploads" / "xray_workbench").resolve()
_MAX_FILE_SIZE = 80 * 1024 * 1024
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_DICOM_EXTS = {".dcm", ".dicom"}


def _cleanup_stale_tmp(max_age_seconds: int = 24 * 60 * 60) -> None:
    """Remove abandoned uploads without touching finalized report studies."""
    cutoff = time.time() - max_age_seconds
    for root in (_DATA_ROOT / "tmp", _PREVIEW_ROOT / "tmp"):
        if not root.exists():
            continue
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue


def _safe_name(name: str) -> str:
    suffix = Path(name or "image").suffix.lower()
    stem = "".join(c for c in Path(name or "image").stem if c.isalnum() or c in "-_")[:60] or "image"
    return stem + suffix


def _is_dicom(data: bytes, suffix: str) -> bool:
    return suffix in _DICOM_EXTS or (len(data) > 132 and data[128:132] == b"DICM")


def _dicom_preview(data: bytes, target: Path) -> dict[str, Any]:
    try:
        import io
        import numpy as np
        import pydicom
        from pydicom.pixel_data_handlers.util import apply_voi_lut
    except ImportError as exc:
        raise ValueError("服务器尚未安装 DICOM 解析组件 pydicom") from exc

    try:
        ds = pydicom.dcmread(io.BytesIO(data), force=True)
        arr = ds.pixel_array
    except Exception as exc:
        raise ValueError(f"DICOM 无法读取像素数据：{exc}") from exc

    try:
        arr = apply_voi_lut(arr, ds)
    except Exception:
        arr = arr.astype("float32")
        arr = arr * float(getattr(ds, "RescaleSlope", 1) or 1) + float(getattr(ds, "RescaleIntercept", 0) or 0)
    arr = np.asarray(arr)
    if arr.ndim > 2:
        arr = arr[0]
    arr = arr.astype("float32")
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        raise ValueError("DICOM 影像没有有效像素")
    lo, hi = np.percentile(finite, (0.5, 99.5))
    if hi <= lo:
        lo, hi = float(finite.min()), float(finite.max())
    if hi <= lo:
        hi = lo + 1.0
    arr = np.clip((arr - lo) / (hi - lo), 0, 1)
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        arr = 1.0 - arr
    image = Image.fromarray((arr * 255).astype("uint8"), mode="L")
    image.save(target, format="PNG", optimize=True)

    spacing = getattr(ds, "PixelSpacing", None) or getattr(ds, "ImagerPixelSpacing", None)
    sx = sy = None
    if spacing and len(spacing) >= 2:
        try:
            sy, sx = float(spacing[0]), float(spacing[1])
        except (TypeError, ValueError):
            sx = sy = None
    return {
        "width": image.width,
        "height": image.height,
        "spacing_x_mm": sx,
        "spacing_y_mm": sy,
        "modality": str(getattr(ds, "Modality", "DX") or "DX")[:12],
        "projection": str(getattr(ds, "ViewPosition", "") or "")[:40],
    }


def prepare_study(filename: str, data: bytes) -> dict[str, Any]:
    _cleanup_stale_tmp()
    if not data:
        raise ValueError("文件为空")
    if len(data) > _MAX_FILE_SIZE:
        raise ValueError("单个影像不能超过 80MB")
    suffix = Path(filename or "").suffix.lower()
    is_dicom = _is_dicom(data, suffix)
    if not is_dicom and suffix not in _IMAGE_EXTS:
        raise ValueError("仅支持 DICOM、JPG、PNG、WEBP、BMP 或 TIFF")

    token = secrets.token_urlsafe(18)
    study_id = secrets.token_hex(8)
    private_dir = _DATA_ROOT / "tmp" / token
    preview_dir = _PREVIEW_ROOT / "tmp" / token
    private_dir.mkdir(parents=True, exist_ok=False)
    preview_dir.mkdir(parents=True, exist_ok=False)
    safe = _safe_name(filename)
    original = private_dir / (safe or ("image.dcm" if is_dicom else "image.png"))
    original.write_bytes(data)
    preview = preview_dir / "preview.png"

    if is_dicom:
        meta = _dicom_preview(data, preview)
        source_type = "dicom"
    else:
        try:
            with Image.open(original) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("L", "RGB"):
                    im = im.convert("RGB")
                im.save(preview, format="PNG", optimize=True)
                meta = {"width": im.width, "height": im.height, "spacing_x_mm": None,
                        "spacing_y_mm": None, "modality": "", "projection": ""}
        except Exception as exc:
            shutil.rmtree(private_dir, ignore_errors=True)
            shutil.rmtree(preview_dir, ignore_errors=True)
            raise ValueError(f"影像文件无法读取：{exc}") from exc
        source_type = "image"

    return {
        "id": study_id,
        "token": token,
        "filename": filename[:180],
        "source_type": source_type,
        "original_path": str(original.relative_to(_ROOT)).replace("\\", "/"),
        "preview_path": str(preview.relative_to(_ROOT / "uploads")).replace("\\", "/"),
        "annotated_path": "",
        "width": int(meta["width"]),
        "height": int(meta["height"]),
        "spacing_x_mm": meta.get("spacing_x_mm"),
        "spacing_y_mm": meta.get("spacing_y_mm"),
        "calibrated_mm_per_px": None,
        "modality": meta.get("modality", ""),
        "projection": meta.get("projection", ""),
        "annotations": [],
    }


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _font(size: int = 18):
    for candidate in ("C:/Windows/Fonts/msyh.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _pt(p: dict) -> tuple[float, float]:
    return float(p.get("x", 0)), float(p.get("y", 0))


def _render_annotations(preview: Path, output: Path, annotations: list[dict]) -> None:
    with Image.open(preview) as src:
        image = src.convert("RGB")
    draw = ImageDraw.Draw(image)
    scale = max(1.0, max(image.size) / 1600.0)
    width = max(2, round(3 * scale))
    font = _font(max(14, round(18 * scale)))
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        kind = str(ann.get("kind", ""))
        points = ann.get("points") or []
        color = str(ann.get("color") or "#d53a32")
        if not points:
            continue
        xy = [_pt(p) for p in points if isinstance(p, dict)]
        if not xy:
            continue
        if kind in {"length", "angle", "vhs", "vlas", "norberg", "coverage", "tpa", "arrow"}:
            if kind in {"vhs", "vlas", "coverage", "tpa"}:
                for idx in range(0, len(xy) - 1, 2):
                    draw.line([xy[idx], xy[idx + 1]], fill=color, width=width)
            else:
                draw.line(xy, fill=color, width=width, joint="curve")
            if kind == "arrow" and len(xy) >= 2:
                (x1, y1), (x2, y2) = xy[-2], xy[-1]
                angle = math.atan2(y2 - y1, x2 - x1)
                size = 14 * scale
                wings = [(x2 - size * math.cos(angle - .55), y2 - size * math.sin(angle - .55)),
                         (x2 - size * math.cos(angle + .55), y2 - size * math.sin(angle + .55))]
                draw.polygon([(x2, y2), *wings], fill=color)
        elif kind in {"rect", "ellipse"} and len(xy) >= 2:
            box = [(min(xy[0][0], xy[1][0]), min(xy[0][1], xy[1][1])),
                   (max(xy[0][0], xy[1][0]), max(xy[0][1], xy[1][1]))]
            (draw.rectangle if kind == "rect" else draw.ellipse)(box, outline=color, width=width)
        elif kind == "freehand" and len(xy) >= 2:
            draw.line(xy, fill=color, width=width, joint="curve")
        label = str(ann.get("label") or "").strip()
        if label:
            x, y = xy[-1]
            bbox = draw.textbbox((x + 5, y + 5), label, font=font)
            draw.rectangle((bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2), fill="#111111")
            draw.text((x + 5, y + 5), label, fill="#ffffff", font=font)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="JPEG", quality=92, optimize=True)


def finalize_studies(report_id: int, raw_json: str) -> list[dict[str, Any]]:
    try:
        studies = json.loads(raw_json or "[]")
    except Exception:
        return []
    if not isinstance(studies, list):
        return []
    final_private = _DATA_ROOT / str(report_id)
    final_preview = _PREVIEW_ROOT / str(report_id)
    final_private.mkdir(parents=True, exist_ok=True)
    final_preview.mkdir(parents=True, exist_ok=True)
    cleaned: list[dict[str, Any]] = []

    for raw in studies[:20]:
        if not isinstance(raw, dict):
            continue
        sid = "".join(c for c in str(raw.get("id", "")) if c.isalnum())[:32]
        if not sid:
            continue
        original = (_ROOT / str(raw.get("original_path", ""))).resolve()
        preview = (_ROOT / "uploads" / str(raw.get("preview_path", ""))).resolve()
        allowed_original = _inside(original, _DATA_ROOT / "tmp") or _inside(original, final_private)
        allowed_preview = _inside(preview, _PREVIEW_ROOT / "tmp") or _inside(preview, final_preview)
        if not allowed_original or not allowed_preview or not original.exists() or not preview.exists():
            continue
        original_dest = final_private / f"{sid}{original.suffix.lower()}"
        preview_dest = final_preview / f"{sid}.png"
        if original != original_dest:
            shutil.move(str(original), str(original_dest))
        if preview != preview_dest:
            shutil.move(str(preview), str(preview_dest))
        annotations = raw.get("annotations") if isinstance(raw.get("annotations"), list) else []
        annotated_dest = final_preview / f"{sid}_annotated.jpg"
        _render_annotations(preview_dest, annotated_dest, annotations)
        calibration = raw.get("calibrated_mm_per_px")
        try:
            calibration = float(calibration) if calibration not in (None, "") else None
        except (TypeError, ValueError):
            calibration = None
        cleaned.append({
            "id": sid,
            "filename": str(raw.get("filename", ""))[:180],
            "source_type": "dicom" if raw.get("source_type") == "dicom" else "image",
            "original_path": str(original_dest.relative_to(_ROOT)).replace("\\", "/"),
            "preview_path": str(preview_dest.relative_to(_ROOT / "uploads")).replace("\\", "/"),
            "annotated_path": str(annotated_dest.relative_to(_ROOT / "uploads")).replace("\\", "/"),
            "width": int(raw.get("width") or 0), "height": int(raw.get("height") or 0),
            "spacing_x_mm": raw.get("spacing_x_mm"), "spacing_y_mm": raw.get("spacing_y_mm"),
            "calibrated_mm_per_px": calibration,
            "modality": str(raw.get("modality", ""))[:12],
            "projection": str(raw.get("projection", ""))[:40],
            "annotations": annotations[:300],
        })
    return cleaned


def remove_report_files(report_id: int) -> None:
    shutil.rmtree(_DATA_ROOT / str(report_id), ignore_errors=True)
    shutil.rmtree(_PREVIEW_ROOT / str(report_id), ignore_errors=True)
