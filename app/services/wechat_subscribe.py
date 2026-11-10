"""TNR 小程序订阅消息：授权台账、代约 openid、手术前提醒。

一次性订阅：用户每点一次允许，对应模板可发 1 次。发送成功扣减；
微信返回 43101 时把剩余次数清零。时间戳用北京时间（naive，便于和字符串预约日比较）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Application,
    ApplicationStatus,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
    Customer,
    NotificationLog,
    WechatSubscribeGrant,
)
from app.time_utils import beijing_now, beijing_today

logger = logging.getLogger(__name__)

MAIN_SWITCH_TEMPLATE = "__main__"
_ACCEPT = "accept"
_BLOCKED = {"ban", "unauthorized"}

# (台账键, settings 字段, 顾客侧场景优先级)
TNR_TEMPLATE_FIELDS = (
    ("application_result", "wechat_tmpl_application_result"),
    ("rejection", "wechat_tmpl_rejection"),
    ("pending_manual", "wechat_tmpl_pending_manual"),
    ("appointment", "wechat_tmpl_appointment"),
    ("surgery_reminder", "wechat_tmpl_surgery_reminder"),
    ("surgery_done", "wechat_tmpl_surgery_done"),
)

SCENE_ORDER = {
    "apply": ("application_result", "rejection", "pending_manual"),
    "appointment": ("appointment", "surgery_reminder", "surgery_done"),
    "status": (
        "application_result",
        "appointment",
        "surgery_reminder",
        "surgery_done",
        "rejection",
        "pending_manual",
    ),
}

_APPOINTMENT_TEXT = {
    "confirmed": "已确认，请按约定时间到院",
    "booked": "预约已登记，请按约定时间到院",
    "cancelled": "已取消",
    "rescheduled": "已改约，请按新时间到院",
}

_scheduler = None


def _now_naive():
    return beijing_now().replace(tzinfo=None)


def template_catalog() -> dict[str, str]:
    out: dict[str, str] = {}
    for key, field in TNR_TEMPLATE_FIELDS:
        value = (getattr(settings, field, "") or "").strip()
        if value:
            out[key] = value
    return out


def template_key_for(template_id: str) -> str:
    wanted = (template_id or "").strip()
    for key, value in template_catalog().items():
        if value == wanted:
            return key
    return ""


def configured_template_ids() -> set[str]:
    return set(template_catalog().values())


def _grant(db: Session, openid: str, template_id: str) -> WechatSubscribeGrant | None:
    return (
        db.query(WechatSubscribeGrant)
        .filter(
            WechatSubscribeGrant.openid == openid,
            WechatSubscribeGrant.template_id == template_id,
        )
        .first()
    )


def _get_or_create_grant(db: Session, openid: str, template_id: str) -> WechatSubscribeGrant:
    row = _grant(db, openid, template_id)
    if row:
        return row
    now = _now_naive()
    row = WechatSubscribeGrant(
        openid=openid[:64],
        template_id=template_id[:80],
        template_key=template_key_for(template_id)[:40],
        last_status="",
        available_count=0,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def record_subscribe_results(
    db: Session,
    openid: str,
    results: dict,
    *,
    scene: str = "",
    main_switch: str | None = None,
) -> list[dict]:
    """记录一次授权弹窗的结果。accept 加 1 次；ban 清零。"""
    openid = (openid or "").strip()
    if not openid:
        return []
    saved: list[dict] = []
    scene = (scene or "")[:40]
    for raw_id, raw_status in (results or {}).items():
        template_id = str(raw_id or "").strip()
        status = str(raw_status or "").strip().lower()
        if not template_id or template_id in ("errMsg", "errCode"):
            continue
        if status not in ("accept", "reject", "ban", "filter"):
            continue
        row = _get_or_create_grant(db, openid, template_id)
        row.template_key = (template_key_for(template_id) or row.template_key or "")[:40]
        row.scene = scene
        row.updated_at = _now_naive()
        if status == "accept":
            row.available_count = int(row.available_count or 0) + 1
            row.last_status = _ACCEPT
        elif status == "ban":
            row.available_count = 0
            row.last_status = "ban"
        else:
            row.last_status = status
        saved.append({
            "template_id": row.template_id,
            "template_key": row.template_key,
            "last_status": row.last_status,
            "available_count": int(row.available_count or 0),
        })
    if main_switch in ("on", "off"):
        row = _get_or_create_grant(db, openid, MAIN_SWITCH_TEMPLATE)
        row.template_key = "main_switch"
        row.last_status = main_switch
        row.available_count = 0
        row.scene = scene
        row.updated_at = _now_naive()
        saved.append({"template_id": MAIN_SWITCH_TEMPLATE, "last_status": main_switch})
    return saved


def consume_grant(db: Session, openid: str, template_id: str) -> None:
    """发送成功：有台账就扣 1。没有台账则记 0，表示这次一次性额度已经用掉。"""
    openid = (openid or "").strip()
    template_id = (template_id or "").strip()
    if not openid or not template_id:
        return
    row = _get_or_create_grant(db, openid, template_id)
    row.available_count = max(0, int(row.available_count or 0) - 1)
    if row.last_status in ("", "unauthorized"):
        row.last_status = _ACCEPT
    row.updated_at = _now_naive()


def mark_grant_unauthorized(db: Session, openid: str, template_id: str) -> None:
    openid = (openid or "").strip()
    template_id = (template_id or "").strip()
    if not openid or not template_id:
        return
    row = _get_or_create_grant(db, openid, template_id)
    row.available_count = 0
    row.last_status = "unauthorized"
    row.updated_at = _now_naive()


def _grants_for(db: Session, openids: set[str]) -> dict[str, list[WechatSubscribeGrant]]:
    if not openids:
        return {}
    rows = (
        db.query(WechatSubscribeGrant)
        .filter(WechatSubscribeGrant.openid.in_(list(openids)))
        .all()
    )
    grouped: dict[str, list[WechatSubscribeGrant]] = {}
    for row in rows:
        grouped.setdefault(row.openid, []).append(row)
    return grouped


def is_unauthorized(openid: str, grants: list[WechatSubscribeGrant] | None) -> bool:
    """没有 openid、总开关关闭、被 ban，或配置的 TNR 模板都没有剩余次数。"""
    openid = (openid or "").strip()
    if not openid:
        return True
    rows = grants or []
    if any(r.template_id == MAIN_SWITCH_TEMPLATE and r.last_status == "off" for r in rows):
        return True
    if any(r.last_status == "ban" and r.template_id != MAIN_SWITCH_TEMPLATE for r in rows):
        return True
    configured = configured_template_ids()
    if not configured:
        return False
    usable = [
        r for r in rows
        if r.template_id in configured
        and int(r.available_count or 0) > 0
        and r.last_status not in _BLOCKED
    ]
    return not usable


def open_settings_required(grants: list[WechatSubscribeGrant] | None) -> bool:
    rows = grants or []
    if any(r.template_id == MAIN_SWITCH_TEMPLATE and r.last_status == "off" for r in rows):
        return True
    return any(r.last_status == "ban" and r.template_id != MAIN_SWITCH_TEMPLATE for r in rows)


def suggested_template_ids(db: Session, openid: str, scene: str = "status") -> list[str]:
    order = SCENE_ORDER.get(scene, SCENE_ORDER["status"])
    catalog = template_catalog()
    grants = {r.template_id: r for r in (_grants_for(db, {(openid or "").strip()}).get((openid or "").strip(), []))}
    needed: list[str] = []
    for key in order:
        template_id = catalog.get(key) or ""
        if not template_id:
            continue
        row = grants.get(template_id)
        if not row or int(row.available_count or 0) <= 0 or row.last_status in _BLOCKED:
            needed.append(template_id)
    if not needed:
        needed = [catalog[key] for key in order if catalog.get(key)]
    out: list[str] = []
    for template_id in needed:
        if template_id not in out:
            out.append(template_id)
        if len(out) >= 3:
            break
    return out


def subscription_status(db: Session, openid: str, scene: str = "status") -> dict:
    openid = (openid or "").strip()
    grants = _grants_for(db, {openid}).get(openid, []) if openid else []
    unauthorized = is_unauthorized(openid, grants)
    catalog = template_catalog()
    by_id = {r.template_id: r for r in grants}
    templates = []
    for key, template_id in catalog.items():
        row = by_id.get(template_id)
        templates.append({
            "key": key,
            "template_id": template_id,
            "last_status": (row.last_status if row else ""),
            "available_count": int(row.available_count or 0) if row else 0,
        })
    return {
        "openid": openid,
        "needs_prompt": unauthorized,
        "open_settings": open_settings_required(grants),
        "suggested_template_ids": suggested_template_ids(db, openid, scene),
        "templates": templates,
        "prompt": "请开启通知提醒，否则收不到审核/预约/手术进度",
    }


def _like_contains(column, value: str):
    escaped = (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.like(f"%{escaped}%", escape="\\")


def subscribe_event_seen(db: Session, event_key: str, *, success_only: bool = True) -> bool:
    event_key = (event_key or "").strip()
    if not event_key:
        return False
    query = db.query(NotificationLog.id).filter(_like_contains(NotificationLog.payload, event_key))
    if success_only:
        query = query.filter(NotificationLog.success.is_(True))
    return query.first() is not None


def resolve_appointment_openid(db: Session, row: Appointment) -> str:
    """预约上没有 openid 时，从关联申请或客户档案补上，方便之后推送。"""
    current = (row.wechat_openid or "").strip()
    if current:
        return current
    openid = ""
    if row.related_application_id:
        app_row = db.get(Application, row.related_application_id)
        if app_row:
            openid = (app_row.wechat_openid or "").strip()
    if not openid and row.customer_id:
        cust = db.get(Customer, row.customer_id)
        if cust:
            openid = (cust.wechat_openid or "").strip()
    if not openid and (row.category or "") == AppointmentCategory.tnr.value and (row.phone or "").strip():
        app_row = (
            db.query(Application)
            .filter(
                Application.phone == row.phone.strip(),
                Application.wechat_openid.isnot(None),
                Application.wechat_openid != "",
            )
            .order_by(Application.id.desc())
            .first()
        )
        if app_row:
            openid = (app_row.wechat_openid or "").strip()
            if openid and not row.related_application_id:
                row.related_application_id = app_row.id
    if openid:
        row.wechat_openid = openid[:64]
    return openid


def _try_link_tnr_application(db: Session, row: Appointment) -> Application | None:
    if (row.category or "") != AppointmentCategory.tnr.value or row.related_application_id:
        if row.related_application_id:
            return db.get(Application, row.related_application_id)
        return None
    phone = (row.phone or "").strip()
    if not phone:
        return None
    candidates = (
        db.query(Application)
        .filter(
            Application.phone == phone,
            Application.status.in_([
                ApplicationStatus.approved.value,
                ApplicationStatus.pre_approved.value,
                ApplicationStatus.scheduled.value,
            ]),
        )
        .order_by(Application.id.desc())
        .all()
    )
    pet = (row.pet_name or "").strip().lower()
    matched = next((item for item in candidates if (item.cat_nickname or "").strip().lower() == pet), None)
    matched = matched or (candidates[0] if len(candidates) == 1 else None)
    if not matched:
        return None
    row.related_application_id = matched.id
    return matched


def mark_tnr_application_scheduled(db: Session, row: Appointment) -> None:
    """已确认的 TNR 预约，把关联申请推进到「已预约」。与顾客小程序提交预约一致。"""
    if (row.category or "") != AppointmentCategory.tnr.value or not row.related_application_id:
        return
    if (row.status or "") != AppointmentStatus.confirmed.value:
        return
    app_row = db.get(Application, row.related_application_id)
    if not app_row:
        return
    if app_row.status not in (
        ApplicationStatus.approved.value,
        ApplicationStatus.pre_approved.value,
        ApplicationStatus.scheduled.value,
    ):
        return
    if app_row.status != ApplicationStatus.scheduled.value:
        app_row.status = ApplicationStatus.scheduled.value
    app_row.appointment_at = (row.appointment_date or "")[:40]
    # 申请 updated_at 继续用 UTC，和全库既有字段一致；授权台账才记北京时间。
    app_row.updated_at = datetime.utcnow()


def attach_proxy_booking(db: Session, row: Appointment) -> str:
    """代约：补关联申请、带上 openid；已确认的 TNR 同步把申请改为已预约。"""
    if (row.category or "") == AppointmentCategory.tnr.value and not row.related_application_id:
        _try_link_tnr_application(db, row)
    openid = resolve_appointment_openid(db, row)
    mark_tnr_application_scheduled(db, row)
    return openid


def backfill_appointment_openid(db: Session, openid: str) -> int:
    """进度页绑定 / 重新授权后，把 openid 补到还没写上的预约。"""
    openid = (openid or "").strip()
    if not openid:
        return 0
    db.flush()
    app_ids = [
        item[0]
        for item in db.query(Application.id).filter(Application.wechat_openid == openid).all()
    ]
    if not app_ids:
        return 0
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.related_application_id.in_(app_ids),
            or_(Appointment.wechat_openid.is_(None), Appointment.wechat_openid == ""),
        )
        .all()
    )
    for row in rows:
        row.wechat_openid = openid[:64]
    return len(rows)


def appointment_event_key(row: Appointment, kind: str) -> str:
    slot = f"{row.id}:{row.appointment_date}:{row.appointment_time}"
    if kind == "cancelled":
        return f"appointment_cancelled:{slot}"
    if kind == "rescheduled":
        return f"appointment_rescheduled:{slot}"
    return f"appointment_notice:{slot}"


def notify_appointment_event(db: Session, row: Appointment, kind: str, *, note: str = "") -> bool:
    """预约确认 / 登记 / 改约 / 取消。同一 event_key 成功过就不再发。"""
    openid = resolve_appointment_openid(db, row)
    if not openid:
        return False
    status_text = _APPOINTMENT_TEXT.get(kind, _APPOINTMENT_TEXT["booked"])
    from app.services.wechat_miniapp import push_appointment_status
    return push_appointment_status(
        db,
        appointment_id=row.id,
        openid=openid,
        status_text=status_text,
        service_name=row.service_name or "",
        store=row.store or "",
        appointment_date=row.appointment_date or "",
        appointment_time=row.appointment_time or "",
        phone=row.phone or "",
        customer_name=row.customer_name or "",
        note=note or status_text,
        event_key=appointment_event_key(row, kind),
        application_id=row.related_application_id,
    )


def _record_openid(record) -> str:
    if isinstance(record, Application):
        return (record.wechat_openid or "").strip()
    if isinstance(record, Appointment):
        return (getattr(record, "_notify_openid", None) or record.wechat_openid or "").strip()
    return ""


def annotate_notify_flags(db: Session, records: list) -> None:
    """给申请/预约对象挂上员工侧可见的两个标记。"""
    apps = [row for row in records if isinstance(row, Application)]
    appts = [row for row in records if isinstance(row, Appointment)]
    app_ids = {row.id for row in apps}
    app_ids.update(row.related_application_id for row in appts if row.related_application_id)
    related = {}
    if app_ids:
        related = {
            row.id: row
            for row in db.query(Application).filter(Application.id.in_(list(app_ids))).all()
        }
    cust_ids = {row.customer_id for row in appts if row.customer_id}
    customers = {}
    if cust_ids:
        customers = {
            row.id: row
            for row in db.query(Customer).filter(Customer.id.in_(list(cust_ids))).all()
        }
    openids: set[str] = set()
    for row in apps:
        openids.add((row.wechat_openid or "").strip())
    for row in appts:
        openid = (row.wechat_openid or "").strip()
        if not openid and row.related_application_id and row.related_application_id in related:
            openid = (related[row.related_application_id].wechat_openid or "").strip()
        if not openid and row.customer_id and row.customer_id in customers:
            openid = (customers[row.customer_id].wechat_openid or "").strip()
        row._notify_openid = openid
        openids.add(openid)
    openids.discard("")
    grouped = _grants_for(db, openids)

    log_filters = []
    if app_ids:
        log_filters.append(NotificationLog.application_id.in_(list(app_ids)))
    appt_ids = [row.id for row in appts]
    if appt_ids:
        log_filters.append(NotificationLog.appointment_id.in_(appt_ids))
    logs = []
    if log_filters:
        logs = (
            db.query(NotificationLog)
            .filter(NotificationLog.channel == "wechat_miniapp", or_(*log_filters))
            .order_by(NotificationLog.id.desc())
            .all()
        )
    latest_by_app: dict[int, NotificationLog] = {}
    latest_by_appt: dict[int, NotificationLog] = {}
    for item in logs:
        if item.application_id and item.application_id not in latest_by_app:
            latest_by_app[item.application_id] = item
        if item.appointment_id and item.appointment_id not in latest_by_appt:
            latest_by_appt[item.appointment_id] = item

    for row in records:
        openid = _record_openid(row)
        unauthorized = is_unauthorized(openid, grouped.get(openid, []))
        latest = None
        if isinstance(row, Application):
            latest = latest_by_app.get(row.id)
        elif isinstance(row, Appointment):
            latest = latest_by_appt.get(row.id) or (
                latest_by_app.get(row.related_application_id) if row.related_application_id else None
            )
        failed = bool(latest is not None and not latest.success)
        row.notify_unauthorized = unauthorized
        row.notify_last_failed = failed
        labels = []
        if unauthorized:
            labels.append("客户未授权通知")
        if failed:
            labels.append("上次推送失败")
        row.notify_alert = " · ".join(labels)


def run_surgery_reminders(db: Session | None = None, today=None) -> int:
    """给今天和明天已确认的 TNR/手术预约发术前提醒。同一天同类型只尝试一次。"""
    from app.database import SessionLocal
    from app.services.wechat_miniapp import push_surgery_reminder

    own_session = db is None
    if db is None:
        db = SessionLocal()
    sent = 0
    try:
        today = today or beijing_today()
        tomorrow = today + timedelta(days=1)
        today_str = today.strftime("%Y-%m-%d")
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")
        rows = (
            db.query(Appointment)
            .filter(
                Appointment.category.in_([
                    AppointmentCategory.surgery.value,
                    AppointmentCategory.tnr.value,
                ]),
                Appointment.status == AppointmentStatus.confirmed.value,
                Appointment.appointment_date.in_([today_str, tomorrow_str]),
            )
            .all()
        )
        for row in rows:
            openid = resolve_appointment_openid(db, row)
            if not openid:
                continue
            reminder_type = "day_of" if row.appointment_date == today_str else "day_before"
            event_key = f"surgery_reminder_{reminder_type}_{row.id}_{row.appointment_date}"
            delivered = push_surgery_reminder(
                db,
                appointment_id=row.id,
                openid=openid,
                cat_name=(row.pet_name or "猫咪").strip() or "猫咪",
                appointment_date=row.appointment_date or "",
                appointment_time=row.appointment_time or "",
                reminder_type=reminder_type,
                event_key=event_key,
                application_id=row.related_application_id,
                skip_if_any_attempt=True,
            )
            if delivered:
                sent += 1
        db.commit()
        if sent:
            logger.info("手术前提醒已发送 %s 条（%s）", sent, today_str)
        return sent
    except Exception:
        db.rollback()
        logger.exception("手术前提醒执行失败")
        return sent
    finally:
        if own_session:
            db.close()


def start_surgery_reminder_scheduler() -> None:
    """每天北京时间 08:00 跑。进程若在 8 点后才起来，当天补跑一次（有去重）。

    原先用 asyncio.create_task 挂的循环没有保存引用，事件循环只持有弱引用，
    任务可能在睡到 8 点之前被回收；并且一旦启动时已过 8 点，会直接等到明天，
    部署重启就会把当天整段跳过。所以改成和回访/预约提醒一样的调度器。
    """
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:
        logger.warning("手术提醒调度器不可用：%s", exc)
        return
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        run_surgery_reminders,
        CronTrigger(hour=8, minute=0, timezone="Asia/Shanghai"),
        id="tnr_surgery_reminder",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("手术前提醒调度器已启动（北京时间 08:00）")
    if beijing_now().hour >= 8:
        try:
            run_surgery_reminders()
        except Exception:
            logger.exception("手术前提醒启动补跑失败")


def stop_surgery_reminder_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        pass
    _scheduler = None
