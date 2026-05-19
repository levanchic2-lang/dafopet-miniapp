"""
协议签署 PDF 生成：用 weasyprint 把 ConsentTask.snapshot_html + 签字图 渲染成 PDF。
失败不阻塞主流程（签字仍然成功，PDF 后台可补生成）。
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


PDF_CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: "PingFang SC", "Microsoft YaHei", "Source Han Sans CN",
               "Noto Sans CJK SC", "WenQuanYi Zen Hei", sans-serif;
  font-size: 11pt; color: #1a1a1a; line-height: 1.75;
}
.pdf-clinic { text-align: center; font-size: 18pt; letter-spacing: 4px; margin-bottom: 2pt; }
.pdf-sub { text-align: center; font-size: 9pt; color: #666; letter-spacing: 2px; }
.pdf-rule { border: 0; border-top: 1.5px solid #111; border-bottom: 0.5px solid #111; height: 3px; margin: 8pt 0 12pt; }
.pdf-meta { font-size: 9pt; color: #666; padding: 4pt 0 8pt; border-bottom: 0.5px solid #ddd; margin-bottom: 12pt; }
.pdf-meta b { color: #111; }
.pdf-title { text-align: center; font-size: 14pt; font-weight: 700; letter-spacing: 4px; margin: 8pt 0 12pt; }

.body-content h1, .body-content h2, .body-content h3 { font-weight: 700; }
.body-content h1 { font-size: 14pt; }
.body-content h2 { font-size: 12pt; }
.body-content h3 { font-size: 11pt; }
.body-content p, .body-content li { line-height: 1.85; margin: 6pt 0; }
.body-content ol, .body-content ul { padding-left: 22pt; }
.body-content blockquote {
  border-left: 3px solid #ccc; padding: 4pt 8pt; color: #555; margin: 8pt 0;
}

.sig-block {
  margin-top: 24pt; padding-top: 10pt; border-top: 1px solid #ccc;
}
.sig-row { display: flex; justify-content: space-between; gap: 24pt; margin-top: 10pt; }
.sig-col { flex: 1; font-size: 10pt; }
.sig-col .lbl { color: #666; font-size: 9pt; margin-bottom: 4pt; }
.sig-col img.sig { max-height: 60pt; max-width: 220pt; border: 0.5px solid #e0e0e0; padding: 3pt; background: #fff; }
.sig-meta { font-size: 8.5pt; color: #777; margin-top: 4pt; line-height: 1.6; }

.pdf-foot {
  position: fixed; bottom: 6mm; left: 16mm; right: 16mm;
  text-align: center; font-size: 7.5pt; color: #999; border-top: 0.3px solid #eee;
  padding-top: 3pt;
}
"""


def _build_html(task, cust, pet, doc_url_suffix: str, clinic_name: str) -> str:
    """组合最终 HTML 字符串供 weasyprint 渲染。"""
    sig_img_html = ""
    if task.signature_path:
        # weasyprint 用 file:// 直接读本地比 base64 更稳；构造绝对路径
        sig_abs = Path("uploads") / task.signature_path
        if sig_abs.exists():
            # 内联 base64 避免 weasyprint 的 file:// 安全限制
            b64 = base64.b64encode(sig_abs.read_bytes()).decode("ascii")
            sig_img_html = f'<img class="sig" src="data:image/png;base64,{b64}"/>'
    signed_at = task.signed_at.strftime("%Y-%m-%d %H:%M:%S") if task.signed_at else "—"

    cust_line = ""
    if cust:
        cust_line = f"客户：<b>{cust.name or ''}</b>　电话：{cust.phone or ''}"
    pet_line = ""
    if pet:
        species_zh = {"cat": "猫", "dog": "狗"}.get(pet.species or "", pet.species or "")
        pet_line = f"宠物：<b>{pet.name or ''}</b>{(' · ' + species_zh) if species_zh else ''}"
    initiated_at = task.initiated_at.strftime("%Y-%m-%d %H:%M") if task.initiated_at else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{PDF_CSS}</style></head>
