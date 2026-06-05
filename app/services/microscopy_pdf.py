"""显微镜检查报告 PDF 生成（皮肤刮片 / 耳道分泌物 / 粪检 等手工出报告）
仿 consent_pdf.py：weasyprint 渲染本地图片用 base64 内联，避开 file:// 安全限制
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


PDF_CSS = """
@page { size: A4; margin: 16mm 14mm 14mm 14mm; }
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

.photo-grid { display: flex; flex-wrap: wrap; gap: 6pt; margin: 4pt 0 6pt; }
.photo-cell { width: 32%; box-sizing: border-box; page-break-inside: avoid; }
.photo-cell img {
  width: 100%; max-height: 140pt; object-fit: cover;
  border: 0.5px solid #999; display: block;
}

.find-table { width: 100%; border-collapse: collapse; font-size: 10pt; }
.find-table th, .find-table td { padding: 4pt 6pt; border: 0.5px solid #bbb; }
.find-table th { background: #f6f6f6; text-align: left; font-weight: 600; }
.find-grade-pos { color: #7a2828; font-weight: 700; }
.find-grade-neg { color: #888; }

.body-text { white-space: pre-wrap; font-size: 10pt; line-height: 1.75; padding: 4pt 0; }

.sig-row { margin-top: 18pt; padding-top: 8pt; border-top: 0.5px solid #ccc; display: flex; justify-content: space-between; font-size: 9.5pt; }
.sig-row .lbl { color: #666; }
.foot { position: fixed; bottom: 5mm; left: 14mm; right: 14mm; text-align: center; font-size: 7.5pt; color: #999; border-top: 0.3px solid #eee; padding-top: 2pt; }
"""


def _img_data_uri(path: Path, max_dim: int = 1000, quality: int = 78) -> Optional[str]:
    """读图 → Pillow 缩到 max_dim 边长 → JPEG 嵌入 base64
    缩图是关键：手机原图 5-8MB × 多张 = PDF 暴涨 24MB / 几百页
    """
    if not path.exists():
        return None
    try:
        from PIL import Image, ImageOps
    except Exception:
        # 没 Pillow 就直接 base64 原图（仍能嵌入）
        try:
            b = path.read_bytes()
        except Exception:
            return None
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "jpeg")
        return f"data:image/{mime};base64,{base64.b64encode(b).decode('ascii')}"

    try:
        with Image.open(path) as im:
            # 处理 EXIF 方向
            im = ImageOps.exif_transpose(im)
            # 转 RGB（PNG 带透明也兜底为白底）
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if im.mode == "P":
                    im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            # 缩到 max_dim
            im.thumbnail((max_dim, max_dim), Image.LANCZOS)
            import io as _io
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            data = buf.getvalue()
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return None


def _build_html(report, cust, pet, clinic_name: str) -> str:
    findings = []
    try:
        findings = json.loads(report.findings_json or "[]") or []
    except Exception:
        findings = []
    photos = []
    try:
        photos = json.loads(report.photos_json or "[]") or []
    except Exception:
        photos = []

    species_zh = {"cat": "猫", "dog": "犬"}.get((pet.species if pet else "") or "", (pet.species if pet else "") or "")
    pet_line = f"{pet.name or ''} · {species_zh}" if pet else "—"
    age_line = ""
    if pet and getattr(pet, "birth_date", None):
        age_line = pet.birth_date
    breed_line = (pet.breed if pet and pet.breed else "—")
    cust_name = (cust.name if cust else "—")
    cust_phone = (cust.phone if cust else "—")

    # 照片网格
    photo_html = ""
    if photos:
        cells = []
        for rel in photos:
            uri = _img_data_uri(Path("uploads") / rel)
            if uri:
                cells.append(f'<div class="photo-cell"><img src="{uri}"/></div>')
        if cells:
            photo_html = '<h2 class="sec">镜下照片</h2><div class="photo-grid">' + "".join(cells) + '</div>'

    # 检出物：按 cat 拆 3 段（镜检可见 / 寄生虫 / 病理可见）
    def _render_table(title: str, items: list, value_col_label: str) -> str:
        if not items:
            return ""
        rows = []
        for f in items:
            name = (f.get("name") or "").strip()
            grade = (f.get("grade") or "").strip()
            if not name:
                continue
            cls = "find-grade-neg" if grade in ("", "阴性", "-") else "find-grade-pos"
            rows.append(f'<tr><td>{name}</td><td class="{cls}">{grade or "—"}</td></tr>')
        if not rows:
            return ""
        return (
            f'<h2 class="sec">{title}</h2>'
            f'<table class="find-table"><thead><tr><th style="width:60%;">项目</th>'
            f'<th>{value_col_label}</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table>'
        )
    microbe_items   = [f for f in findings if f.get("cat", "microbe") == "microbe"]
    parasite_items  = [f for f in findings if f.get("cat") == "parasite"]
    pathology_items = [f for f in findings if f.get("cat") == "pathology"]
    find_html = (
        _render_table("镜下所见（半定量）", microbe_items, "等级")
        + _render_table("寄生虫定性",     parasite_items, "结果")
        + _render_table("病理可见",       pathology_items, "结果")
    )

    narrative = (report.narrative or "").strip()
    conclusion = (report.conclusion or "").strip()
    advice = (report.advice or "").strip()
    narrative_html = f'<h2 class="sec">镜下所见</h2><div class="body-text">{narrative}</div>' if narrative else ""
    conclusion_html = f'<h2 class="sec">结论</h2><div class="body-text">{conclusion}</div>' if conclusion else ""
    advice_html = f'<h2 class="sec">建议</h2><div class="body-text">{advice}</div>' if advice else ""

    created_at = report.created_at.strftime("%Y-%m-%d %H:%M") if report.created_at else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{PDF_CSS}</style></head>
<body>
<div class="clinic">{clinic_name}</div>
<div class="sub">DaFo Animal Hospital · Microscopy Report</div>
<hr class="rule"/>
<div class="title">{report.item_label or '显微镜检查报告'}</div>

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
    <td class="k">标本部位</td><td>{report.sample_site or '—'}</td>
    <td class="k">放大倍数</td><td>{report.magnification or '—'}</td>
  </tr>
  <tr>
    <td class="k">兽医</td><td>{report.vet_name or '—'}</td>
    <td class="k">报告时间</td><td>{created_at}</td>
  </tr>
</table>

{photo_html}
{find_html}
{narrative_html}
{conclusion_html}
{advice_html}

<div class="sig-row">
  <div><span class="lbl">报告医师：</span>{report.vet_name or '________________'}</div>
  <div><span class="lbl">报告日期：</span>{created_at}</div>
</div>

<div class="foot">本报告由 {clinic_name} 出具 · 单号 MR{report.id:06d}</div>
</body></html>
"""


def generate_microscopy_pdf(db: Session, report_id: int) -> tuple[Optional[str], Optional[str]]:
    """渲染 MicroscopyReport 为 PDF，写入 uploads/exam_reports/<order_id>/ 并 upsert ExamReport。
    返回 (PDF 相对路径, 错误信息)
    """
    try:
        from weasyprint import HTML
    except ImportError as e:
        return None, f"weasyprint 包未安装：{e}（pip install weasyprint）"
    except OSError as e:
        return None, f"weasyprint 系统库缺失：{e}（apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2）"

    from app.models import MicroscopyReport, ExamReport, Customer, Pet
    report = db.get(MicroscopyReport, report_id)
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
    fname = f"microscopy_{report.id}.pdf"
    out_path = out_dir / fname
    try:
        HTML(string=html_str).write_pdf(target=str(out_path))
    except Exception as e:
        return None, f"weasyprint 渲染异常：{type(e).__name__}: {e}"

    # upsert ExamReport 链接到 exam_order，复用现有"已上传报告"列表渲染
    operator = report.operator or "系统"
    if report.exam_report_id:
        er = db.get(ExamReport, report.exam_report_id)
        if er:
            er.file_path = str(out_path)
            er.original_name = f"显微镜报告_{report.item_label or report.id}.pdf"
            er.file_type = "pdf"
            er.item_label = report.item_label or ""
            er.uploaded_by = operator
        else:
            er = None
    else:
        er = None

    if er is None:
        er = ExamReport(
            exam_order_id=report.exam_order_id,
            file_path=str(out_path),
            original_name=f"显微镜报告_{report.item_label or report.id}.pdf",
            file_type="pdf",
            item_label=report.item_label or "",
            uploaded_by=operator,
        )
        db.add(er)
        db.flush()
        report.exam_report_id = er.id

    db.commit()
    logger.info("[microscopy_pdf] 生成 report=%s → %s", report.id, out_path)
    return str(out_path), None
