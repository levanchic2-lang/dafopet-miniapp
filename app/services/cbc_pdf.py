"""BC-60R 血常规 PDF：原始屏幕照片仅后台留存，不进入客户报告。"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _e(value) -> str:
    return html.escape(str(value or ""))


def _fmt(value) -> str:
    if value is None:
        return "—"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _build_html(report, customer, pet, previous) -> str:
    rows = json.loads(report.results_json or "[]") or []
    old_rows = json.loads(previous.results_json or "[]") if previous else []
    old = {r.get("code"): r.get("value") for r in old_rows}
    species = {"cat": "猫", "dog": "犬"}.get(report.species, report.species or "—")
    clinic = "大风动物医院"
    if pet and pet.store:
        clinic = f"大风动物医院（{pet.store.replace('店', '分院')}）"
    body = []
    for r in rows:
        code, value = r.get("code", ""), r.get("value")
        low, high = r.get("range_low"), r.get("range_high")
        status = r.get("range_status", "na")
        mark = "↑" if status == "high" else ("↓" if status == "low" else "")
        cls = "abn" if mark else ""
        ref = "—" if low is None or high is None else f"{_fmt(low)}–{_fmt(high)}"
        prior = old.get(code)
        trend = ""
        if value is not None and prior is not None:
            trend = "↑" if value > prior else ("↓" if value < prior else "→")
        body.append(
            f'<tr class="{cls}"><td>{_e(code)}</td><td class="num">{_e(_fmt(value))} {mark}</td>'
            f'<td>{_e(r.get("unit"))}</td><td class="num">{_e(ref)}</td>'
            f'<td class="num">{_e(_fmt(prior)) if previous else "—"} {trend}</td></tr>'
        )
    tested = report.tested_at or (report.created_at.strftime("%Y-%m-%d %H:%M") if report.created_at else "")
    footer = f"{clinic} · CBC{report.id:06d} · AI 辅助分析须结合临床，由兽医解释"
    css = f"""
    @page {{ size:A4; margin:13mm 13mm 17mm; @bottom-center {{ content:'{footer}'; font-size:7pt; color:#555; }} }}
    *{{box-sizing:border-box}} body{{font-family:'Noto Serif CJK SC','Microsoft YaHei',serif;color:#171717;font-size:9.3pt;line-height:1.55}}
    h1{{text-align:center;font-size:17pt;letter-spacing:4px;margin:0}} .sub{{text-align:center;color:#666;font-size:8pt;letter-spacing:2px}}
    .rule{{border:0;border-top:1.5px solid #111;border-bottom:.5px solid #111;height:3px;margin:7pt 0 9pt}}
    .meta,.result{{width:100%;border-collapse:collapse}} .meta td{{border:.5px solid #bbb;padding:3pt 5pt}} .meta .k{{background:#f4f4f1;color:#555;width:14%}}
    h2{{font-size:10.5pt;letter-spacing:1px;border-bottom:1px solid #333;padding-bottom:2pt;margin:9pt 0 4pt}}
    .result th,.result td{{border:.5px solid #c7c7c7;padding:2.6pt 5pt}} .result th{{background:#f1f1ee}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
    .result tr.abn td{{font-weight:700;color:#7a2828;background:#fff8f6}} .text{{white-space:pre-wrap;line-height:1.7}}
    .note{{font-size:8pt;color:#666;margin-top:3pt}} .sig{{display:flex;justify-content:space-between;border-top:.5px solid #ccc;margin-top:12pt;padding-top:6pt}}
    """
    previous_label = previous.tested_at if previous else "无（本功能启用后首次）"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>
    <h1>{_e(clinic)}</h1><div class="sub">BC-60R · COMPLETE BLOOD COUNT REPORT</div><hr class="rule">
    <table class="meta">
      <tr><td class="k">客户</td><td>{_e(customer.name if customer else '—')}</td><td class="k">电话</td><td>{_e(customer.phone if customer else '—')}</td></tr>
      <tr><td class="k">宠物</td><td>{_e(pet.name if pet else '—')}</td><td class="k">物种/品种</td><td>{_e(species)} / {_e(pet.breed if pet else '—')}</td></tr>
      <tr><td class="k">设备</td><td>{_e(report.device_model)}</td><td class="k">样本号</td><td>{_e(report.sample_no or '—')}</td></tr>
      <tr><td class="k">检测时间</td><td>{_e(tested)}</td><td class="k">报告医师</td><td>{_e(report.vet_name or '—')}</td></tr>
    </table>
    <h2>血常规检测结果</h2>
    <table class="result"><thead><tr><th>项目</th><th>结果</th><th>单位</th><th>参考范围</th><th>最近一次</th></tr></thead><tbody>{''.join(body)}</tbody></table>
    <div class="note">最近一次：{_e(previous_label)}。↑/↓ 为相对参考范围或较上次变化；无参考范围的项目显示“—”。</div>
    <h2>AI 辅助分析（经医生审核）</h2><div class="text">{_e(report.ai_analysis or '—')}</div>
    <h2>医生结论与建议</h2><div class="text">{_e(report.doctor_conclusion or '—')}</div>
    <div class="note">提示：本报告须结合主诉、体格检查及其他检查综合判断；AI 内容不替代兽医临床诊断。</div>
    <div class="sig"><span>报告医师：{_e(report.vet_name or '________________')}</span><span>报告时间：{_e(tested)}</span></div>
    </body></html>"""


def generate_cbc_pdf(db: Session, report_id: int) -> tuple[Optional[str], Optional[str]]:
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:
        return None, f"PDF 组件不可用：{exc}"
    from app.models import CbcReport, Customer, ExamOrder, ExamReport, Pet
    report = db.get(CbcReport, report_id)
    if not report:
        return None, "报告不存在"
    customer = db.get(Customer, report.customer_id) if report.customer_id else None
    pet = db.get(Pet, report.pet_id) if report.pet_id else None
    previous = db.get(CbcReport, report.previous_report_id) if report.previous_report_id else None
    out_dir = Path("uploads") / "exam_reports" / str(report.exam_order_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"cbc_{report.id}.pdf"
    try:
        HTML(string=_build_html(report, customer, pet, previous)).write_pdf(str(out_path))
    except Exception as exc:
        return None, f"PDF 渲染失败：{type(exc).__name__}: {exc}"
    er = db.get(ExamReport, report.exam_report_id) if report.exam_report_id else None
    if not er:
        er = ExamReport(exam_order_id=report.exam_order_id)
        db.add(er); db.flush(); report.exam_report_id = er.id
    er.file_path = str(out_path)
    er.original_name = f"血常规报告_{pet.name if pet else report.id}.pdf"
    er.file_type = "pdf"
    er.item_label = report.item_label or "血常规"
    er.uploaded_by = report.operator or "系统"
    order = db.get(ExamOrder, report.exam_order_id)
    if order and order.status not in ("completed", "voided"):
        order.status = "completed"; order.updated_at = datetime.utcnow()
    db.commit()
    logger.info("[cbc_pdf] report=%s -> %s", report.id, out_path)
    return str(out_path), None
