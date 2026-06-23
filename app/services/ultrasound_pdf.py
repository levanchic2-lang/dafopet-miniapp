"""B超 / 超声检查报告 PDF 生成。
仿 microscopy_pdf.py：weasyprint 渲染，本地图片用 base64 内联避开 file:// 限制。
测量值是动态分组，逐组渲染表格。
"""
from __future__ import annotations

import base64
import html as _html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


PDF_CSS = """
@page {
  size: A4;
  margin: 16mm 14mm 18mm 14mm;
  @bottom-center {
    content: "__FOOTER_TEXT__";
    font-family: "Noto Serif CJK SC", "Noto Sans CJK SC", serif;
    font-size: 7.5pt; color: #999;
    border-top: 0.3px solid #eee; padding-top: 3pt;
    vertical-align: top; white-space: nowrap;
  }
}
* { box-sizing: border-box; }
body {
  font-family: "Noto Serif CJK SC", "Noto Sans CJK SC", "WenQuanYi Zen Hei",
               "Source Han Sans CN", "PingFang SC", "Microsoft YaHei", serif;
  font-size: 10.5pt; color: #1a1a1a; line-height: 1.7;
}
.clinic { text-align: center; font-size: 17pt; letter-spacing: 4px; margin-bottom: 1pt; }
.sub    { text-align: center; font-size: 8.5pt; color: #666; letter-spacing: 2px; }
.rule   { border: 0; border-top: 1.5px solid #111; border-bottom: 0.5px solid #111; height: 3px; margin: 8pt 0 12pt; }
.title  { text-align: center; font-size: 13pt; font-weight: 700; letter-spacing: 3px; margin: 6pt 0 12pt; }

.meta-table { width: 100%; border-collapse: collapse; font-size: 9.5pt; margin-bottom: 10pt; }
.meta-table td { padding: 3pt 6pt; border: 0.5px solid #bbb; }
.meta-table td.k { background: #f6f6f6; color: #555; width: 16%; }

h2.sec { font-size: 11pt; margin: 12pt 0 6pt; padding-bottom: 2pt; border-bottom: 1px solid #333; letter-spacing: 1px; }

.ms-group { margin: 6pt 0; page-break-inside: avoid; }
.ms-group-title { font-size: 9.5pt; font-weight: 700; color: #333; margin: 6pt 0 2pt; letter-spacing: .5px; }
.ms-table { width: 100%; border-collapse: collapse; font-size: 9.5pt; }
.ms-table td { padding: 3pt 6pt; border: 0.5px solid #ccc; }
.ms-table td.k { color: #444; width: 38%; }
.ms-table td.u { color: #777; width: 14%; text-align: right; }
.ms-table td.v { font-weight: 600; text-align: right; width: 18%; }

.photo-grid { display: flex; flex-wrap: wrap; gap: 6pt; margin: 4pt 0 6pt; }
.photo-cell { width: 32%; box-sizing: border-box; page-break-inside: avoid; }
.photo-cell img { width: 100%; max-height: 150pt; object-fit: cover; border: 0.5px solid #999; display: block; }

.body-text { white-space: pre-wrap; font-size: 10pt; line-height: 1.75; padding: 4pt 0; }

.sig-row { margin-top: 18pt; padding-top: 8pt; border-top: 0.5px solid #ccc; display: flex; justify-content: space-between; font-size: 9.5pt; }
.sig-row .lbl { color: #666; }
"""


def _img_data_uri(path: Path, max_dim: int = 1100, quality: int = 80) -> Optional[str]:
    if not path.exists():
        return None
    try:
        from PIL import Image, ImageOps
    except Exception:
        try:
            b = path.read_bytes()
        except Exception:
            return None
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "jpeg")
        return f"data:image/{mime};base64,{base64.b64encode(b).decode('ascii')}"
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if im.mode == "P":
                    im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((max_dim, max_dim), Image.LANCZOS)
            import io as _io
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            data = buf.getvalue()
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return None


def _esc(s) -> str:
    return _html.escape(str(s or ""))


_EXAM_TYPE_LABEL = {
    "cardiac": "心脏彩超报告",
    "abdominal": "腹部超声报告",
    "urogenital": "泌尿生殖超声报告",
    "general": "超声检查报告",
}


