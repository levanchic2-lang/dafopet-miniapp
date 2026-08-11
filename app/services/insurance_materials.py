from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Customer,
    ExamOrder,
    ExamReport,
    InsuranceMaterialShare,
    InsuranceMaterialSnapshot,
    Invoice,
    Payment,
    Pet,
    Prescription,
    Visit,
    WeightRecord,
)

def _safe_name(text: str, fallback: str = "file") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (text or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or fallback)[:120]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _resolve_upload_src(src: str) -> Path:
    rel = src.split("?", 1)[0].lstrip("/")
    if rel.startswith("uploads/"):
        return Path(rel)
    return Path(settings.upload_dir) / rel


def _rewrite_local_sources(html: str, db: Session) -> str:
    """把打印 HTML 里的后台/上传地址改成本地 file://，供 WeasyPrint 固定快照。"""

    def repl_exam_page(m: re.Match) -> str:
        report_id = int(m.group(1))
        page = int(m.group(2))
        rpt = db.get(ExamReport, report_id)
        if not rpt:
            return m.group(0)
        p = Path(rpt.file_path or "")
        if not p.exists():
            return m.group(0)
        if (rpt.file_type or "").lower() == "pdf":
            try:
                from app.services.pdf_render import render_pdf_page

                out = render_pdf_page(str(p), page, rpt.id)
                if out and out.exists():
                    return f'src="{_file_uri(out)}"'
            except Exception:
                return m.group(0)
        return f'src="{_file_uri(p)}"'

    html = re.sub(
        r'src="/admin/exam-reports/(\d+)/page-image\?page=(\d+)"',
        repl_exam_page,
        html,
    )

    def repl_upload(m: re.Match) -> str:
        p = _resolve_upload_src(m.group(1))
        if p.exists():
            return f'src="{_file_uri(p)}"'
        return m.group(0)

    html = re.sub(r'src="/uploads/([^"]+)"', repl_upload, html)
    return html


def _render_pdf(templates, template_name: str, context: dict, out_path: Path, db: Session) -> None:
    try:
        from weasyprint import HTML
    except Exception as e:
        raise RuntimeError(f"WeasyPrint 不可用：{e}") from e

    html = templates.env.get_template(template_name).render(**context)
    html = _rewrite_local_sources(html, db)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(Path.cwd().resolve())).write_pdf(target=str(out_path))


def _pet_age(pet: Pet | None) -> str:
    if not pet or not pet.birthday_estimate:
        return ""
    try:
        from datetime import date as _date

        parts = pet.birthday_estimate.split("-")
        by = int(parts[0])
        bm = int(parts[1]) if len(parts) > 1 else 1
        today = _date.today()
        years = today.year - by - (1 if (today.month, 1) < (bm, 1) else 0)
        if years > 0:
            return f"{years} 岁"
        return f"{max(0, (today.year - by) * 12 + (today.month - bm))} 个月"
    except Exception:
        return pet.birthday_estimate or ""


def _pet_weight(db: Session, pet: Pet | None) -> float:
    if not pet:
        return 0.0
    last_w = (
        db.query(WeightRecord)
        .filter(WeightRecord.pet_id == pet.id)
        .order_by(WeightRecord.record_date.desc(), WeightRecord.id.desc())
        .first()
    )
    return float(last_w.weight_kg or 0) if last_w else 0.0


