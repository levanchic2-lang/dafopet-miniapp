"""
麻醉监护表 PDF 导出：WeasyPrint 渲染表头 + 时间×指标表格 + 服务端 SVG 趋势曲线。
WeasyPrint 不执行 JS，所以趋势图用纯 Python 拼 SVG（HR/RR/SpO₂ 三条线），
作为 inline 图嵌入 HTML，再整页渲染成 PDF。给主人或麻醉师看。
失败不阻塞主流程（只回错误信息，前端提示）。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_DEPTH_ZH = {"light": "偏浅", "adequate": "适宜", "deep": "偏深"}
_SPECIES_ZH = {"cat": "猫", "dog": "犬"}


def _esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# 趋势曲线三条线（避开饱和蓝，用 3 暗警示色，打印可辨）
_SERIES = [
    ("HR", "#7a2828", "hr"),
    ("RR", "#1d4d3a", "rr"),
    ("SpO₂", "#6b4423", "spo2"),
]

PDF_CSS = """
@page { size: A4 landscape; margin: 12mm 12mm 14mm 12mm; }
* { box-sizing: border-box; }
body {
  font-family: "PingFang SC", "Microsoft YaHei", "Source Han Sans CN",
               "Noto Sans CJK SC", "WenQuanYi Zen Hei", sans-serif;
  font-size: 9pt; color: #1a1a1a; line-height: 1.5;
}
.clinic { text-align: center; font-size: 16pt; letter-spacing: 4px; margin-bottom: 1pt; }
.sub { text-align: center; font-size: 8pt; color: #777; letter-spacing: 2px; }
.rule { border: 0; border-top: 1.4px solid #111; border-bottom: 0.5px solid #111; height: 3px; margin: 6pt 0 8pt; }
.title { text-align: center; font-size: 13pt; font-weight: 700; letter-spacing: 4px; margin: 4pt 0 8pt; }

.hd { width: 100%; border-collapse: collapse; margin-bottom: 8pt; }
.hd td { padding: 2.5pt 6pt; font-size: 9pt; border-bottom: 0.5px solid #ddd; }
.hd td.k { color: #777; width: 9%; white-space: nowrap; }
.hd td.v { color: #111; font-weight: 600; width: 16%; }

.chart-wrap { margin: 4pt 0 10pt; }
.chart-cap { font-size: 8pt; color: #777; letter-spacing: 1px; margin-bottom: 2pt; }

table.grid { width: 100%; border-collapse: collapse; }
table.grid th, table.grid td {
  border: 0.5px solid #c8c4bc; padding: 3pt 4pt; text-align: center;
  font-variant-numeric: tabular-nums lining-nums;
}
table.grid th { background: #f4f1ec; font-weight: 600; font-size: 8.5pt; }
table.grid td { font-size: 9pt; }
table.grid td.ev { text-align: left; font-size: 8pt; color: #555; }
table.grid td.dim { color: #bbb; }
td.bad { color: #7a2828; font-weight: 700; }
td.warn { color: #6b4423; font-weight: 700; }

.foot {
  margin-top: 10pt; padding-top: 4pt; border-top: 0.5px solid #ddd;
  font-size: 7.5pt; color: #999; display: flex; justify-content: space-between;
}
.sign { margin-top: 12pt; font-size: 9pt; color: #555; }
.sign span { display: inline-block; min-width: 200pt; border-bottom: 0.5px solid #999; margin-left: 6pt; }
"""


def _fmt_t(dt: datetime) -> str:
    try:
        return dt.strftime("%H:%M")
    except Exception:
        return "—"


def _svg_trend(entries) -> str:
    """HR/RR/SpO₂ 随时间趋势的纯 SVG 折线图。空值（0）断线。"""
    pts = [e for e in entries if e.recorded_at]
    if len(pts) < 2:
        return ""
    t0 = pts[0].recorded_at
    times = [max(0.0, (e.recorded_at - t0).total_seconds() / 60.0) for e in pts]
    tmax = max(times) or 1.0

    W, H = 900, 250
    ml, mr, mt, mb = 40, 96, 14, 28
    pw, ph = W - ml - mr, H - mt - mb

    # y 轴上限：220 起步，遇到更高值再抬
    # 只取有限正数；异常大/非数值会被忽略，且整体封顶，避免下方网格循环被撑爆。
    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) and f > 0 else None
    observed = [n for e in pts for _, _, k in _SERIES
                if (n := _num(getattr(e, k, None))) is not None]
    raw_max = max(observed) if observed else 0.0
    ymax = max(220.0, min(raw_max * 1.05, 2000.0))   # 封顶 2000，防脏数据无限网格

    def X(i: int) -> float:
        return ml + (times[i] / tmax) * pw

    def Y(v: float) -> float:
        return mt + ph - (min(v, ymax) / ymax) * ph

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">']
    # 网格 + y 标签：动态步长，保证 ~5-8 条线，绝不无限（step 始终 >0 且行数封顶）
    step = 50
    while ymax / step > 12:
        step *= 2
    gv = 0
    _guard = 0
    while gv <= ymax and _guard < 64:
        gy = Y(gv)
        out.append(f'<line x1="{ml}" y1="{gy:.1f}" x2="{ml + pw}" y2="{gy:.1f}" stroke="#e2ddd4" stroke-width="0.6"/>')
        out.append(f'<text x="{ml - 6}" y="{gy + 3:.1f}" font-size="10" fill="#8a8a8a" text-anchor="end">{gv}</text>')
        gv += step
        _guard += 1
    # x 轴时间刻度（最多 7 个）
    n = len(pts)
    tick_idx = sorted(set([round(i * (n - 1) / 6) for i in range(7)]))
    base_y = mt + ph
    for i in tick_idx:
        tx = X(i)
        out.append(f'<line x1="{tx:.1f}" y1="{base_y}" x2="{tx:.1f}" y2="{base_y + 4}" stroke="#8a8a8a" stroke-width="0.6"/>')
        out.append(f'<text x="{tx:.1f}" y="{base_y + 16}" font-size="9" fill="#8a8a8a" text-anchor="middle">{_fmt_t(pts[i].recorded_at)}</text>')
    # 折线 + 点
    for si, (label, color, key) in enumerate(_SERIES):
        d, started = [], False
        for i, e in enumerate(pts):
            v = getattr(e, key)
            if v and v > 0:
                d.append(f'{"M" if not started else "L"}{X(i):.1f} {Y(v):.1f}')
                started = True
            else:
                started = False  # 断线
        if d:
            out.append(f'<path d="{" ".join(d)}" fill="none" stroke="{color}" stroke-width="1.6"/>')
        for i, e in enumerate(pts):
            v = getattr(e, key)
            if v and v > 0:
                out.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="2" fill="{color}"/>')
        # 图例
        ly = mt + 6 + si * 18
        out.append(f'<line x1="{ml + pw + 14}" y1="{ly}" x2="{ml + pw + 32}" y2="{ly}" stroke="{color}" stroke-width="2.2"/>')
        out.append(f'<text x="{ml + pw + 36}" y="{ly + 4}" font-size="10" fill="#1a1a1a">{label}</text>')
    out.append("</svg>")
    return "".join(out)


def _cell(v, unit_ok=True) -> str:
    if not v:
        return '<td class="dim">—</td>'
    return f"<td>{v}</td>"


def _bp_cell(e) -> str:
    if not (e.bp_sys or e.bp_dia or e.bp_map):
        return '<td class="dim">—</td>'
    sd = f"{e.bp_sys or '—'}/{e.bp_dia or '—'}"
    m = f"({e.bp_map})" if e.bp_map else ""
    return f"<td>{sd}{m}</td>"


def _temp_cell(e) -> str:
    t = e.temperature_c
    if not t:
        return '<td class="dim">—</td>'
    cls = ""
    if t < 36.0 or t > 40.0:
        cls = "bad"
    elif t < 37.0 or t > 39.5:
        cls = "warn"
    return f'<td class="{cls}">{t:g}</td>'


def _spo2_cell(e) -> str:
    s = e.spo2
    if not s:
        return '<td class="dim">—</td>'
    cls = "bad" if s < 90 else ("warn" if s < 95 else "")
    return f'<td class="{cls}">{s}</td>'


def _build_html(sheet, cust, pet, clinic_name: str) -> str:
    species = _SPECIES_ZH.get(pet.species or "", pet.species or "") if pet else ""
    rows = []
    for e in sheet.entries:
        depth = _DEPTH_ZH.get(e.depth or "", e.depth or "")
        rows.append(
            "<tr>"
            f"<td>{_fmt_t(e.recorded_at)}</td>"
            f"{_cell(e.hr)}"
            f"{_cell(e.rr)}"
            f"{_spo2_cell(e)}"
            f"{_cell(e.etco2)}"
            f"{_temp_cell(e)}"
            f"{_bp_cell(e)}"
            f"{_cell(('%g' % e.agent_pct) if e.agent_pct else 0)}"
            f"{_cell(('%g' % e.o2_flow) if e.o2_flow else 0)}"
            f"<td>{depth or '—'}</td>"
            f'<td class="ev">{_esc(e.event)}</td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="11" style="color:#aaa;padding:10pt;">（暂无监护记录）</td></tr>')

    svg = _svg_trend(list(sheet.entries))
    chart_block = (
        f'<div class="chart-wrap"><div class="chart-cap">趋势曲线 · HR / RR / SpO₂</div>{svg}</div>'
        if svg else ""
    )

    def hv(label, value):
        return f'<td class="k">{label}</td><td class="v">{_esc(value) if value else "—"}</td>'

    weight = f"{sheet.weight_kg:g} kg" if sheet.weight_kg else "—"
    span = f"{sheet.start_time or '—'} ~ {sheet.end_time or '进行中'}"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{PDF_CSS}</style></head>
<body>
<div class="clinic">{clinic_name}</div>
<div class="sub">DaFo Animal Hospital · Anesthesia Monitoring Record</div>
<hr class="rule"/>
<div class="title">麻 醉 监 护 记 录 单</div>

<table class="hd">
  <tr>{hv("宠物", (pet.name if pet else "") + (("（" + species + "）") if species else ""))}{hv("主人", cust.name if cust else "")}{hv("电话", cust.phone if cust else "")}</tr>
  <tr>{hv("日期", sheet.monitor_date)}{hv("术式", sheet.procedure)}{hv("ASA 分级", sheet.asa_grade)}</tr>
  <tr>{hv("麻醉/监护", sheet.anesthetist)}{hv("术者", sheet.surgeon)}{hv("主麻醉药", sheet.agent)}</tr>
  <tr>{hv("体重", weight)}{hv("麻醉时段", span)}{hv("单号", f"AM{sheet.id:06d}")}</tr>
</table>

{chart_block}

<table class="grid">
  <thead><tr>
    <th>时刻</th><th>HR</th><th>RR</th><th>SpO₂%</th><th>EtCO₂</th><th>体温℃</th>
    <th>血压(收/舒)</th><th>麻醉%</th><th>O₂(L)</th><th>深度</th><th>事件 / 备注</th>
  </tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>

{('<div style="margin-top:8pt;font-size:9pt;color:#555;"><b>备注：</b>' + _esc(sheet.notes) + "</div>") if sheet.notes else ""}

<div class="sign">麻醉/监护人签字：<span></span></div>

<div class="foot">
  <div>本记录单由 {clinic_name} 出具 · 麻醉监护数据按时间点逐条记录</div>
  <div>导出时间：{datetime.utcnow().strftime("%Y-%m-%d") }</div>
</div>
</body></html>
"""


def generate_monitor_pdf(db: Session, sheet_id: int) -> tuple[Optional[str], Optional[str]]:
    """渲染并保存 PDF，返回 (相对路径, 错误信息)；成功时错误为 None。"""
    try:
        from weasyprint import HTML
    except ImportError as e:
        return None, f"weasyprint 包未安装：{e}"
    except OSError as e:
        return None, f"weasyprint 系统库缺失：{e}（apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2）"

    from app.models import AnesthesiaMonitorSheet, Customer, Pet
    sheet = db.get(AnesthesiaMonitorSheet, sheet_id)
    if not sheet:
        return None, "监护表不存在"
    cust = db.get(Customer, sheet.customer_id) if sheet.customer_id else None
    pet = db.get(Pet, sheet.pet_id) if sheet.pet_id else None

    clinic_name = "大风动物医院"
    store_for_title = sheet.store or (pet.store if pet else "")
    if store_for_title:
        clinic_name = f"大风动物医院（{store_for_title.replace('店', '分院')}）"

    html_str = _build_html(sheet, cust, pet, clinic_name)

    out_dir = Path("uploads/anesthesia_monitor")
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"sheet_{sheet.id}.pdf"
    out_path = out_dir / fname
    try:
        HTML(string=html_str).write_pdf(target=str(out_path))
    except Exception as e:
        logger.warning("[anmon_pdf] 渲染失败 sheet=%s: %s", sheet.id, e)
        return None, f"weasyprint 渲染异常：{type(e).__name__}: {e}"

    logger.info("[anmon_pdf] 生成 sheet=%s → %s", sheet.id, fname)
    return f"anesthesia_monitor/{fname}", None