def _build_html(report, cust, pet, clinic_name: str) -> str:
    try:
        groups = json.loads(report.measurements_json or "[]") or []
    except Exception:
        groups = []
    try:
        photos = json.loads(report.photos_json or "[]") or []
    except Exception:
        photos = []

    species_zh = {"cat": "猫", "dog": "犬"}.get((pet.species if pet else "") or "", (pet.species if pet else "") or "")
    pet_line = f"{_esc(pet.name) if pet else ''} · {species_zh}" if pet else "—"
    breed_line = _esc(pet.breed) if pet and pet.breed else "—"
    cust_name = _esc(cust.name) if cust else "—"
    cust_phone = _esc(cust.phone) if cust else "—"

    # 动态测量分组
    ms_html = ""
    if groups:
        blocks = []
        for g in groups:
            rows = g.get("rows") or []
            if not rows:
                continue
            trs = []
            for r in rows:
                nm = _esc(r.get("name"))
                val = _esc(r.get("value"))
                unit = _esc(r.get("unit"))
                if not nm:
                    continue
                trs.append(f'<tr><td class="k">{nm}</td><td class="v">{val or "—"}</td><td class="u">{unit}</td></tr>')
            if not trs:
                continue
            gt = _esc(g.get("group")) or "测量"
            blocks.append(
                f'<div class="ms-group"><div class="ms-group-title">{gt}</div>'
                f'<table class="ms-table">{"".join(trs)}</table></div>'
            )
        if blocks:
            ms_html = '<h2 class="sec">超声测量数据</h2>' + "".join(blocks)

    # 照片
    photo_html = ""
    if photos:
        cells = []
        for rel in photos:
            uri = _img_data_uri(Path("uploads") / rel)
            if uri:
                cells.append(f'<div class="photo-cell"><img src="{uri}"/></div>')
        if cells:
            photo_html = '<h2 class="sec">超声影像</h2><div class="photo-grid">' + "".join(cells) + '</div>'

    findings = (report.findings or "").strip()
    conclusion = (report.conclusion or "").strip()
    advice = (report.advice or "").strip()
    findings_html = f'<h2 class="sec">超声所见</h2><div class="body-text">{_esc(findings)}</div>' if findings else ""
    conclusion_html = f'<h2 class="sec">超声提示</h2><div class="body-text">{_esc(conclusion)}</div>' if conclusion else ""
    advice_html = f'<h2 class="sec">建议</h2><div class="body-text">{_esc(advice)}</div>' if advice else ""

    created_at = report.created_at.strftime("%Y-%m-%d %H:%M") if report.created_at else ""
    title = _esc(report.item_label) or _EXAM_TYPE_LABEL.get(report.exam_type or "general", "超声检查报告")

    footer_text = f"本报告由 {clinic_name} 出具 · 单号 US{report.id:06d} · 仅供临床参考"
    css_filled = PDF_CSS.replace("__FOOTER_TEXT__", footer_text.replace('"', '\\"'))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{css_filled}</style></head>
<body>
<div class="clinic">{_esc(clinic_name)}</div>
<div class="sub">DaFo Animal Hospital · Ultrasound Report</div>
<hr class="rule"/>
<div class="title">{title}</div>

<table class="meta-table">
  <tr>
    <td class="k">客户</td><td>{cust_name}</td>
    <td class="k">电话</td><td>{cust_phone}</td>
  </tr>
  <tr>
    <td class="k">宠物</td><td>{pet_line}</td>
    <td class="k">品种</td><td>{breed_line}</td>
  </tr>
  <tr>
    <td class="k">设备</td><td>{_esc(report.device) or '—'}</td>
    <td class="k">兽医</td><td>{_esc(report.vet_name) or '—'}</td>
  </tr>
  <tr>
    <td class="k">检查项</td><td>{_esc(report.item_label) or '—'}</td>
    <td class="k">报告时间</td><td>{created_at}</td>
  </tr>
</table>

{ms_html}
{photo_html}
{findings_html}
{conclusion_html}
{advice_html}

<div class="sig-row">
  <div><span class="lbl">报告医师：</span>{_esc(report.vet_name) or '________________'}</div>
  <div><span class="lbl">报告日期：</span>{created_at}</div>
</div>

</body></html>
"""


def generate_ultrasound_pdf(db: Session, report_id: int) -> tuple[Optional[str], Optional[str]]:
    """渲染 UltrasoundReport 为 PDF，写入 uploads/exam_reports/<order_id>/ 并 upsert ExamReport。
    返回 (PDF 相对路径, 错误信息)"""
    try:
        from weasyprint import HTML
    except ImportError as e:
        return None, f"weasyprint 包未安装：{e}（pip install weasyprint）"
    except OSError as e:
        return None, f"weasyprint 系统库缺失：{e}（apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2）"

    from app.models import UltrasoundReport, ExamReport, Customer, Pet, ExamOrder
    report = db.get(UltrasoundReport, report_id)
    if not report:
        return None, "报告不存在"

    cust = db.get(Customer, report.customer_id) if report.customer_id else None
    pet = db.get(Pet, report.pet_id) if report.pet_id else None

    clinic_name = "大风动物医院"
    if pet and pet.store:
        clinic_name = f"大风动物医院（{pet.store.replace('店', '分院')}）"

    html_str = _build_html(report, cust, pet, clinic_name)

    out_dir = Path("uploads") / "exam_reports" / str(report.exam_order_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ultrasound_{report.id}.pdf"
    try:
        HTML(string=html_str).write_pdf(target=str(out_path))
    except Exception as e:
        return None, f"weasyprint 渲染异常：{type(e).__name__}: {e}"

    operator = report.operator or "系统"
    er = db.get(ExamReport, report.exam_report_id) if report.exam_report_id else None
    if er:
        er.file_path = str(out_path)
        er.original_name = f"B超报告_{report.item_label or report.id}.pdf"
        er.file_type = "pdf"
        er.item_label = report.item_label or ""
        er.uploaded_by = operator
    else:
        er = ExamReport(
            exam_order_id=report.exam_order_id,
            file_path=str(out_path),
            original_name=f"B超报告_{report.item_label or report.id}.pdf",
            file_type="pdf",
            item_label=report.item_label or "",
            uploaded_by=operator,
        )
        db.add(er)
        db.flush()
        report.exam_report_id = er.id

    _eo = db.get(ExamOrder, report.exam_order_id) if report.exam_order_id else None
    if _eo and _eo.status not in ("completed", "voided"):
        _eo.status = "completed"
        _eo.updated_at = datetime.utcnow()

    db.commit()
    logger.info("[ultrasound_pdf] 生成 report=%s → %s", report.id, out_path)
    return str(out_path), None