def _add_manifest_file(files: list[dict], kind: str, label: str, path: Path, base_dir: Path) -> None:
    if not path.exists():
        return
    rel = path.resolve().relative_to(base_dir.resolve()).as_posix()
    files.append(
        {
            "kind": kind,
            "label": label,
            "filename": path.name,
            "path": rel,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
    )


def _pdf_to_jpegs(
    pdf_path: Path,
    image_dir: Path,
    stem: str,
    files: list[dict],
    base_dir: Path,
    source_kind: str,
    source_label: str,
    dpi: int = 150,
) -> None:
    """把一份固定 PDF 快照逐页转成保险平台通用的 JPG 文件。"""
    try:
        import fitz
        from PIL import Image
    except Exception as e:
        raise RuntimeError(f"生成保险图片版需要 PyMuPDF 和 Pillow：{e}") from e

    image_dir.mkdir(parents=True, exist_ok=True)
    try:
        with fitz.open(str(pdf_path)) as doc:
            if doc.page_count < 1:
                raise RuntimeError("PDF 没有可转换页面")
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)
                pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
                dst = image_dir / f"{_safe_name(stem)}_第{page_index + 1:02d}页.jpg"
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                image.save(dst, "JPEG", quality=88, optimize=True, progressive=True)
                _add_manifest_file(
                    files,
                    f"{source_kind}_image",
                    f"{source_label} · 第 {page_index + 1} 页（图片版）",
                    dst,
                    base_dir,
                )
    except Exception as e:
        raise RuntimeError(f"PDF 转图片失败：{pdf_path.name}：{e}") from e


def _image_to_jpeg(
    src: Path,
    image_dir: Path,
    stem: str,
    files: list[dict],
    base_dir: Path,
    source_kind: str,
    source_label: str,
) -> None:
    """把原始图片附件归一为 RGB JPG，便于保险平台上传。"""
    try:
        from PIL import Image, ImageOps

        image_dir.mkdir(parents=True, exist_ok=True)
        dst = image_dir / f"{_safe_name(stem)}.jpg"
        with Image.open(src) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2400, 2400))
            image.save(dst, "JPEG", quality=88, optimize=True, progressive=True)
        _add_manifest_file(files, f"{source_kind}_image", f"{source_label}（图片版）", dst, base_dir)
    except Exception as e:
        raise RuntimeError(f"检查附件转图片失败：{src.name}：{e}") from e


def _copy_original_exam_files(
    db: Session,
    order: ExamOrder,
    out_dir: Path,
    image_dir: Path,
    files: list[dict],
) -> None:
    for idx, rpt in enumerate(order.reports or [], start=1):
        src = Path(rpt.file_path or "")
        if not src.exists():
            continue
        ext = src.suffix or (".pdf" if (rpt.file_type or "").lower() == "pdf" else ".jpg")
        label = rpt.item_label or rpt.original_name or f"附件{idx}"
        dst = out_dir / f"原始检查附件_EX{order.id:06d}_{idx}_{_safe_name(label)}{ext}"
        shutil.copy2(src, dst)
        source_label = f"检查原始附件 EX{order.id:06d} · {label}"
        _add_manifest_file(files, "exam_attachment", source_label, dst, out_dir)
        image_stem = f"原始检查附件_EX{order.id:06d}_{idx}_{_safe_name(label)}"
        if (rpt.file_type or "").lower() == "pdf" or src.suffix.lower() == ".pdf":
            _pdf_to_jpegs(dst, image_dir, image_stem, files, out_dir, "exam_attachment", source_label)
        else:
            _image_to_jpeg(dst, image_dir, image_stem, files, out_dir, "exam_attachment", source_label)


def _make_zip(out_dir: Path, files: list[dict], title: str) -> Path:
    zip_path = out_dir / f"{_safe_name(title, '保险材料包')}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in files:
            p = out_dir / item["path"]
            if p.exists():
                zf.write(p, arcname=item["path"])
    return zip_path


def _make_image_zip(out_dir: Path, files: list[dict]) -> Path:
    zip_path = out_dir / "保险材料_图片版.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in files:
            if not str(item.get("kind") or "").endswith("_image"):
                continue
            p = out_dir / str(item.get("path") or "")
            if p.exists():
                zf.write(p, arcname=p.name)
    return zip_path


def snapshot_image_files(snapshot: InsuranceMaterialSnapshot) -> list[dict]:
    return [row for row in load_snapshot_files(snapshot) if str(row.get("kind") or "").endswith("_image")]


