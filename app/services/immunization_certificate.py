from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Pet, Vaccination


_VACCINE_TYPE_ZH = {
    "rabies": "狂犬疫苗",
    "combo_3": "猫三联疫苗",
    "combo_6": "犬六联疫苗",
    "canine_8": "犬八联疫苗",
    "deworming": "驱虫",
    "other": "其他疫苗",
}

_SPECIES_ZH = {"cat": "猫", "dog": "犬", "other": "其他"}
_GENDER_ZH = {"male": "公", "female": "母", "unknown": "未知"}


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def build_download_token(
    secret: str,
    customer_id: int,
    pet_id: int,
    ttl_seconds: int = 600,
) -> str:
    payload = {
        "customer_id": int(customer_id),
        "pet_id": int(pet_id),
        "exp": int(time.time()) + max(60, int(ttl_seconds)),
    }
    encoded = _b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def parse_download_token(secret: str, token: str) -> dict[str, int]:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
            raise ValueError("invalid signature")
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
        customer_id = int(payload["customer_id"])
        pet_id = int(payload["pet_id"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid token") from exc
    if expires_at < int(time.time()):
        raise ValueError("expired token")
    return {"customer_id": customer_id, "pet_id": pet_id, "exp": expires_at}


def _dose_label(value: int | None) -> str:
    if value == 99:
        return "加强针"
    if value and value > 0:
        return f"第{value}针"
    return ""


def _vaccination_rows(db: Session, pet_id: int) -> list[dict[str, Any]]:
    vaccinations = (
        db.query(Vaccination)
        .filter(
            Vaccination.pet_id == pet_id,
            or_(Vaccination.status.is_(None), Vaccination.status != "voided"),
        )
        .order_by(Vaccination.vaccinated_date.asc(), Vaccination.id.asc())
        .all()
    )
    rows: list[dict[str, Any]] = []
    for vaccination in vaccinations:
        rabies = vaccination.rabies_record
        inventory_item = vaccination.inventory_item
        vaccine_name = (vaccination.vaccine_name or "").strip()
        vaccine_type = _VACCINE_TYPE_ZH.get(
            (vaccination.vaccine_type or "").strip(), "其他疫苗"
        )
        if not vaccine_name:
            vaccine_name = vaccine_type
        rows.append(
            {
                "id": vaccination.id,
                "vaccinated_date": (vaccination.vaccinated_date or "").strip(),
                "vaccine_type": vaccine_type,
                "vaccine_name": vaccine_name,
                "dose_label": _dose_label(vaccination.dose_number),
                "manufacturer": (
                    ((rabies.vaccine_manufacturer if rabies else "") or "").strip()
                    or ((inventory_item.manufacturer if inventory_item else "") or "").strip()
                ),
                "batch_no": (
                    (vaccination.batch_no or "").strip()
                    or (((rabies.vaccine_batch_no if rabies else "") or "").strip())
                ),
                "next_due_date": (vaccination.next_due_date or "").strip(),
                "vet_name": (
                    (vaccination.vet_name or "").strip()
                    or (((rabies.staff_name if rabies else "") or "").strip())
                ),
                "cert_no": (((rabies.cert_no if rabies else "") or "").strip()),
                "store": (((rabies.clinic_store if rabies else "") or "").strip()),
            }
        )
    return rows


def build_certificate_data(db: Session, pet: Pet) -> dict[str, Any]:
    rows = _vaccination_rows(db, pet.id)
    customer = pet.customer
    store = next((row["store"] for row in reversed(rows) if row["store"]), "")
    if not store:
        store = (pet.store or "").strip()
    last_id = rows[-1]["id"] if rows else 0
    certificate_no = f"EIC-{pet.id:06d}-{last_id:06d}"
    phone = (customer.phone or "").strip() if customer else ""
    masked_phone = phone
    if len(phone) >= 7:
        masked_phone = f"{phone[:3]}****{phone[-4:]}"
    return {
        "certificate_no": certificate_no,
        "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "owner_name": (customer.name or "").strip() if customer else "",
        "owner_phone": masked_phone,
        "pet_id": pet.id,
        "pet_name": (pet.name or "").strip(),
        "medical_record_no": (pet.medical_record_no or "").strip(),
        "species": _SPECIES_ZH.get((pet.species or "").strip(), pet.species or "其他"),
        "breed": (pet.breed or "").strip(),
        "gender": _GENDER_ZH.get((pet.gender or "").strip(), "未知"),
        "birthday": (pet.birthday_estimate or "").strip(),
        "color": (pet.color_pattern or "").strip(),
        "microchip_id": (pet.microchip_id or "").strip(),
        "store": store,
        "vaccinations": rows,
    }


def _text(value: Any, fallback: str = "-") -> str:
    raw = str(value or "").strip()
    return escape(raw if raw else fallback)


def render_immunization_certificate_pdf(data: dict[str, Any]) -> bytes:
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError("服务器未安装 PDF 组件 weasyprint") from exc

    rows_html = []
    for index, row in enumerate(data.get("vaccinations") or [], start=1):
        vaccine = _text(row.get("vaccine_name"))
        vaccine_type = _text(row.get("vaccine_type"), "")
        if vaccine_type and vaccine_type != vaccine:
            vaccine = f"{vaccine}<div class=\"secondary\">{vaccine_type}</div>"
        rows_html.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{_text(row.get('vaccinated_date'))}</td>"
            f"<td>{vaccine}</td>"
            f"<td>{_text(row.get('dose_label'))}</td>"
            f"<td>{_text(row.get('manufacturer'))}</td>"
            f"<td>{_text(row.get('batch_no'))}</td>"
            f"<td>{_text(row.get('vet_name'))}</td>"
            f"<td>{_text(row.get('next_due_date'))}</td>"
            "</tr>"
        )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
@page {{ size: A4 portrait; margin: 16mm 14mm 18mm; @bottom-center {{ content: "大风动物医院电子免疫证 · 第 " counter(page) " 页"; font-size: 8pt; color: #777; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #171717; font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; font-style: normal; font-size: 10pt; line-height: 1.55; }}
.header {{ border-top: 3px solid #171717; border-bottom: 1px solid #171717; padding: 14px 0 12px; text-align: center; }}
.hospital {{ font-size: 10pt; letter-spacing: 4px; }}
h1 {{ margin: 8px 0 2px; font-size: 24pt; letter-spacing: 5px; font-weight: 700; }}
.en {{ color: #666; font-size: 8pt; letter-spacing: 2px; }}
.meta {{ display: flex; justify-content: space-between; margin-top: 9px; color: #555; font-size: 8.5pt; }}
.section-title {{ margin: 18px 0 8px; padding-bottom: 5px; border-bottom: 1px solid #333; font-size: 12pt; font-weight: 700; letter-spacing: 3px; }}
.info {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
.info td {{ border-bottom: 1px solid #ddd; padding: 7px 8px; vertical-align: top; }}
.info .label {{ width: 12%; color: #666; font-size: 8.5pt; }}
.records {{ width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 8.5pt; }}
.records th {{ background: #f1f1ef; border-top: 1px solid #333; border-bottom: 1px solid #333; padding: 7px 4px; text-align: left; font-weight: 700; }}
.records td {{ border-bottom: 1px solid #ddd; padding: 7px 4px; vertical-align: top; word-break: break-word; }}
.records th:nth-child(1), .records td:nth-child(1) {{ width: 4%; text-align: center; }}
.records th:nth-child(2), .records td:nth-child(2) {{ width: 11%; }}
.records th:nth-child(3), .records td:nth-child(3) {{ width: 17%; }}
.records th:nth-child(4), .records td:nth-child(4) {{ width: 8%; }}
.records th:nth-child(5), .records td:nth-child(5) {{ width: 15%; }}
.records th:nth-child(6), .records td:nth-child(6) {{ width: 13%; }}
.records th:nth-child(7), .records td:nth-child(7) {{ width: 10%; }}
.records th:nth-child(8), .records td:nth-child(8) {{ width: 12%; }}
.secondary {{ color: #777; font-size: 7.5pt; margin-top: 2px; }}
.notice {{ margin-top: 14px; padding: 10px 12px; border-left: 3px solid #8b5a2b; background: #faf7f1; color: #555; font-size: 8.5pt; }}
.sign {{ margin-top: 22px; display: flex; justify-content: space-between; color: #555; }}
</style>
</head>
<body>
  <div class="header">
    <div class="hospital">大 风 动 物 医 院</div>
    <h1>电子免疫证</h1>
    <div class="en">ELECTRONIC IMMUNIZATION CERTIFICATE</div>
    <div class="meta"><span>证书编号：{_text(data.get('certificate_no'))}</span><span>生成时间：{_text(data.get('issued_at'))}</span></div>
  </div>

  <div class="section-title">宠物与主人信息</div>
  <table class="info">
    <tr><td class="label">主人</td><td>{_text(data.get('owner_name'))}</td><td class="label">电话</td><td>{_text(data.get('owner_phone'))}</td></tr>
    <tr><td class="label">宠物</td><td>{_text(data.get('pet_name'))}</td><td class="label">病历号</td><td>{_text(data.get('medical_record_no'))}</td></tr>
    <tr><td class="label">种类</td><td>{_text(data.get('species'))}</td><td class="label">性别</td><td>{_text(data.get('gender'))}</td></tr>
    <tr><td class="label">品种</td><td>{_text(data.get('breed'))}</td><td class="label">出生日期</td><td>{_text(data.get('birthday'))}</td></tr>
    <tr><td class="label">毛色</td><td>{_text(data.get('color'))}</td><td class="label">芯片号</td><td>{_text(data.get('microchip_id'))}</td></tr>
  </table>

  <div class="section-title">免疫接种记录（共 {len(rows_html)} 次）</div>
  <table class="records">
    <thead><tr><th>序</th><th>接种日期</th><th>疫苗</th><th>针次</th><th>生产企业</th><th>批号</th><th>接种医生</th><th>下次日期</th></tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
  <div class="notice">本证书依据医院系统内有效接种记录自动生成。已作废记录不在本证书中显示；如宠物信息或接种信息有误，请联系接种门店核对。</div>
  <div class="sign"><span>接种机构：{_text(data.get('store'), '大风动物医院')}</span><span>电子生成，无需手写签章</span></div>
</body>
</html>"""
    return HTML(string=html).write_pdf()
