"""今日工作台 — 数据聚合。

每个 build_xxx 返回一个 dict：
  {
    "key": "appt_today",
    "title": "今日预约",
    "icon": "calendar",
    "count": 5,
    "items": [ { "label": "...", "sub": "...", "url": "/admin/..." }, ... 最多 3 条 ],
    "all_url": "/admin/appointments?date=...",
    "all_label": "查看全部 →",
    "tone": "danger" / "warn" / "info"
  }

按 staff 自动限本店；superadmin 可传 store_short 显式过滤。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session

from app.models import (
    Application, ApplicationStatus,
    Appointment, AppointmentStatus,
    MediaFile, MediaKind,
    Visit, FollowUp,
    Invoice, Payment,
    ConsentTask,
    Deposit,
    Vaccination,
    InventoryItem, InventoryBatch,
    CustomerPackage, Coupon,
    Customer, Pet,
    RabiesVaccineRecord,
    ExamOrder, ExamReport,
)

_STORE_SHORT_TO_FULL = {
    "东环店": "大风动物医院（东环店）",
    "横岗店": "大风动物医院（横岗店）",
}


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _cust_label(c: Optional[Customer], p: Optional[Pet] = None) -> str:
    n = (c.name if c else "") or "未命名"
    if p and (p.name or "").strip():
        return f"{n} · {p.name}"
    return n


# ─────────────────────────────────────────────────────
# 一、今日必做
# ─────────────────────────────────────────────────────

def build_appt_today(db: Session, store_short: str) -> dict:
    today = _today_str()
    full = _STORE_SHORT_TO_FULL.get(store_short, "")
    q = db.query(Appointment).filter(
        Appointment.appointment_date == today,
        Appointment.status.in_([
            AppointmentStatus.pending.value,
            AppointmentStatus.confirmed.value,
            AppointmentStatus.arrived.value,
        ]),
    )
    if full:
        q = q.filter(Appointment.store == full)
    rows = q.order_by(Appointment.appointment_time.asc()).all()
    now_hm = datetime.now().strftime("%H:%M")
    items = []
    for a in rows[:3]:
        if a.status == AppointmentStatus.arrived.value:
            badge = "已到店"
        elif a.appointment_time and a.appointment_time < now_hm:
            badge = "迟到"
        else:
            badge = "待到店"
        items.append({
            "label": f"{a.appointment_time or '—'}　{a.customer_name or '—'}",
            "sub": f"{a.service_name or a.category} · {badge}",
            "url": f"/admin/appointments?date={today}",
        })
    return {
        "key": "appt_today", "title": "今日预约", "icon": "calendar",
        "count": len(rows), "previews": items,
        "all_url": f"/admin/appointments?date={today}",
        "tone": "danger",
    }


def build_followup_today(db: Session, store_short: str) -> dict:
    """通用今日待回访（FollowUp status due/pending 且日期 <= 今天）。"""
    today = _today_str()
    q = db.query(FollowUp).filter(
        FollowUp.status.in_(["pending", "due", "phone_pending"]),
        FollowUp.planned_date != "",
        FollowUp.planned_date <= today,
    )
    if store_short:
        q = q.filter(FollowUp.store == store_short)
    rows = q.order_by(FollowUp.planned_date.asc()).all()
    items = []
    _fu_status_zh = {"pending":"待处理","due":"到期","phone_pending":"电话待催","sent":"已发送","responded":"已回复","closed":"已关闭","skipped":"已忽略"}
    for fu in rows[:3]:
        items.append({
            "label": _cust_label(fu.customer, fu.pet),
            "sub": f"{fu.planned_date} · {_fu_status_zh.get(fu.status, fu.status)}",
            "url": "/admin/follow-ups?tab=today",
        })
    return {
        "key": "followup_today", "title": "待回访任务", "icon": "phone",
        "count": len(rows), "previews": items,
        "all_url": "/admin/follow-ups?tab=today",
        "tone": "danger",
    }


def build_exam_report_pending(db: Session, store_short: str) -> dict:
    """未出检查报告：检查单已开但缺少对应报告的项目。
    粒度到「项」：一张检查单 6 项，其中 X 光/血常规 已上传，B 超 没上传 → 算 1 个待出。
    若一张报告 item_label="" (无归属项)，视为通用报告覆盖全部项。
    限近 30 天的检查单，避免老数据噪声。
    """
    import json as _json
    cutoff = datetime.now() - timedelta(days=30)
    q = db.query(ExamOrder).filter(
        ExamOrder.status != "voided",
        ExamOrder.created_at >= cutoff,
    )
    if store_short:
        q = q.join(Visit, ExamOrder.visit_id == Visit.id, isouter=True)\
             .join(Pet, Visit.pet_id == Pet.id, isouter=True)\
             .filter(or_(Pet.store == store_short, Pet.store == None))
    rows = q.order_by(ExamOrder.created_at.desc()).all()

    pending: list[dict] = []
    for eo in rows:
        try:
            items = _json.loads(eo.items_json or "[]")
        except Exception:
            items = []
        # 没明细项的单子跳过（数据不全）+ 过滤掉「不需要出报告」的纯收费项
        item_names = []
        for i in items:
            n = (i.get("name") or "").strip()
            if not n:
                continue
            iid = i.get("item_id")
            if iid:
                inv = db.get(InventoryItem, int(iid))
                if inv is not None and getattr(inv, "requires_report", True) is False:
                    continue  # 该项无需报告
            item_names.append(n)
        if not item_names:
            continue
        reports = db.query(ExamReport).filter(ExamReport.exam_order_id == eo.id).all()
        reported_labels = {(r.item_label or "").strip() for r in reports}
        # 如果有任何报告的 item_label 为空 → 视为通用，覆盖全部
        has_generic = "" in reported_labels and reports
        if has_generic:
            continue
        missing = [n for n in item_names if n not in reported_labels]
        if not missing:
            continue
        pending.append({"eo": eo, "missing": missing})

    # 老的先排前面（更紧迫）
    pending.sort(key=lambda x: x["eo"].created_at or datetime.min)

    items_preview = []
    for p in pending[:3]:
        eo = p["eo"]
        visit = db.get(Visit, eo.visit_id) if eo.visit_id else None
        cust = db.get(Customer, visit.customer_id) if visit and visit.customer_id else None
        pet = db.get(Pet, visit.pet_id) if visit and visit.pet_id else None
        date_str = eo.created_at.strftime("%m-%d") if eo.created_at else "—"
        miss = " / ".join(p["missing"][:2])
        if len(p["missing"]) > 2:
            miss += f" 等 {len(p['missing'])} 项"
        items_preview.append({
            "label": f"{date_str}　{cust.name if cust else '—'}",
            "sub": f"{pet.name if pet else '宠物'} · 缺：{miss}",
            "url": f"/admin/exam-orders/{eo.id}",
        })
    return {
        "key": "exam_reports_pending", "title": "未出检查报告", "icon": "file-text",
        "count": len(pending), "previews": items_preview,
        "all_url": "/admin/exam-reports/pending",
        "tone": "warn",
    }


def build_consent_pending(db: Session, store_short: str) -> dict:
    """待签协议：status=pending 且发起 > 1 小时还没签的。"""
    cutoff = datetime.now() - timedelta(hours=1)
    q = db.query(ConsentTask).filter(
        ConsentTask.status == "pending",
        ConsentTask.initiated_at <= cutoff,
    )
    if store_short:
        q = q.filter(ConsentTask.store == store_short)
    rows = q.order_by(ConsentTask.initiated_at.asc()).all()
    items = []
    for t in rows[:3]:
        items.append({
            "label": _cust_label(t.customer, t.pet),
            "sub": f"{t.title or '协议'} · {t.initiated_at.strftime('%m-%d %H:%M') if t.initiated_at else ''}",
            "url": f"/admin/consent-tasks/{t.id}",
        })
    return {
        "key": "consent_pending", "title": "待签协议", "icon": "pen-tool",
        "count": len(rows), "previews": items,
        "all_url": "/admin/customers",
        "tone": "danger",
    }


def build_invoice_unpaid(db: Session, store_short: str) -> dict:
    """未付收费单：超过 1 天还没收钱的。"""
    cutoff = datetime.now() - timedelta(days=1)
    q = db.query(Invoice).filter(
        Invoice.payment_status == "unpaid",
        Invoice.created_at <= cutoff,
    )
    # Invoice 没有 store 字段，按 customer 关联门店较复杂；先不按门店过滤（superadmin 全看）
    rows = q.order_by(Invoice.created_at.asc()).limit(50).all()
    items = []
    for inv in rows[:3]:
        cust = inv.customer
        items.append({
            "label": _cust_label(cust, inv.pet),
            "sub": f"{inv.invoice_no or '#'+str(inv.id)} · ¥{inv.total_amount:.0f}",
            "url": f"/admin/invoices/{inv.id}",
        })
    return {
        "key": "invoice_unpaid", "title": "超期未付收费单", "icon": "wallet",
        "count": len(rows), "previews": items,
        "all_url": "/admin/invoices?status=unpaid",
        "tone": "danger",
    }


def build_rabies_pending(db: Session, store_short: str) -> dict:
    """狂犬疫苗登记未完成：owner_pending（待主人签）+ staff_pending（待医护填写）。"""
    q = db.query(RabiesVaccineRecord).filter(
        RabiesVaccineRecord.status.in_(["owner_pending", "staff_pending"]),
    )
    if store_short:
        q = q.filter(RabiesVaccineRecord.clinic_store == store_short)
    rows = q.order_by(RabiesVaccineRecord.created_at.desc()).all()
    items = []
    for r in rows[:3]:
        if r.status == "owner_pending":
            badge = "待主人签字"
        else:
            badge = "待医护填写"
        owner = r.owner_name or "未填姓名"
        animal = r.animal_name or "—"
        items.append({
            "label": f"{owner} · {animal}",
            "sub": f"{badge} · {r.created_at.strftime('%m-%d %H:%M') if r.created_at else ''}",
            "url": f"/admin/rabies/{r.id}",
        })
    return {
        "key": "rabies_pending", "title": "狂犬疫苗待完成", "icon": "dog",
        "count": len(rows), "previews": items,
        "all_url": "/admin/rabies?status=staff_pending",
        "tone": "danger",
    }


def build_tnr_pending(db: Session, store_short: str) -> dict:
    """TNR 待人工审核。"""
    full = _STORE_SHORT_TO_FULL.get(store_short, "")
    q = db.query(Application).filter(
        Application.status == ApplicationStatus.pending_manual.value,
    )
    if full:
        q = q.filter(Application.clinic_store == full)
    rows = q.order_by(Application.created_at.asc()).all()
    items = []
    for a in rows[:3]:
        items.append({
            "label": a.applicant_name or "未命名",
            "sub": f"#{a.id} · {a.created_at.strftime('%m-%d') if a.created_at else ''}",
            "url": f"/admin?status=pending_manual",
        })
    return {
        "key": "tnr_pending", "title": "TNR 待审核", "icon": "cat",
        "count": len(rows), "previews": items,
        "all_url": "/admin?status=pending_manual",
        "tone": "danger",
    }


# ─────────────────────────────────────────────────────
# 二、本周提醒
# ─────────────────────────────────────────────────────

def build_vaccine_due(db: Session, store_short: str) -> dict:
    """疫苗 7 天内到期。"""
    today = _today_str()
    end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    q = db.query(Vaccination).filter(
        Vaccination.next_due_date != "",
        Vaccination.next_due_date >= today,
        Vaccination.next_due_date <= end,
        Vaccination.vaccine_type != "deworming",
    )
    # vaccination 没 store；按 pet.store 过滤
    if store_short:
        q = q.join(Pet, Pet.id == Vaccination.pet_id).filter(Pet.store == store_short)
    rows = q.order_by(Vaccination.next_due_date.asc()).all()
    items = []
    for v in rows[:3]:
        pet = db.query(Pet).filter(Pet.id == v.pet_id).first() if v.pet_id else None
        cust = db.query(Customer).filter(Customer.id == v.customer_id).first() if v.customer_id else None
        items.append({
            "label": _cust_label(cust, pet),
            "sub": f"{v.vaccine_name or v.vaccine_type} · {v.next_due_date}",
            "url": f"/admin/customers/{v.customer_id}?tab=vaccinations" if v.customer_id else "/admin/vaccinations",
        })
    return {
        "key": "vaccine_due", "title": "疫苗 7 天内到期", "icon": "syringe",
        "count": len(rows), "previews": items,
        "all_url": "/admin/vaccinations",
        "tone": "warn",
    }


def build_deworm_due(db: Session, store_short: str) -> dict:
    """驱虫 7 天内到期。"""
    today = _today_str()
    end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    q = db.query(Vaccination).filter(
        Vaccination.next_due_date != "",
        Vaccination.next_due_date >= today,
        Vaccination.next_due_date <= end,
        Vaccination.vaccine_type == "deworming",
    )
    if store_short:
        q = q.join(Pet, Pet.id == Vaccination.pet_id).filter(Pet.store == store_short)
    rows = q.order_by(Vaccination.next_due_date.asc()).all()
    items = []
    for v in rows[:3]:
        pet = db.query(Pet).filter(Pet.id == v.pet_id).first() if v.pet_id else None
        cust = db.query(Customer).filter(Customer.id == v.customer_id).first() if v.customer_id else None
        items.append({
            "label": _cust_label(cust, pet),
            "sub": f"驱虫 · {v.next_due_date}",
            "url": f"/admin/customers/{v.customer_id}?tab=vaccinations" if v.customer_id else "/admin/vaccinations",
        })
    return {
        "key": "deworm_due", "title": "驱虫 7 天内到期", "icon": "bug",
        "count": len(rows), "previews": items,
        "all_url": "/admin/vaccinations?type=deworming",
        "tone": "warn",
    }


def build_chronic_recheck(db: Session, store_short: str) -> dict:
    """慢病复诊：Visit.follow_up_at 在今天 ± 7 天内。"""
    today = _today_str()
    end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    q = db.query(Visit).filter(
        Visit.follow_up_at != "",
        Visit.follow_up_at >= today,
        Visit.follow_up_at <= end,
    )
    if store_short:
        # visit 没 store 字段；按 pet.store 过滤
        q = q.join(Pet, Pet.id == Visit.pet_id).filter(Pet.store == store_short)
    rows = q.order_by(Visit.follow_up_at.asc()).all()
    items = []
    for v in rows[:3]:
        items.append({
            "label": _cust_label(v.customer, v.pet),
            "sub": f"{v.diagnosis[:20] if v.diagnosis else '复诊'} · {v.follow_up_at}",
            "url": f"/admin/visits/{v.id}",
        })
    return {
        "key": "chronic_recheck", "title": "复诊提醒（含慢病）", "icon": "repeat",
        "count": len(rows), "previews": items,
        "all_url": "/admin/visits",
        "tone": "warn",
    }


def build_deposit_held(db: Session, store_short: str) -> dict:
    """押金未结算 > 7 天。"""
    cutoff = datetime.now() - timedelta(days=7)
    q = db.query(Deposit).filter(
        Deposit.status == "held",
        Deposit.created_at <= cutoff,
    )
    if store_short:
        q = q.filter(Deposit.store == store_short)
    rows = q.order_by(Deposit.created_at.asc()).all()
    items = []
    for d in rows[:3]:
        items.append({
            "label": _cust_label(d.customer, d.pet),
            "sub": f"{d.category} · ¥{d.amount:.0f} · {d.created_at.strftime('%m-%d') if d.created_at else ''}",
            "url": f"/admin/customers/{d.customer_id}?tab=deposits" if d.customer_id else "/admin/customers",
        })
    return {
        "key": "deposit_held", "title": "押金超 7 天未结算", "icon": "lock",
        "count": len(rows), "previews": items,
        "all_url": "/admin/customers",
        "tone": "warn",
    }


def build_surgery_after_missing(db: Session, store_short: str) -> dict:
    """术后照片缺失：Application 状态 arrived_verified 但未上传 surgery_after。"""
    full = _STORE_SHORT_TO_FULL.get(store_short, "")
    q = db.query(Application).filter(
        Application.status == ApplicationStatus.arrived_verified.value,
    )
    if full:
        q = q.filter(Application.clinic_store == full)
    rows = q.all()
    # 过滤：没有 surgery_after 媒体
    pending = []
    for a in rows:
        has_after = any(
            m.kind == MediaKind.surgery_after.value
            for m in (a.media or [])
        )
        if not has_after:
            pending.append(a)
    items = []
    for a in pending[:3]:
        items.append({
            "label": a.applicant_name or f"#{a.id}",
            "sub": f"已到院待手术完成 · {a.updated_at.strftime('%m-%d') if a.updated_at else ''}",
            "url": f"/admin?status=arrived_verified",
        })
    return {
        "key": "surgery_after_missing", "title": "术后照片未上传", "icon": "camera",
        "count": len(pending), "previews": items,
        "all_url": "/admin?status=arrived_verified",
        "tone": "warn",
    }


def build_surgery_followup_today(db: Session, store_short: str) -> dict:
    """手术回访今日提醒：FollowUp 关联的 Visit.visit_type in ('surgery','postop')。"""
    today = _today_str()
    q = (db.query(FollowUp)
         .join(Visit, Visit.id == FollowUp.visit_id)
         .filter(
             FollowUp.status.in_(["pending", "due", "sent", "phone_pending"]),
             FollowUp.planned_date != "",
             FollowUp.planned_date <= today,
             Visit.visit_type.in_(["surgery", "postop"]),
         ))
    if store_short:
        q = q.filter(FollowUp.store == store_short)
    rows = q.order_by(FollowUp.planned_date.asc()).all()
    items = []
    for fu in rows[:3]:
        items.append({
            "label": _cust_label(fu.customer, fu.pet),
            "sub": f"手术回访 · {fu.planned_date}",
            "url": f"/admin/follow-ups",
        })
    return {
        "key": "surgery_followup_today", "title": "手术回访今日", "icon": "stethoscope",
        "count": len(rows), "previews": items,
        "all_url": "/admin/follow-ups?status=due",
        "tone": "warn",
    }


def build_outpatient_followup_today(db: Session, store_short: str) -> dict:
    """门诊回访今日提醒：Visit.visit_type = outpatient。"""
    today = _today_str()
    q = (db.query(FollowUp)
         .join(Visit, Visit.id == FollowUp.visit_id)
         .filter(
             FollowUp.status.in_(["pending", "due", "sent", "phone_pending"]),
             FollowUp.planned_date != "",
             FollowUp.planned_date <= today,
             Visit.visit_type == "outpatient",
         ))
    if store_short:
        q = q.filter(FollowUp.store == store_short)
    rows = q.order_by(FollowUp.planned_date.asc()).all()
    items = []
    for fu in rows[:3]:
        items.append({
            "label": _cust_label(fu.customer, fu.pet),
            "sub": f"门诊回访 · {fu.planned_date}",
            "url": f"/admin/follow-ups",
        })
    return {
        "key": "outpatient_followup_today", "title": "门诊回访今日", "icon": "stethoscope",
        "count": len(rows), "previews": items,
        "all_url": "/admin/follow-ups?status=due",
        "tone": "warn",
    }


# ─────────────────────────────────────────────────────
# 三、库存/经营预警
# ─────────────────────────────────────────────────────

def build_batch_expiry(db: Session, store_short: str) -> dict:
    """库存批次有效期 90 天内到期 — 覆盖所有品目（药品/疫苗/耗材/化验/影像试剂等）。"""
    today = _today_str()
    end = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
    q = (db.query(InventoryBatch)
         .filter(
             InventoryBatch.is_depleted.is_(False),
             InventoryBatch.quantity > 0,
             InventoryBatch.expiry_date != "",
             InventoryBatch.expiry_date >= today,
             InventoryBatch.expiry_date <= end,
         ))
    if store_short:
        # 批次本身没 store 字段，通过 item 关联过滤
        q = q.join(InventoryItem, InventoryItem.id == InventoryBatch.item_id).filter(
            or_(InventoryItem.store == store_short, InventoryItem.store == "")
        )
    q = q.order_by(InventoryBatch.expiry_date.asc())
    rows = q.all()
    _CAT_ZH = {
        "medication": "药品", "vaccine": "疫苗", "antiparasitic": "驱虫",
        "consumable": "耗材", "product": "商品", "grooming": "美容用品",
        "lab": "化验试剂", "imaging": "影像耗材", "microscopy": "显微",
    }
    items = []
    for b in rows[:3]:
        item = b.item
        cat = _CAT_ZH.get((item.category if item else ""), (item.category if item else "")) or "—"
        items.append({
            "label": f"[{cat}] {(item.name if item else '未知品目')}",
            "sub": f"批 {b.batch_no or '—'} · 余 {b.quantity} · 到期 {b.expiry_date}",
            "url": f"/admin/inventory/{b.item_id}",
        })
    return {
        "key": "batch_expiry", "title": "库存 90 天内到期", "icon": "hourglass",
        "count": len(rows), "previews": items,
        "all_url": "/admin/inventory",
        "tone": "info",
    }


def build_low_stock(db: Session, store_short: str) -> dict:
    q = db.query(InventoryItem).filter(
        InventoryItem.is_active.is_(True),
        InventoryItem.is_service.is_(False),
        InventoryItem.low_stock_min > 0,
        InventoryItem.stock_qty <= InventoryItem.low_stock_min,
    )
    if store_short:
        q = q.filter(or_(InventoryItem.store == store_short, InventoryItem.store == ""))
    q = q.order_by((InventoryItem.stock_qty - InventoryItem.low_stock_min).asc())
    rows = q.all()
    items = []
    for it in rows[:3]:
        items.append({
            "label": it.name,
            "sub": f"剩 {it.stock_qty} {it.unit} · 阈值 {it.low_stock_min}",
            "url": f"/admin/inventory/{it.id}",
        })
    return {
        "key": "low_stock", "title": "库存低于阈值", "icon": "trending-down",
        "count": len(rows), "previews": items,
        "all_url": "/admin/inventory",
        "tone": "info",
    }


def build_package_expiring(db: Session, store_short: str) -> dict:
    """套餐 30 天内到期。"""
    today = _today_str()
    end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    q = db.query(CustomerPackage).filter(
        CustomerPackage.status == "active",
        CustomerPackage.expires_at != "",
        CustomerPackage.expires_at >= today,
        CustomerPackage.expires_at <= end,
    )
    if store_short:
        # 已售的 CustomerPackage.store 记录的是售卖时的门店；通用就空
        q = q.filter(or_(CustomerPackage.store == store_short, CustomerPackage.store == ""))
    rows = q.order_by(CustomerPackage.expires_at.asc()).all()
    items = []
    for p in rows[:3]:
        items.append({
            "label": _cust_label(p.customer, p.pet),
            "sub": f"{p.name} · 余 {p.total_uses - p.used_count} 次 · {p.expires_at}",
            "url": f"/admin/customers/{p.customer_id}?tab=packages" if p.customer_id else "/admin/packages",
        })
    return {
        "key": "package_expiring", "title": "套餐 30 天内到期", "icon": "package",
        "count": len(rows), "previews": items,
        "all_url": "/admin/packages",
        "tone": "info",
    }


def build_coupon_expiring(db: Session, store_short: str) -> dict:
    """优惠券 30 天内到期。"""
    today = _today_str()
    end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    q = db.query(Coupon).filter(
        Coupon.status == "issued",
        Coupon.expires_at != "",
        Coupon.expires_at >= today,
        Coupon.expires_at <= end,
    )
    if store_short:
        q = q.filter(or_(Coupon.store == store_short, Coupon.store == ""))
    rows = q.order_by(Coupon.expires_at.asc()).all()
    items = []
    for c in rows[:3]:
        items.append({
            "label": c.title or c.code,
            "sub": f"{c.code} · {c.expires_at}",
            "url": "/admin/coupons",
        })
    return {
        "key": "coupon_expiring", "title": "优惠券 30 天内到期", "icon": "ticket",
        "count": len(rows), "previews": items,
        "all_url": "/admin/coupons",
        "tone": "info",
    }


# ─────────────────────────────────────────────────────
# 汇总入口
# ─────────────────────────────────────────────────────

def build_workbench(db: Session, store_short: str = "") -> dict:
    """返回 {urgent: [...], weekly: [...], stock: [...]} 三组卡片。"""
    urgent = [
        build_appt_today(db, store_short),
        build_followup_today(db, store_short),
        build_exam_report_pending(db, store_short),
        build_consent_pending(db, store_short),
        build_rabies_pending(db, store_short),
        build_invoice_unpaid(db, store_short),
        build_tnr_pending(db, store_short),
    ]
    weekly = [
        build_vaccine_due(db, store_short),
        build_deworm_due(db, store_short),
        build_chronic_recheck(db, store_short),
        build_deposit_held(db, store_short),
        build_surgery_after_missing(db, store_short),
        build_surgery_followup_today(db, store_short),
        build_outpatient_followup_today(db, store_short),
    ]
    stock = [
        build_batch_expiry(db, store_short),
        build_low_stock(db, store_short),
        build_package_expiring(db, store_short),
        build_coupon_expiring(db, store_short),
    ]
    return {"urgent": urgent, "weekly": weekly, "stock": stock}