def generate_insurance_material_snapshot(
    *,
    db: Session,
    templates,
    visit_id: int,
    generated_by: str,
    visit_type_zh: dict,
    inv_status_zh: dict,
    inv_pay_zh: dict,
    detect_report_style: Callable[[list], dict],
    print_clinic_store: Callable[[Visit | None, Pet | None], str],
    payment_balance_hints: Callable[[Session, list[Payment]], dict[int, str]],
    note: str = "",
) -> tuple[InsuranceMaterialShare, InsuranceMaterialSnapshot]:
    visit = db.get(Visit, visit_id)
    if not visit:
        raise ValueError("就诊记录不存在")
    cust = db.get(Customer, visit.customer_id) if visit.customer_id else None
    pet = db.get(Pet, visit.pet_id) if visit.pet_id else None

    share = (
        db.query(InsuranceMaterialShare)
        .filter(InsuranceMaterialShare.visit_id == visit.id, InsuranceMaterialShare.status == "active")
        .order_by(InsuranceMaterialShare.id.desc())
        .first()
    )
    if not share:
        share = InsuranceMaterialShare(
            token=secrets.token_urlsafe(24),
            customer_id=visit.customer_id,
            pet_id=visit.pet_id,
            visit_id=visit.id,
            title=f"保险材料包 · {pet.name if pet else '宠物'} · 病历#{visit.id}",
            store=(visit.store or (pet.store if pet else "") or ""),
            expires_at=datetime.utcnow() + timedelta(days=30),
            created_by=generated_by,
            notes="",
        )
        db.add(share)
        db.flush()

    max_ver = (
        db.query(func.max(InsuranceMaterialSnapshot.version))
        .filter(InsuranceMaterialSnapshot.share_id == share.id)
        .scalar()
        or 0
    )
    version = int(max_ver) + 1
    title = share.title or f"保险材料包 · 病历#{visit.id}"
    out_dir = Path(settings.upload_dir) / "insurance_materials" / str(share.id) / f"v{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = out_dir / "图片版"
    files: list[dict] = []

    prescriptions = (
        db.query(Prescription)
        .filter(Prescription.visit_id == visit.id)
        .order_by(Prescription.id.asc())
        .all()
    )
    exam_orders = (
        db.query(ExamOrder)
        .filter(ExamOrder.visit_id == visit.id, ExamOrder.status != "voided")
        .order_by(ExamOrder.id.asc())
        .all()
    )
    from app.services.pdf_render import get_pdf_page_count

    for eo in exam_orders:
        try:
            eo._items_parsed = json.loads(eo.items_json or "[]")
        except Exception:
            eo._items_parsed = []
        for rpt in eo.reports:
            if (rpt.file_type or "").lower() == "pdf":
                cnt = get_pdf_page_count(rpt.file_path)
                rpt.page_count = min(cnt, 5) if cnt else 0
            else:
                rpt.page_count = 1

    clinic_name_zh = "大风动物医院"
    clinic_name_en = "DaFo Animal Hospital"
    pcs = print_clinic_store(visit, pet)
    if pcs:
        clinic_name_zh = f"大风动物医院（{pcs.replace('店', '分院')}）"
        clinic_name_en = f"DaFo Animal Hospital · {pcs.replace('店', '')}"

    visit_pdf = out_dir / f"病历_MR{visit.id:06d}_{_safe_name(pet.name if pet else '宠物')}.pdf"
    _render_pdf(
        templates,
        "admin_visit_print.html",
        {
            "visit": visit,
            "cust": cust,
            "pet": pet,
            "prescriptions": prescriptions,
            "exam_orders": exam_orders,
            "pet_weight": _pet_weight(db, pet),
            "pet_age": _pet_age(pet),
            "clinic_name_zh": clinic_name_zh,
            "clinic_name_en": clinic_name_en,
            "visit_type_zh": visit_type_zh,
        },
        visit_pdf,
        db,
    )
    _add_manifest_file(files, "medical_record", "病历（含 SOAP、处方、检查概要）", visit_pdf, out_dir)
    _pdf_to_jpegs(
        visit_pdf, image_dir, visit_pdf.stem, files, out_dir,
        "medical_record", "病历（含 SOAP、处方、检查概要）",
    )

    invoices = (
        db.query(Invoice)
        .filter(Invoice.visit_id == visit.id)
        .order_by(Invoice.id.asc())
        .all()
    )
    for inv in invoices:
        payments = (
            db.query(Payment)
            .filter(Payment.invoice_id == inv.id)
            .order_by(Payment.id.asc())
            .all()
        )
        rcpt_store = (visit.store or "") or (inv.store or "") or (pet.store if pet else "")
        inv_clinic_zh = "大风动物医院"
        inv_clinic_en = "DaFo Animal Hospital"
        if rcpt_store:
            inv_clinic_zh = f"大风动物医院（{rcpt_store.replace('店', '分院')}）"
            inv_clinic_en = f"DaFo Animal Hospital · {rcpt_store.replace('店', '')}"
        inv_pdf = out_dir / f"收费单_{_safe_name(inv.invoice_no or ('INV%06d' % inv.id))}.pdf"
        _render_pdf(
            templates,
            "admin_invoice_print.html",
            {
                "inv": inv,
                "cust": cust,
                "pet": pet,
                "visit": visit,
                "payments": payments,
                "payment_balance_hints": payment_balance_hints(db, payments),
                "inv_status_zh": inv_status_zh,
                "inv_pay_zh": inv_pay_zh,
                "clinic_name_zh": inv_clinic_zh,
                "clinic_name_en": inv_clinic_en,
            },
            inv_pdf,
            db,
        )
        _add_manifest_file(files, "invoice", f"收费单 {inv.invoice_no or inv.id}", inv_pdf, out_dir)
        _pdf_to_jpegs(
            inv_pdf, image_dir, inv_pdf.stem, files, out_dir,
            "invoice", f"收费单 {inv.invoice_no or inv.id}",
        )

    for order in exam_orders:
        items = getattr(order, "_items_parsed", [])
        style = detect_report_style(items)
        image_reports = [r for r in order.reports if (r.file_type or "image").lower() != "pdf"]
        pdf_reports = [r for r in order.reports if (r.file_type or "").lower() == "pdf"]
        exam_pdf = out_dir / f"检查报告_EX{order.id:06d}.pdf"
        _render_pdf(
            templates,
            "admin_exam_print.html",
            {
                "order": order,
                "visit": visit,
                "cust": cust,
                "pet": pet,
                "items": items,
                "image_reports": image_reports,
                "pdf_reports": pdf_reports,
                "report_style": style,
                "clinic_name_zh": clinic_name_zh,
            },
            exam_pdf,
            db,
        )
        _add_manifest_file(files, "exam_report", f"检查报告 EX{order.id:06d}", exam_pdf, out_dir)
        _pdf_to_jpegs(
            exam_pdf, image_dir, exam_pdf.stem, files, out_dir,
            "exam_report", f"检查报告 EX{order.id:06d}",
        )
        _copy_original_exam_files(db, order, out_dir, image_dir, files)

    zip_path = _make_zip(out_dir, files, f"{title}_v{version}")
    image_zip_path = _make_image_zip(out_dir, files)
    if not snapshot_image_files_from_rows(files):
        raise RuntimeError("保险材料图片版未生成任何图片")
    if not image_zip_path.exists() or image_zip_path.stat().st_size <= 0:
        raise RuntimeError("保险材料图片版 ZIP 生成失败")
    rel_zip = zip_path.relative_to(Path(settings.upload_dir)).as_posix()

    snap = InsuranceMaterialSnapshot(
        share_id=share.id,
        version=version,
        manifest_json=json.dumps(files, ensure_ascii=False),
        zip_path=rel_zip,
        zip_size=zip_path.stat().st_size if zip_path.exists() else 0,
        zip_sha256=_sha256(zip_path) if zip_path.exists() else "",
        file_count=len(files),
        generated_by=generated_by,
        note=(note or "").strip(),
    )
    share.updated_at = datetime.utcnow()
    db.add(snap)
    db.commit()
    db.refresh(share)
    db.refresh(snap)
    return share, snap


def load_snapshot_files(snapshot: InsuranceMaterialSnapshot) -> list[dict]:
    try:
        rows = json.loads(snapshot.manifest_json or "[]")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def snapshot_image_files_from_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("kind") or "").endswith("_image")]