<body>
<div class="pdf-clinic">{clinic_name or '大风动物医院'}</div>
<div class="pdf-sub">Da Feng Animal Hospital · Veterinary Consent</div>
<hr class="pdf-rule"/>
<div class="pdf-title">{task.title or '协议签署'}</div>
<div class="pdf-meta">
  {('<div>' + cust_line + '</div>') if cust_line else ''}
  {('<div>' + pet_line + '</div>') if pet_line else ''}
  <div>发起时间：{initiated_at} · 单号 CT{task.id:06d}</div>
</div>

<div class="body-content">{task.snapshot_html or ''}</div>

<div class="sig-block">
  <div class="sig-row">
    <div class="sig-col">
      <div class="lbl">签字（客户）</div>
      {sig_img_html or '<div style="color:#aaa;">（未签字）</div>'}
      <div class="sig-meta">
        签署时间：{signed_at}<br/>
        签署 IP：{task.signed_ip or '—'}
      </div>
    </div>
    <div class="sig-col" style="flex:.6;">
      <div class="lbl">医院</div>
      <div style="margin-top:14pt;border-bottom:1px solid #aaa;height:50pt;"></div>
      <div class="sig-meta">主治/审核医师：__________________</div>
    </div>
  </div>
</div>

<div class="pdf-foot">本协议由 {clinic_name or '大风动物医院'} 出具 · 签字数据加密保存 · {doc_url_suffix}</div>
</body></html>
"""


def generate_consent_pdf(db: Session, task_id: int) -> tuple[Optional[str], Optional[str]]:
    """渲染并保存 PDF。返回 (相对路径, 错误信息)；成功时错误为 None。
    成功时会 upsert ConsentDocument。
    """
    try:
        from weasyprint import HTML
    except ImportError as e:
        msg = f"weasyprint 包未安装：{e}（服务器跑 pip install weasyprint）"
        logger.warning("[consent_pdf] %s", msg)
        return None, msg
    except OSError as e:
        msg = f"weasyprint 系统库缺失：{e}（Linux: apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2）"
        logger.warning("[consent_pdf] %s", msg)
        return None, msg

    from app.models import ConsentTask, ConsentDocument, Customer, Pet
    task = db.get(ConsentTask, task_id)
    if not task:
        return None, "任务不存在"
    if task.status != "signed":
        return None, f"任务状态为 {task.status}，仅 signed 可生成 PDF"
    cust = db.get(Customer, task.customer_id) if task.customer_id else None
    pet = db.get(Pet, task.pet_id) if task.pet_id else None

    clinic_name = "大风动物医院"
    if pet and pet.store:
        clinic_name = f"大风动物医院（{pet.store.replace('店', '分院')}）"
    html_str = _build_html(task, cust, pet, "", clinic_name)

    out_dir = Path("uploads/consent_pdfs")
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"task_{task.id}.pdf"
    out_path = out_dir / fname
    try:
        HTML(string=html_str).write_pdf(target=str(out_path))
    except Exception as e:
        msg = f"weasyprint 渲染异常：{type(e).__name__}: {e}"
        logger.warning("[consent_pdf] 渲染失败 task=%s: %s", task.id, msg)
        return None, msg

    rel_path = f"consent_pdfs/{fname}"
    size = out_path.stat().st_size if out_path.exists() else 0
    doc = db.query(ConsentDocument).filter(ConsentDocument.task_id == task.id).first()
    if doc:
        doc.pdf_path = rel_path
        doc.pdf_size = size
        doc.title = task.title or ""
    else:
        db.add(ConsentDocument(
            task_id=task.id,
            customer_id=task.customer_id,
            pet_id=task.pet_id,
            visit_id=task.visit_id,
            pdf_path=rel_path,
            pdf_size=size,
            title=task.title or "",
        ))
    db.commit()
    logger.info("[consent_pdf] 生成 task=%s → %s (%d bytes)", task.id, rel_path, size)
    return rel_path, None
