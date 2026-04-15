from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from passlib.context import CryptContext
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.config import settings
from app.database import get_db, init_db
from app.models import (
    AdminUser,
    Application,
    ApplicationStatus,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
    AuditLog,
    Contract,
    ContractType,
    MediaFile,
    MediaKind,
    Staff,
    StaffStatus,
)
from app.services.ai_review import apply_auto_status_from_ai, review_application_media
from app.services.notify import notify_application_result
from app.services.backup_local import create_backup_zip, is_safe_backup_filename, list_backup_zips
from app.services.wechat_miniapp import push_application_result, push_appointment_status, push_pending_manual_notice, push_rejection_notice, push_surgery_done, push_surgery_reminder, wechat_code2session

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="tnr_session")

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# 后台「AI 辅助结论」：把模型 JSON 转成中文可读结构（模板用 | ai_review_view）
_AI_FLAG_ZH = {
    "collar": "可见项圈等装饰",
    "carrier": "猫包 / 航空箱等携带方式",
    "indoor_luxury": "偏家养 / 室内饲养环境线索",
}


def _filter_ai_review_view(raw: str | None) -> dict:
    out: dict = {
        "parse_error": False,
        "stray_zh": "—",
        "confidence_zh": "—",
        "reasons": [],
        "photo_text": "",
        "fraud_lines": [],
        "caveats": [],
        "suggestion_zh": "",
    }
    if not raw or not str(raw).strip():
        return out
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        out["parse_error"] = True
        return out
    if not isinstance(d, dict):
        out["parse_error"] = True
        return out

    v = d.get("is_likely_stray")
    if v is True:
        out["stray_zh"] = "是（模型判断更接近流浪 / 无主场景）"
    elif v is False:
        out["stray_zh"] = "否（模型判断不够像流浪，或信息不足）"
    else:
        out["stray_zh"] = "未给出明确结论"

    conf = d.get("confidence")
    try:
        c = float(conf)
        if 0 <= c <= 1:
            out["confidence_zh"] = f"{c * 100:.0f}%"
        else:
            out["confidence_zh"] = f"{c:.2f}"
    except (TypeError, ValueError):
        out["confidence_zh"] = "—"

    reasons = d.get("reasons")
    if isinstance(reasons, list):
        out["reasons"] = [str(x).strip() for x in reasons if str(x).strip()]

    kidx = d.get("key_evidence_photo_indexes")
    if isinstance(kidx, list) and kidx:
        nums: list[str] = []
        for x in kidx:
            try:
                nums.append(str(int(x)))
            except (TypeError, ValueError):
                if x is not None and str(x).strip():
                    nums.append(str(x).strip())
        if nums:
            out["photo_text"] = "第 " + "、".join(nums) + " 张（按申请时照片顺序）"

    flags = d.get("anti_fraud_flags")
    if isinstance(flags, list) and flags:
        known: list[str] = []
        unknown_n = 0
        for f in flags:
            key = str(f).strip().lower()
            zh = _AI_FLAG_ZH.get(key)
            if zh:
                if zh not in known:
                    known.append(zh)
            elif str(f).strip():
                unknown_n += 1
        out["fraud_lines"] = known.copy()
        if unknown_n:
            out["fraud_lines"].append(f"另有 {unknown_n} 条内部标记未展开（已由系统参与规则判断）")

    caveats = d.get("caveats")
    if isinstance(caveats, list):
        out["caveats"] = [str(x).strip() for x in caveats if str(x).strip()]

    step = (d.get("suggested_next_step") or "").strip().lower().replace("-", "_")
    if step == "auto_approve_candidate":
        out["suggestion_zh"] = "模型流程建议：可作自动通过候选（实际状态已由系统规则与阈值综合决定，医院仍可拒绝或取消）。"
    elif step == "manual_review":
        out["suggestion_zh"] = "模型流程建议：优先走人工复核。"
    elif step:
        out["suggestion_zh"] = "模型已给出内部流程建议，系统已按规则处理。"
    else:
        out["suggestion_zh"] = "—"

    return out


templates.env.filters["ai_review_view"] = _filter_ai_review_view

_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)

# 中国省 / 市 / 区 / 街道四级数据（static/china_pcas.json，来源见 static/china_pcas.source.txt）
_china_pcas: dict | None = None


def _load_china_pcas() -> dict:
    global _china_pcas
    if _china_pcas is None:
        path = Path(__file__).resolve().parent.parent / "static" / "china_pcas.json"
        if not path.is_file():
            _china_pcas = {}
        else:
            _china_pcas = json.loads(path.read_text(encoding="utf-8"))
    return _china_pcas


@app.on_event("startup")
def _startup():
    init_db()
    asyncio.get_event_loop().create_task(_surgery_reminder_loop())


async def _surgery_reminder_loop():
    """每天定时检查手术预约并推送前一天+当天提醒（#6）。"""
    await asyncio.sleep(10)  # 启动后延迟 10s 再进入循环
    while True:
        try:
            _run_surgery_reminders()
        except Exception:
            pass
        # 计算距离明天 8:00 的等待时间（每天早上 8 点触发）
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        if now.hour < 8:
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())


def _run_surgery_reminders():
    """同步执行：查询手术预约并推送提醒。"""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        today_str = today.strftime("%Y-%m-%d")
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")

        # 查询今天和明天的手术/TNR 预约（已确认）
        rows = (
            db.query(Appointment)
            .filter(
                Appointment.category.in_([AppointmentCategory.surgery.value, AppointmentCategory.tnr.value]),
                Appointment.status == AppointmentStatus.confirmed.value,
                Appointment.appointment_date.in_([today_str, tomorrow_str]),
                Appointment.wechat_openid.isnot(None),
                Appointment.wechat_openid != "",
            )
            .all()
        )
        for row in rows:
            openid = (row.wechat_openid or "").strip()
            if not openid:
                continue
            reminder_type = "day_of" if row.appointment_date == today_str else "day_before"
            log_key = f"surgery_reminder_{reminder_type}_{row.id}_{row.appointment_date}"
            # 检查是否已推送过（同一天同一类型不重复发）
            from app.models import NotificationLog as _NL
            already = (
                db.query(_NL)
                .filter(_NL.payload.contains(log_key))
                .first()
            )
            if already:
                continue
            cat_name = (row.pet_name or "猫咪").strip()
            push_surgery_reminder(
                db,
                appointment_id=row.id,
                openid=openid,
                cat_name=cat_name,
                appointment_date=row.appointment_date or "",
                appointment_time=row.appointment_time or "",
                reminder_type=reminder_type,
            )
            # 记录已推送标记
            db.add(_NL(
                application_id=row.related_application_id,
                channel="log",
                payload=log_key,
                success=True,
            ))
            db.commit()
    finally:
        db.close()


def _admin_ok(request: Request) -> bool:
    return bool(request.session.get("admin"))


def _admin_role(request: Request) -> str:
    """返回当前登录角色：'superadmin' | 'staff' | ''（未登录）。旧 session 默认当 superadmin。"""
    if not request.session.get("admin"):
        return ""
    return request.session.get("admin_role", "superadmin")


def _is_superadmin(request: Request) -> bool:
    return _admin_role(request) == "superadmin"


def require_admin(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=401, detail="需要医院后台登录")


def require_superadmin(request: Request):
    if not _is_superadmin(request):
        raise HTTPException(status_code=403, detail="需要超级管理员权限")


def _get_csrf_token(request: Request) -> str:
    tok = request.session.get("csrf_token") or ""
    if not isinstance(tok, str) or not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf_token"] = tok
    return tok


def _require_csrf(request: Request, csrf_token: str) -> None:
    expected = request.session.get("csrf_token") or ""
    if not isinstance(expected, str) or not expected:
        raise HTTPException(status_code=403, detail="CSRF token missing")
    if not isinstance(csrf_token, str) or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token invalid")


def _audit(
    db: Session,
    request: Request,
    action: str,
    *,
    application_id: int | None = None,
    detail: dict | str | None = None,
):
    ip = (request.client.host if request.client else "") or ""
    ua = request.headers.get("user-agent", "") or ""
    if isinstance(detail, dict):
        detail_s = json.dumps(detail, ensure_ascii=False)
    elif isinstance(detail, str):
        detail_s = detail
    else:
        detail_s = ""
    db.add(
        AuditLog(
            action=action,
            actor=request.session.get("admin_username", "admin"),
            application_id=application_id,
            ip=ip,
            user_agent=ua[:300],
            detail=detail_s,
        )
    )


def _require_status_in(row: Application, allowed: set[str], action_label: str) -> None:
    if row.status not in allowed:
        zh = {
            ApplicationStatus.draft.value: "草稿",
            ApplicationStatus.pending_ai.value: "系统处理中",
            ApplicationStatus.pending_manual.value: "待人工审核",
            ApplicationStatus.pre_approved.value: "预通过（待复核）",
            ApplicationStatus.approved.value: "已通过",
            ApplicationStatus.scheduled.value: "已预约",
            ApplicationStatus.no_show.value: "爽约",
            ApplicationStatus.cancelled.value: "已取消",
            ApplicationStatus.rejected.value: "已拒绝",
            ApplicationStatus.arrived_verified.value: "到院已核对",
            ApplicationStatus.surgery_completed.value: "手术完成",
        }
        allowed_zh = " / ".join(zh.get(s, s) for s in sorted(allowed))
        current_zh = zh.get(row.status, row.status)
        raise HTTPException(409, f"{action_label}仅允许在「{allowed_zh}」状态执行，当前为「{current_zh}」。")


def _application_has_surgery_before_and_after(db: Session, application_id: int) -> bool:
    """术前、术后各至少一条媒体（照片或视频均可）。"""
    rows = (
        db.query(MediaFile.kind)
        .filter(MediaFile.application_id == application_id)
        .filter(MediaFile.kind.in_((MediaKind.surgery_before.value, MediaKind.surgery_after.value)))
        .all()
    )
    kinds = {r[0] for r in rows}
    return MediaKind.surgery_before.value in kinds and MediaKind.surgery_after.value in kinds


@app.get("/api/regions/china")
async def api_regions_china():
    """省 / 市 / 区 / 街道四级行政区划（全量）。数据：modood/Administrative-divisions-of-China dist/pcas.json"""
    return _load_china_pcas()


def _shenzhen_district_streets() -> dict:
    """深圳市：区 → 街道列表（当前业务仅限深圳门店）。"""
    pcas = _load_china_pcas()
    prov = pcas.get("广东省") or {}
    sz = prov.get("深圳市")
    return sz if isinstance(sz, dict) else {}


def _shenzhen_regions_embed() -> dict:
    """优先读 static/shenzhen_regions.json；缺失或损坏时从全量 pcas 推导。"""
    p = Path(__file__).resolve().parent.parent / "static" / "shenzhen_regions.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d:
                return d
        except (json.JSONDecodeError, OSError):
            pass
    return _shenzhen_district_streets()


@app.get("/api/regions/shenzhen")
async def api_regions_shenzhen():
    """深圳市 区 / 街道二级数据（体积小，供申请页默认深圳使用）。"""
    return _shenzhen_regions_embed()


@app.get("/api/diag")
async def api_diag():
    k = settings.openai_api_key or ""
    ws = settings.wechat_appsecret or ""
    return {
        "openai_base_url": settings.openai_base_url,
        "openai_model": settings.openai_model,
        "openai_key_set": bool(k.strip()),
        "openai_key_is_ascii": k.isascii() if k else True,
        "openai_key_len": len(k),

        # wechat miniapp (do not expose secret)
        "wechat_appid_set": bool((settings.wechat_appid or "").strip()),
        "wechat_appsecret_set": bool(ws.strip()),
        "wechat_tmpl_application_result_set": bool((settings.wechat_tmpl_application_result or "").strip()),
        "wechat_tmpl_surgery_done_set": bool((settings.wechat_tmpl_surgery_done or "").strip()),
        "wechat_message_page": settings.wechat_message_page,
    }


@app.get("/api/wechat/config")
async def api_wechat_config():
    """给小程序前端下发订阅消息模板配置（不包含任何 secret）。"""
    return {
        "wechat_appid": settings.wechat_appid,
        "wechat_tmpl_application_result": settings.wechat_tmpl_application_result,
        "wechat_tmpl_surgery_done": settings.wechat_tmpl_surgery_done,
        "wechat_tmpl_appointment": settings.wechat_tmpl_appointment,
        "wechat_tmpl_rejection": settings.wechat_tmpl_rejection,
        "wechat_tmpl_pending_manual": settings.wechat_tmpl_pending_manual,
        "wechat_message_page": settings.wechat_message_page,
        "wechat_fields_application_result": settings.wechat_fields_application_result,
        "wechat_fields_surgery_done": settings.wechat_fields_surgery_done,
        "wechat_fields_appointment": settings.wechat_fields_appointment,
        "wechat_fields_rejection": settings.wechat_fields_rejection,
    }


@app.get("/api/geocode/regeo")
async def api_geocode_regeo(lat: str = "", lng: str = ""):
    """经纬度 -> 地址（高德逆地理编码）。未配置 Key 时返回空。"""
    key = (settings.amap_web_key or "").strip()
    if not key:
        return {"ok": False, "address": "", "detail": "AMAP_WEB_KEY not set", "key_tail": ""}
    lat = (lat or "").strip()
    lng = (lng or "").strip()
    if not lat or not lng:
        raise HTTPException(400, "missing lat/lng")
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {
        "key": key,
        "location": f"{lng},{lat}",
        "radius": "1000",
        "extensions": "base",
        "roadlevel": "0",
    }
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"ok": False, "address": "", "detail": str(e)}
    addr = ""
    try:
        if str(data.get("status")) == "1":
            rg = data.get("regeocode") or {}
            addr = (rg.get("formatted_address") or "").strip()
    except Exception:
        addr = ""
    return {
        "ok": bool(addr),
        "address": addr,
        "amap_info": data.get("info"),
        "amap_infocode": data.get("infocode"),
        "key_tail": key[-6:],
        "raw": data if not addr else None,
    }


@app.get("/", response_class=HTMLResponse)
async def page_apply(request: Request):
    return templates.TemplateResponse(
        request,
        "apply.html",
        {
            "title": settings.app_name,
            "shenzhen_regions": _shenzhen_regions_embed(),
        },
    )


_CLINIC_STORES = ("大风动物医院（东环店）", "大风动物医院（横岗店）")
_ALLOWED_CLINIC_STORES = frozenset(_CLINIC_STORES)
_ALLOWED_APPOINTMENT_CATEGORIES = frozenset({x.value for x in AppointmentCategory})
_ALLOWED_APPOINTMENT_STATUSES = frozenset({x.value for x in AppointmentStatus})
_APPOINTMENT_CATEGORY_LABELS = {
    AppointmentCategory.tnr.value: "TNR 预约",
    AppointmentCategory.outpatient.value: "门诊预约",
    AppointmentCategory.surgery.value: "手术预约",
    AppointmentCategory.beauty.value: "美容预约",
    AppointmentCategory.grooming.value: "造型预约",   # 历史兼容
    AppointmentCategory.washcare.value: "洗护预约",   # 历史兼容
}
_APPOINTMENT_STATUS_LABELS = {
    AppointmentStatus.pending.value: "待确认",
    AppointmentStatus.confirmed.value: "已确认",
    AppointmentStatus.completed.value: "已完成",
    AppointmentStatus.cancelled.value: "已取消",
    AppointmentStatus.no_show.value: "未到店",
}
_PET_GENDER_LABELS = {"male": "公", "female": "母", "unknown": "未知"}
_APPOINTMENT_BOOKING_MAX_DAYS_AHEAD = 30


def _assert_appointment_fields(
    *,
    category: str,
    service_name: str,
    customer_name: str,
    phone: str,
    pet_name: str,
    pet_gender: str,
    store: str,
    appointment_date: str,
    appointment_time: str,
    notes: str,
    duration_minutes: str,
) -> dict[str, str | int]:
    def need(label: str, raw: str, max_len: int) -> str:
        s = (raw or "").strip()
        if not s:
            raise HTTPException(400, f"请填写{label}。")
        if len(s) > max_len:
            raise HTTPException(400, f"{label}过长。")
        return s

    out: dict[str, str | int] = {}
    cat = (category or "").strip()
    if cat not in _ALLOWED_APPOINTMENT_CATEGORIES:
        raise HTTPException(400, "请选择有效的预约类型。")
    out["category"] = cat
    out["service_name"] = need("预约项目", service_name, 120)
    out["customer_name"] = need("联系人姓名", customer_name, 120)
    phone_v = need("手机号", phone, 40)
    if not re.fullmatch(r"1\d{10}", phone_v):
        raise HTTPException(400, "请填写 11 位中国大陆手机号。")
    out["phone"] = phone_v
    out["pet_name"] = need("宠物/流浪猫名称", pet_name, 120)
    g = (pet_gender or "").strip().lower()
    if g not in ("male", "female", "unknown"):
        raise HTTPException(400, "请选择性别。")
    out["pet_gender"] = g
    store_v = need("门店", store, 120)
    if store_v not in _ALLOWED_CLINIC_STORES:
        raise HTTPException(400, "请选择有效的预约门店。")
    out["store"] = store_v
    date_v = need("预约日期", appointment_date, 20)
    try:
        date_obj = datetime.strptime(date_v, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "预约日期格式应为 YYYY-MM-DD。")
    if date_obj < datetime.now().date():
        raise HTTPException(400, "预约日期不能早于今天。")
    out["appointment_date"] = date_v
    time_v = need("预约时间", appointment_time, 20)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_v):
        raise HTTPException(400, "预约时间格式应为 HH:MM。")
    out["appointment_time"] = time_v
    out["notes"] = (notes or "").strip()[:2000]
    try:
        dur = int((duration_minutes or "30").strip() or "30")
    except ValueError:
        raise HTTPException(400, "时长应为分钟数。")
    if dur < 10 or dur > 480:
        raise HTTPException(400, "时长范围应在 10 到 480 分钟之间。")
    out["duration_minutes"] = dur
    # 与小程序 TNR 预约一致：后台/API 侧也固定项目与时长，避免手改表单绕过
    if out["category"] == AppointmentCategory.tnr.value:
        out["service_name"] = "TNR 手术安排"
        out["duration_minutes"] = 60
    return out


def _mask_phone(phone: str) -> str:
    t = (phone or "").strip()
    if len(t) < 7:
        return t
    return t[:3] + "****" + t[-4:]


_TNR_TIME_START = "11:00"
_TNR_TIME_END = "18:00"
_TNR_DAILY_MAX = 2


def _time_to_minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _check_appointment_conflict(
    db: "Session",
    store: str,
    appointment_date: str,
    appointment_time: str,
    duration_minutes: int,
    exclude_id: int | None = None,
) -> "Appointment | None":
    """检查同门店同日是否存在时间重叠的预约（排除已取消/爽约）。重叠则返回冲突预约记录，否则返回 None。"""
    q = (
        db.query(Appointment)
        .filter(
            Appointment.store == store,
            Appointment.appointment_date == appointment_date,
            Appointment.status.notin_([AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value]),
        )
    )
    if exclude_id is not None:
        q = q.filter(Appointment.id != exclude_id)
    existing = q.all()
    new_start = _time_to_minutes(appointment_time)
    new_end = new_start + duration_minutes
    for appt in existing:
        a_start = _time_to_minutes(appt.appointment_time)
        a_end = a_start + appt.duration_minutes
        if new_start < a_end and new_end > a_start:
            return appt
    return None


def _check_tnr_constraints(
    db: "Session",
    category: str,
    store: str,
    appointment_date: str,
    appointment_time: str,
    exclude_id: int | None = None,
) -> str | None:
    """TNR 预约专项校验：时间段限制（11:00–18:00）和每店每日上限 2 个。违规返回错误字符串，通过返回 None。"""
    if category != AppointmentCategory.tnr.value:
        return None
    # 时间段校验
    t_minutes = _time_to_minutes(appointment_time)
    start_minutes = _time_to_minutes(_TNR_TIME_START)
    end_minutes = _time_to_minutes(_TNR_TIME_END)
    if t_minutes < start_minutes or t_minutes >= end_minutes:
        return f"TNR 手术预约时间须在 {_TNR_TIME_START}–{_TNR_TIME_END} 之间，请重新选择。"
    # 每店每日上限
    q = (
        db.query(Appointment)
        .filter(
            Appointment.category == AppointmentCategory.tnr.value,
            Appointment.store == store,
            Appointment.appointment_date == appointment_date,
            Appointment.status.notin_([AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value]),
        )
    )
    if exclude_id is not None:
        q = q.filter(Appointment.id != exclude_id)
    count = q.count()
    if count >= _TNR_DAILY_MAX:
        return f"该门店 {appointment_date} TNR 手术预约已约满（每日上限 {_TNR_DAILY_MAX} 个），请改约其他日期。"
    return None


def _check_duplicate_application_appointment(
    db: "Session",
    related_application_id: int | None,
    exclude_id: int | None = None,
) -> str | None:
    """检查同一 TNR 申请编号是否已有有效预约（非取消/爽约）。重复则返回错误字符串，否则返回 None。"""
    if not related_application_id:
        return None
    q = (
        db.query(Appointment)
        .filter(
            Appointment.related_application_id == related_application_id,
            Appointment.status.notin_([AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value]),
        )
    )
    if exclude_id is not None:
        q = q.filter(Appointment.id != exclude_id)
    existing = q.first()
    if existing:
        return (
            f"申请 #{related_application_id} 已存在有效预约（预约 #{existing.id}，"
            f"状态：{existing.status}），请先取消原预约再重新预约。"
        )
    return None


async def _resolve_wechat_openid(payload: dict) -> str:
    openid = ((payload or {}).get("openid", "") or "").strip()
    code = ((payload or {}).get("code", "") or "").strip()
    if not openid and code:
        data = wechat_code2session(code)
        openid = (data.get("openid", "") or "").strip()
    if not openid:
        raise HTTPException(400, "missing openid")
    return openid


def _appointment_catalog() -> dict:
    today = datetime.now().date()
    return {
        "stores": list(_CLINIC_STORES),
        "booking_window": {
            "start_date": today.strftime("%Y-%m-%d"),
            "max_days_ahead": _APPOINTMENT_BOOKING_MAX_DAYS_AHEAD,
            "suggestion": "请使用「预约日期 / 预约时间」自主选择到院时段；后续可按门店、医生与服务能力细化排班规则。",
        },
        "categories": [
            {
                "value": AppointmentCategory.tnr.value,
                "label": "流浪动物 TNR",
                "description": "适合流浪动物初诊评估、TNR 手术安排和术后复诊。",
                "booking_tip": "请使用下方「预约日期 / 预约时间」自主选择到院时段；不再展示固定建议时段列表。",
                "supports_related_application": True,
                "time_slots": [],
                "services": [
                    {
                        "name": "TNR 手术安排",
                        "duration_minutes": 60,
                        "description": "用于确认手术时间、门店与到院前准备事项。",
                    },
                ],
            },
            {
                "value": AppointmentCategory.outpatient.value,
                "label": "常规门诊",
                "description": "适合普通门诊、复诊、健康检查及疫苗驱虫等咨询。",
                "booking_tip": "门诊可预约时间：10:00 – 21:00（上午需护理住院动物，晚上 21:00 后不接受新预约）。疫苗/驱虫 30 分钟，其余科目 60 分钟。",
                "supports_related_application": False,
                "time_range": {"start": "10:00", "end": "21:00"},
                "time_slots": [],
                "services": [
                    {"name": "疫苗/驱虫",  "duration_minutes": 30},
                    {"name": "体检",      "duration_minutes": 60},
                    {"name": "呼吸道",    "duration_minutes": 60},
                    {"name": "胃肠道",    "duration_minutes": 60},
                    {"name": "泌尿道",    "duration_minutes": 60},
                    {"name": "皮肤",      "duration_minutes": 60},
                    {"name": "口腔",      "duration_minutes": 60},
                    {"name": "行动异常",   "duration_minutes": 60},
                    {"name": "心内科",    "duration_minutes": 60},
                    {"name": "肾内科",    "duration_minutes": 60},
                ],
            },
            {
                "value": AppointmentCategory.surgery.value,
                "label": "手术预约",
                "description": "适合绝育手术、术前评估和术后复查。",
                "booking_tip": "请使用下方「预约日期 / 预约时间」自主选择到院时段；后续可按手术台与麻醉安排细化。",
                "supports_related_application": False,
                "time_slots": [],
                "services": [
                    {"name": "绝育手术", "duration_minutes": 60, "description": "用于常规绝育手术预约。"},
                    {"name": "术前评估", "duration_minutes": 30, "description": "用于评估手术风险、检查准备条件。"},
                    {"name": "术后复查", "duration_minutes": 20, "description": "用于术后恢复、伤口与用药情况复核。"},
                ],
            },
            {
                "value": AppointmentCategory.beauty.value,
                "label": "美容预约",
                "description": "适合猫/犬洗护与造型服务，支持附加项目选择。",
                "booking_tip": "请选择美容项目及附加服务，并提供宠物体型与毛发信息，以便安排合适的美容师与时段。",
                "supports_related_application": False,
                "time_slots": [],
                "services": [
                    {"name": "猫洗护", "duration_minutes": 60},
                    {"name": "猫造型", "duration_minutes": 60},
                    {"name": "犬洗护", "duration_minutes": 90},
                    {"name": "犬造型", "duration_minutes": 90},
                ],
                "addon_options": ["去浮毛", "SPA", "护发素", "纯手剪", "药浴", "去油"],
                "size_options": ["微小型犬（4kg 以下）", "小型犬（4–10kg）", "中型犬（10–15kg）", "中大型犬（15–25kg）", "大型犬（25kg 以上）"],
                "coat_options": ["长毛", "短毛"],
            },
        ],
        "pet_genders": [
            {"value": "female", "label": "母"},
            {"value": "male", "label": "公"},
            {"value": "unknown", "label": "未知"},
        ],
    }


def _serialize_appointment(row: Appointment) -> dict:
    return {
        "id": row.id,
        "category": row.category,
        "category_zh": _APPOINTMENT_CATEGORY_LABELS.get(row.category, row.category),
        "status": row.status,
        "status_zh": _APPOINTMENT_STATUS_LABELS.get(row.status, row.status),
        "service_name": row.service_name,
        "customer_name": row.customer_name,
        "phone_masked": _mask_phone(row.phone),
        "pet_name": row.pet_name,
        "pet_gender": row.pet_gender,
        "pet_gender_zh": _PET_GENDER_LABELS.get(row.pet_gender, row.pet_gender),
        "store": row.store,
        "appointment_date": row.appointment_date,
        "appointment_time": row.appointment_time,
        "duration_minutes": row.duration_minutes,
        "notes": row.notes or "",
        "related_application_id": row.related_application_id,
        "pet_size": row.pet_size or "",
        "coat_length": row.coat_length or "",
        "addon_services": row.addon_services or "",
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "",
    }


def _assert_application_form_fields(
    *,
    applicant_name: str,
    phone: str,
    address: str,
    clinic_store: str,
    appointment_at: str,
    post_surgery_plan: str,
    id_number: str,
    cat_nickname: str,
    cat_gender: str,
    age_estimate: str,
    health_note: str,
) -> dict[str, str]:
    def need(label: str, raw: str, max_len: int) -> str:
        s = (raw or "").strip()
        if not s:
            raise HTTPException(400, f"请填写{label}。")
        if len(s) > max_len:
            raise HTTPException(400, f"{label}过长。")
        return s

    out: dict[str, str] = {}
    out["applicant_name"] = need("申请人姓名", applicant_name, 120)
    out["phone"] = need("手机号", phone, 40)
    if not re.fullmatch(r"1\d{10}", out["phone"]):
        raise HTTPException(400, "请填写 11 位中国大陆手机号。")
    out["address"] = need("完整地址", address, 500)
    cs = need("预约门店", clinic_store, 80)
    if cs not in _ALLOWED_CLINIC_STORES:
        raise HTTPException(400, "请选择有效的预约门店。")
    out["clinic_store"] = cs
    out["appointment_at"] = (appointment_at or "").strip()[:40]  # 可选字段
    out["post_surgery_plan"] = need("术后打算", post_surgery_plan, 120)
    idn_raw = need("身份证号", id_number, 40)
    idn = idn_raw.upper()
    if len(idn) == 18:
        if not re.fullmatch(r"\d{17}[\dX]", idn):
            raise HTTPException(400, "请填写 18 位身份证号（末位可为 X）。")
    elif len(idn) == 15:
        if not idn.isdigit():
            raise HTTPException(400, "请填写 15 位身份证号。")
    else:
        raise HTTPException(400, "请填写 15 或 18 位身份证号。")
    out["id_number"] = idn
    out["cat_nickname"] = need("流浪猫名字", cat_nickname, 120)
    g = (cat_gender or "").strip().lower()
    if g not in ("male", "female", "unknown"):
        raise HTTPException(400, "请选择猫咪性别。")
    out["cat_gender"] = g
    out["age_estimate"] = need("年龄估计", age_estimate, 80)
    out["health_note"] = need("流浪状况说明", health_note, 8000)
    return out


def _count_valid_apply_images(images: list[UploadFile]) -> int:
    n = 0
    for uf in images:
        if not uf.filename:
            continue
        ext = Path(uf.filename).suffix.lower() or ".jpg"
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            n += 1
    return n


def _assert_application_row_complete(row: Application) -> None:
    _assert_application_form_fields(
        applicant_name=row.applicant_name,
        phone=row.phone,
        address=row.address,
        clinic_store=row.clinic_store,
        appointment_at=row.appointment_at,
        post_surgery_plan=row.post_surgery_plan,
        id_number=row.id_number,
        cat_nickname=row.cat_nickname,
        cat_gender=row.cat_gender,
        age_estimate=row.age_estimate,
        health_note=row.health_note,
    )


@app.post("/api/apply")
async def api_apply(
    request: Request,
    db: Session = Depends(get_db),
    applicant_name: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    clinic_store: str = Form(""),
    appointment_at: str = Form(""),
    location_lat: str = Form(""),
    location_lng: str = Form(""),
    location_address: str = Form(""),
    id_number: str = Form(""),
    post_surgery_plan: str = Form(""),
    cat_nickname: str = Form(""),
    cat_gender: str = Form(...),
    age_estimate: str = Form(""),
    health_note: str = Form(""),
    wechat_openid: str = Form(""),
    agree_ear_tip: str = Form("false"),
    agree_no_pet_fraud: str = Form("false"),
    images: Annotated[Optional[list[UploadFile]], File()] = None,
    videos: Annotated[Optional[list[UploadFile]], File()] = None,
):
    images = images or []
    videos = videos or []
    ok_ear = agree_ear_tip.lower() in ("true", "1", "on", "yes")
    ok_fraud = agree_no_pet_fraud.lower() in ("true", "1", "on", "yes")
    if not ok_ear or not ok_fraud:
        raise HTTPException(400, "请勾选同意剪耳标记与承诺非家养猫冒充。")
    if _count_valid_apply_images(images) < 1:
        raise HTTPException(400, "请至少上传 1 张申请照片。")

    f = _assert_application_form_fields(
        applicant_name=applicant_name,
        phone=phone,
        address=address,
        clinic_store=clinic_store,
        appointment_at=appointment_at,
        post_surgery_plan=post_surgery_plan,
        id_number=id_number,
        cat_nickname=cat_nickname,
        cat_gender=cat_gender,
        age_estimate=age_estimate,
        health_note=health_note,
    )

    app_row = Application(
        applicant_name=f["applicant_name"],
        phone=f["phone"],
        wechat_openid=wechat_openid.strip(),
        clinic_store=f["clinic_store"],
        appointment_at=f["appointment_at"],
        location_lat=location_lat.strip(),
        location_lng=location_lng.strip(),
        location_address=location_address.strip()[:400],
        id_number=f["id_number"],
        post_surgery_plan=f["post_surgery_plan"],
        address=f["address"],
        cat_nickname=f["cat_nickname"],
        cat_gender=f["cat_gender"],
        age_estimate=f["age_estimate"],
        weight_estimate="",
        health_note=f["health_note"],
        agree_ear_tip=ok_ear,
        agree_no_pet_fraud=ok_fraud,
        status=ApplicationStatus.pending_ai.value,
    )
    db.add(app_row)
    db.flush()

    aid = app_row.id
    base = Path(settings.upload_dir) / str(aid)
    base.mkdir(parents=True, exist_ok=True)

    image_paths: list[Path] = []
    video_paths: list[Path] = []

    for uf in images:
        if not uf.filename:
            continue
        ext = Path(uf.filename).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        dest = base / f"app_img_{secrets.token_hex(6)}{ext}"
        dest.write_bytes(await uf.read())
        db.add(
            MediaFile(
                application_id=aid,
                kind=MediaKind.application_image.value,
                stored_path=str(dest),
                original_name=uf.filename,
            )
        )
        image_paths.append(dest)

    for uf in videos:
        if not uf.filename:
            continue
        ext = Path(uf.filename).suffix.lower() or ".mp4"
        if ext not in (".mp4", ".webm", ".mov", ".mkv"):
            continue
        dest = base / f"app_vid_{secrets.token_hex(6)}{ext}"
        dest.write_bytes(await uf.read())
        db.add(
            MediaFile(
                application_id=aid,
                kind=MediaKind.application_video.value,
                stored_path=str(dest),
                original_name=uf.filename,
            )
        )
        video_paths.append(dest)

    db.commit()
    db.refresh(app_row)

    ai_result = await review_application_media(image_paths, video_paths)

    app_row.ai_raw_json = json.dumps(ai_result, ensure_ascii=False)
    app_row.ai_is_likely_stray = ai_result.get("is_likely_stray")
    conf = ai_result.get("confidence")
    app_row.ai_confidence = float(conf) if conf is not None else None

    new_status, auto_ok = apply_auto_status_from_ai(ai_result)
    app_row.status = new_status
    db.commit()

    if auto_ok:
        notify_application_result(db, aid, app_row.phone, app_row.applicant_name, approved=True, extra="系统根据图像辅助判断已完成预审通过，到院后仍需工作人员核对猫只身份。")
        push_application_result(
            db,
            application_id=aid,
            openid=app_row.wechat_openid,
            applicant_name=app_row.applicant_name,
            status_text="审核已通过",
            phone_masked=app_row.phone,
            note="请按约定时间携带猫咪到院",
            submitted_at=app_row.created_at.strftime("%Y-%m-%d %H:%M") if app_row.created_at else "",
            action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    elif new_status == ApplicationStatus.pre_approved.value:
        push_application_result(
            db,
            application_id=aid,
            openid=app_row.wechat_openid,
            applicant_name=app_row.applicant_name,
            status_text="预通过（待复核）",
            phone_masked=app_row.phone,
            note="医院将尽快人工复核，请保持手机畅通",
            submitted_at=app_row.created_at.strftime("%Y-%m-%d %H:%M") if app_row.created_at else "",
            action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    elif new_status == ApplicationStatus.pending_manual.value:
        push_pending_manual_notice(
            db,
            application_id=aid,
            openid=app_row.wechat_openid,
            applicant_name=app_row.applicant_name,
            submitted_at=app_row.created_at.strftime("%Y-%m-%d %H:%M") if app_row.created_at else "",
        )

    return {
        "id": aid,
        "status": app_row.status,
        "ai_summary": {
            "is_likely_stray": app_row.ai_is_likely_stray,
            "confidence": app_row.ai_confidence,
            "auto_approved": auto_ok,
        },
        "message": "提交成功。若未自动通过，请耐心等待医院人工审核，通过后将以登记方式通知您。"
    }


@app.post("/api/apply/create")
async def api_apply_create(
    request: Request,
    db: Session = Depends(get_db),
    applicant_name: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    clinic_store: str = Form(""),
    appointment_at: str = Form(""),
    location_lat: str = Form(""),
    location_lng: str = Form(""),
    location_address: str = Form(""),
    id_number: str = Form(""),
    post_surgery_plan: str = Form(""),
    cat_nickname: str = Form(""),
    cat_gender: str = Form(...),
    age_estimate: str = Form(""),
    health_note: str = Form(""),
    wechat_openid: str = Form(""),
    agree_ear_tip: str = Form("false"),
    agree_no_pet_fraud: str = Form("false"),
):
    ok_ear = agree_ear_tip.lower() in ("true", "1", "on", "yes")
    ok_fraud = agree_no_pet_fraud.lower() in ("true", "1", "on", "yes")
    if not ok_ear or not ok_fraud:
        raise HTTPException(400, "请勾选同意剪耳标记与承诺非家养猫冒充。")

    f = _assert_application_form_fields(
        applicant_name=applicant_name,
        phone=phone,
        address=address,
        clinic_store=clinic_store,
        appointment_at=appointment_at,
        post_surgery_plan=post_surgery_plan,
        id_number=id_number,
        cat_nickname=cat_nickname,
        cat_gender=cat_gender,
        age_estimate=age_estimate,
        health_note=health_note,
    )

    app_row = Application(
        applicant_name=f["applicant_name"],
        phone=f["phone"],
        wechat_openid=wechat_openid.strip(),
        clinic_store=f["clinic_store"],
        appointment_at=f["appointment_at"],
        location_lat=location_lat.strip(),
        location_lng=location_lng.strip(),
        location_address=location_address.strip()[:400],
        id_number=f["id_number"],
        post_surgery_plan=f["post_surgery_plan"],
        address=f["address"],
        cat_nickname=f["cat_nickname"],
        cat_gender=f["cat_gender"],
        age_estimate=f["age_estimate"],
        weight_estimate="",
        health_note=f["health_note"],
        agree_ear_tip=ok_ear,
        agree_no_pet_fraud=ok_fraud,
        status=ApplicationStatus.draft.value,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)

    base = Path(settings.upload_dir) / str(app_row.id)
    base.mkdir(parents=True, exist_ok=True)
    return {"id": app_row.id, "status": app_row.status, "message": "申请已创建，请继续上传照片/视频后提交。"}


@app.post("/api/apply/{app_id}/upload-media")
async def api_apply_upload_media(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    kind: str = Form("image"),  # image / video
    file: UploadFile = File(...),
):
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404, "not found")

    base = Path(settings.upload_dir) / str(app_id)
    base.mkdir(parents=True, exist_ok=True)

    if kind == "video":
        ext = _video_ext(file.filename or "")
        dest = base / f"app_vid_{secrets.token_hex(6)}{ext}"
        dest.write_bytes(await file.read())
        m = MediaFile(
            application_id=app_id,
            kind=MediaKind.application_video.value,
            stored_path=str(dest),
            original_name=file.filename or "",
        )
    else:
        ext = _image_ext(file.filename or "")
        dest = base / f"app_img_{secrets.token_hex(6)}{ext}"
        dest.write_bytes(await file.read())
        m = MediaFile(
            application_id=app_id,
            kind=MediaKind.application_image.value,
            stored_path=str(dest),
            original_name=file.filename or "",
        )
    db.add(m)
    db.commit()
    db.refresh(m)
    return {"ok": True, "media_id": m.id}


@app.post("/api/apply/{app_id}/finalize")
async def api_apply_finalize(app_id: int, request: Request, db: Session = Depends(get_db)):
    row = (
        db.query(Application)
        .options(selectinload(Application.media))
        .filter(Application.id == app_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "not found")

    _assert_application_row_complete(row)

    image_paths = [Path(m.stored_path) for m in (row.media or []) if m.kind == MediaKind.application_image.value]
    video_paths = [Path(m.stored_path) for m in (row.media or []) if m.kind == MediaKind.application_video.value]
    if not image_paths:
        raise HTTPException(400, "请至少上传 1 张申请照片。")

    row.status = ApplicationStatus.pending_ai.value
    db.commit()

    ai_result = await review_application_media(image_paths, video_paths)
    row.ai_raw_json = json.dumps(ai_result, ensure_ascii=False)
    row.ai_is_likely_stray = ai_result.get("is_likely_stray")
    conf = ai_result.get("confidence")
    row.ai_confidence = float(conf) if conf is not None else None

    new_status, auto_ok = apply_auto_status_from_ai(ai_result)
    row.status = new_status
    db.commit()

    if auto_ok:
        notify_application_result(db, app_id, row.phone, row.applicant_name, approved=True, extra="系统根据图像辅助判断已完成预审通过，到院后仍需工作人员核对猫只身份。")
        push_application_result(
            db,
            application_id=app_id,
            openid=row.wechat_openid,
            applicant_name=row.applicant_name,
            status_text="审核已通过",
            phone_masked=row.phone,
            note="请按约定时间携带猫咪到院",
            submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    elif new_status == ApplicationStatus.pre_approved.value:
        push_application_result(
            db,
            application_id=app_id,
            openid=row.wechat_openid,
            applicant_name=row.applicant_name,
            status_text="预通过（待复核）",
            phone_masked=row.phone,
            note="医院将尽快人工复核，请保持手机畅通",
            submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    elif new_status == ApplicationStatus.pending_manual.value:
        push_pending_manual_notice(
            db,
            application_id=app_id,
            openid=row.wechat_openid,
            applicant_name=row.applicant_name,
            submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        )

    _STATUS_ZH = {
        "draft": "草稿", "pending_ai": "系统处理中", "pending_manual": "待人工审核",
        "pre_approved": "预通过（待复核）", "approved": "已通过", "scheduled": "已预约",
        "arrived_verified": "到院已核对", "surgery_completed": "手术已完成",
        "rejected": "未通过", "cancelled": "已取消", "no_show": "爽约",
    }
    return {
        "id": row.id,
        "status": row.status,
        "status_zh": _STATUS_ZH.get(row.status, row.status),
        "ai_summary": {
            "is_likely_stray": row.ai_is_likely_stray,
            "confidence": row.ai_confidence,
            "auto_approved": auto_ok,
        },
        "message": "提交成功。若未自动通过，请耐心等待医院人工审核，通过后将以登记方式通知您。",
    }


@app.get("/api/app/{app_id}/status")
async def api_app_status(app_id: int, db: Session = Depends(get_db)):
    row = (
        db.query(Application)
        .options(selectinload(Application.notifications))
        .filter(Application.id == app_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "not found")

    def mask_phone(s: str) -> str:
        t = (s or "").strip()
        if len(t) < 7:
            return t
        return t[:3] + "****" + t[-4:]

    notes = (row.reject_reason or "").strip()
    if len(notes) > 80:
        notes = notes[:80] + "…"

    return {
        "id": row.id,
        "status": row.status,
        "clinic_store": row.clinic_store,
        "appointment_at": row.appointment_at,
        "applicant_name": row.applicant_name or "",
        "phone": row.phone or "",
        "phone_masked": mask_phone(row.phone),
        "cat_nickname": row.cat_nickname or "",
        "cat_gender": row.cat_gender or "",
        "age_estimate": row.age_estimate or "",
        "health_note": row.health_note or "",
        "address": row.address or "",
        "note": notes,
        "reject_reason": notes,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "",
        "notifications": [
            {
                "channel": n.channel,
                "success": bool(n.success),
                "created_at": n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else "",
            }
            for n in (row.notifications or [])
        ],
    }


@app.get("/admin", response_class=HTMLResponse)
async def page_admin(request: Request, db: Session = Depends(get_db)):
    if not _admin_ok(request):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)},
        )
    qp = request.query_params
    status = (qp.get("status") or "").strip()
    store = (qp.get("store") or "").strip()
    qtext = (qp.get("q") or "").strip()
    consent = (qp.get("consent") or "").strip()  # any/true/false
    verified = (qp.get("verified") or "").strip()  # any/true/false
    has_media = (qp.get("has_media") or "").strip()  # any/true
    date_from = (qp.get("from") or "").strip()  # YYYY-MM-DD
    date_to = (qp.get("to") or "").strip()  # YYYY-MM-DD
    page = int((qp.get("page") or "1").strip() or 1)
    page_size = int((qp.get("page_size") or "30").strip() or 30)
    page = max(1, page)
    page_size = min(max(10, page_size), 100)

    base_q = db.query(Application)
    if status:
        base_q = base_q.filter(Application.status == status)
    if store:
        base_q = base_q.filter(Application.clinic_store == store)
    if consent == "true":
        base_q = base_q.filter(Application.showcase_consent.is_(True))
    elif consent == "false":
        base_q = base_q.filter(Application.showcase_consent.is_(False))
    if verified == "true":
        base_q = base_q.filter(Application.staff_cat_verified.is_(True))
    elif verified == "false":
        base_q = base_q.filter(Application.staff_cat_verified.is_(False))
    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            base_q = base_q.filter(Application.created_at >= dt)
        except Exception:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d")
            base_q = base_q.filter(Application.created_at < (dt + timedelta(days=1)))
        except Exception:
            pass
    if qtext:
        if qtext.isdigit():
            base_q = base_q.filter(or_(Application.id == int(qtext), Application.phone.contains(qtext)))
        else:
            base_q = base_q.filter(
                or_(
                    Application.applicant_name.contains(qtext),
                    Application.phone.contains(qtext),
                    Application.address.contains(qtext),
                    Application.cat_nickname.contains(qtext),
                )
            )

    # 统计（全量，不受筛选影响）
    overall_by_status = dict(db.query(Application.status, func.count(Application.id)).group_by(Application.status).all())
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_new = db.query(func.count(Application.id)).filter(Application.created_at >= today0).scalar() or 0
    pending_todo = (
        db.query(func.count(Application.id))
        .filter(Application.status.in_([ApplicationStatus.pending_manual.value, ApplicationStatus.pre_approved.value]))
        .scalar()
        or 0
    )

    total = base_q.count()

    rows = (
        base_q.options(selectinload(Application.media))
        .order_by(Application.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    if has_media == "true":
        rows = [a for a in rows if any(m.kind in (MediaKind.application_image.value, MediaKind.application_video.value) for m in (a.media or []))]
        total = len(rows) if page == 1 else total
    try:
        backup_files = list_backup_zips()
    except Exception:
        backup_files = []
    appointments = (
        db.query(Appointment)
        .options(selectinload(Appointment.application))
        .order_by(Appointment.created_at.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "title": "TNR 审核与手术登记",
            "apps": rows,
            "appointments": appointments,
            "csrf_token": _get_csrf_token(request),
            "backup_files": backup_files,
            "filters": {
                "status": status,
                "store": store,
                "q": qtext,
                "consent": consent,
                "verified": verified,
                "has_media": has_media,
                "from": date_from,
                "to": date_to,
                "page": page,
                "page_size": page_size,
            },
            "stats": {"overall_by_status": overall_by_status, "today_new": today_new, "pending_todo": pending_todo, "total": total},
        },
    )


def _admin_appointment_redirect_base(redirect_after: str) -> str:
    """预约表单提交后回到哪一页；仅允许固定路径，避免开放重定向。"""
    if (redirect_after or "").strip().lower() == "appointments":
        return "/admin/appointments"
    return "/admin"


def _admin_appointment_redirect(
    next_val: str | None,
    *,
    ok: str | None = None,
    err: str | None = None,
    anchor: str | None = None,
) -> RedirectResponse:
    base = "/admin/appointments"
    parts: list[str] = []
    if ok:
        parts.append(f"appointment_ok={ok}")
    if err:
        parts.append("appointment_err=" + quote(str(err)[:200], safe=""))
    url = base + ("?" + "&".join(parts) if parts else "")
    if anchor:
        url += f"#{anchor}"
    return RedirectResponse(url, status_code=303)


# ── 美容时长估算 ──────────────────────────────────────────────────────────────
# 美容师工作时间（分钟，从午夜起算）
_BEAUTY_WORK_START = 13 * 60   # 13:00
_BEAUTY_WORK_END   = 22 * 60   # 22:00
_BEAUTY_SLOT_STEP  = 30        # 每 30 分钟一个可选时段


def _calc_beauty_duration(service_name: str, pet_size: str, coat_length: str, addon_services: str = "") -> int:
    """
    根据美容项目、体型、毛发长度、附加服务估算占用分钟数。
    附加服务用逗号分隔，每项 +30 分钟。
    """
    sn    = service_name or ""
    sz    = pet_size     or ""
    is_long  = (coat_length or "") == "长毛"
    is_wash  = "洗护" in sn
    # is_groom = "造型" in sn  # 不需要显式判断，else 分支即为造型

    base = 60  # fallback

    if "犬" in sn:
        if "微小型" in sz:
            base = 30 if is_wash else 90
        elif "小型犬" in sz:           # 注意：elif 保证 "微小型" 已被排除
            base = 60 if is_wash else 120
        elif "中大型" in sz:
            base = (120 if is_long else 90) if is_wash else 180
        elif "中型犬" in sz:
            base = (90 if is_long else 60) if is_wash else 150
        elif "大型犬" in sz:           # "中大型犬" 已被前面 elif 排除
            base = (150 if is_long else 120) if is_wash else 210
    elif "猫" in sn:
        if "大型猫" in sz:
            base = (150 if is_long else 120) if is_wash else 150
        elif "中型猫" in sz:
            base = (120 if is_long else 90) if is_wash else 120
        elif "小型猫" in sz:
            base = (90 if is_long else 60) if is_wash else 120

    # 附加服务：每项 +30 分钟
    if addon_services:
        addon_count = len([a for a in addon_services.split(",") if a.strip()])
        base += addon_count * 30

    return base


def _beauty_slots_for_date(
    db: "Session",
    store: str,
    date_str: str,
    new_service: str,
    new_duration: int,
) -> list[str]:
    """
    返回指定日期门店美容师可接受的开始时间列表（HH:MM 字符串）。
    实现猫进烘干机时可并发做 ≤60min 犬洗护的规则。
    """
    _inactive = {AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value}
    bookings = (
        db.query(Appointment)
        .filter(
            Appointment.appointment_date == date_str,
            Appointment.store == store,
            Appointment.category.in_(["beauty", "grooming", "washcare"]),
            Appointment.status.notin_(list(_inactive)),
        )
        .all()
    )

    # 解析已有预约 → 时间块（分钟）
    dog_blocks: list[tuple[int, int]] = []       # 犬：完全占用美容师
    cat_active: list[tuple[int, int]] = []       # 猫主动护理阶段
    cat_dryer:  list[tuple[int, int]] = []       # 猫烘干机阶段（可并发 ≤60min 犬服务）

    for b in bookings:
        try:
            bh, bm = b.appointment_time.split(":")
            b_s = int(bh) * 60 + int(bm)
            b_e = b_s + (b.duration_minutes or 60)
        except Exception:
            continue
        sn = b.service_name or ""
        if "猫" in sn:
            # 主动护理阶段固定为最初60分钟（洗猫+护理+冲洗）
            # 之后才是烘干机阶段（此阶段可并发小型犬服务）
            cat_active_end = b_s + 60
            if cat_active_end < b_e:
                cat_active.append((b_s, cat_active_end))
                cat_dryer.append((cat_active_end, b_e))
            else:
                # 总时长 ≤60 分钟：全程主动护理，无烘干机窗口
                cat_active.append((b_s, b_e))
        else:
            dog_blocks.append((b_s, b_e))

    is_new_dog = "犬" in new_service
    is_new_cat = "猫" in new_service

    def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
        return s1 < e2 and e1 > s2

    def _slot_ok(start: int) -> bool:
        end = start + new_duration
        if start < _BEAUTY_WORK_START or end > _BEAUTY_WORK_END:
            return False
        # 犬类预约：不能与 dog_blocks 重叠
        for bs, be in dog_blocks:
            if _overlaps(start, end, bs, be):
                return False
        if is_new_dog:
            # 不能与猫的主动护理阶段重叠
            for bs, be in cat_active:
                if _overlaps(start, end, bs, be):
                    return False
            # 烘干机窗口：仅 ≤60min 且完全落在窗口内才允许
            for dws, dwe in cat_dryer:
                if _overlaps(start, end, dws, dwe):
                    if not (new_duration <= 60 and start >= dws and end <= dwe):
                        return False
        elif is_new_cat:
            # 猫：需要完全空闲（不能与任何阶段重叠）
            for bs, be in cat_active:
                if _overlaps(start, end, bs, be):
                    return False
            for dws, dwe in cat_dryer:
                if _overlaps(start, end, dws, dwe):
                    return False
        return True

    slots = []
    t = _BEAUTY_WORK_START
    while t + new_duration <= _BEAUTY_WORK_END:
        if _slot_ok(t):
            slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += _BEAUTY_SLOT_STEP
    return slots


# ── 门诊/手术容量规则 ─────────────────────────────────────────────────────────
# 每门店、每时段（上午/下午/晚上）的总容量单位数上限
# 各时段容量上限（按时长等比，每小时 3 单位）
# 上午 3h=9，下午 6h=18，晚上 4h=12
_SLOT_CAPACITY = {
    "morning":   9,
    "afternoon": 18,
    "evening":   12,
    "other":     9,
}
# 疫苗/驱虫 = 1 单位；普通门诊 = 3 单位；TNR/手术 = 4 单位；美容 = 0（不参与）

_OUTPATIENT_SERVICES = [
    "疫苗/驱虫", "体检", "呼吸道", "胃肠道", "泌尿道",
    "皮肤", "口腔", "行动异常", "心内科", "肾内科",
]
_VACCINE_KEYWORDS = ("疫苗", "驱虫")

_SLOT_BOUNDS = {
    "morning":   ("09:00", "12:00"),
    "afternoon": ("12:00", "18:00"),
    "evening":   ("18:00", "22:00"),
    "other":     ("00:00", "23:59"),
}
_SLOT_NAME_ZH = {"morning": "上午", "afternoon": "下午", "evening": "晚上", "other": "该时段"}


def _capacity_units(category: str, service_name: str) -> int:
    """返回该预约消耗的容量单位（0 = 不纳入容量管控）。
    单位换算：
      疫苗/驱虫 = 1 单位
      普通门诊   = 3 单位
      TNR/手术   = 4 单位（术前检查少，相对快，2台=8单位 ≤ 上限9）
      美容/洗护  = 0 单位（不参与）
    """
    if category in ("tnr", "surgery"):
        return 4
    if category == "outpatient":
        sn = service_name or ""
        if any(kw in sn for kw in _VACCINE_KEYWORDS):
            return 1
        return 3
    return 0  # beauty / grooming / washcare 不参与容量管控


_OUTPATIENT_TIME_START = "10:00"   # 门诊最早开始时间（上午护理住院动物）
_OUTPATIENT_TIME_END   = "21:00"   # 门诊最晚开始时间（避免加班）


def _check_outpatient_time(category: str, appointment_time: str) -> str | None:
    """门诊/疫苗时间限制：10:00 之后、21:00 之前。返回错误描述或 None。"""
    if category != AppointmentCategory.outpatient.value:
        return None
    t = (appointment_time or "")[:5]
    if t < _OUTPATIENT_TIME_START:
        return f"门诊预约最早从 {_OUTPATIENT_TIME_START} 开始（上午需护理住院动物），请选择 {_OUTPATIENT_TIME_START} 或之后的时间。"
    if t >= _OUTPATIENT_TIME_END:
        return f"门诊预约最晚在 {_OUTPATIENT_TIME_END} 之前，请选择更早的时间。"
    return None


def _check_slot_capacity(
    db: "Session",
    store: str,
    appointment_date: str,
    appointment_time: str,
    category: str,
    service_name: str,
    exclude_id: int | None = None,
) -> str | None:
    """返回错误提示（str）或 None（通过）。"""
    new_units = _capacity_units(category, service_name)
    if new_units == 0:
        return None  # 不受容量限制

    slot_key = _appt_time_slot(appointment_time)
    t_from, t_to = _SLOT_BOUNDS.get(slot_key, ("00:00", "23:59"))
    slot_zh = _SLOT_NAME_ZH.get(slot_key, "该时段")

    _inactive = {AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value}
    q = (
        db.query(Appointment)
        .filter(
            Appointment.store == store,
            Appointment.appointment_date == appointment_date,
            Appointment.appointment_time >= t_from,
            Appointment.appointment_time < t_to,
            Appointment.status.notin_(list(_inactive)),
        )
    )
    if exclude_id:
        q = q.filter(Appointment.id != exclude_id)

    slot_max = _SLOT_CAPACITY.get(slot_key, 9)
    used = sum(_capacity_units(a.category, a.service_name or "") for a in q.all())
    avail = slot_max - used

    if avail < new_units:
        type_zh = (
            "手术" if category in ("tnr", "surgery")
            else ("疫苗/驱虫" if new_units == 1 else "门诊")
        )
        if avail <= 0:
            return f"该门店 {slot_zh}时段预约容量已满（上限 {slot_max} 单位），请选择其他时段或日期。"
        return (
            f"该门店 {slot_zh}时段剩余容量不足：剩余 {avail} 单位，"
            f"此{type_zh}需要 {new_units} 单位，请选择其他时段或日期。"
        )
    return None


def _appt_time_slot(time_str: str) -> str:
    try:
        h = int((time_str or "00:00").split(":")[0])
        if 9 <= h < 12:
            return "morning"
        if 12 <= h < 18:
            return "afternoon"
        if 18 <= h < 22:
            return "evening"
        return "other"
    except Exception:
        return "other"


@app.get("/api/admin/pending-count")
async def api_admin_pending_count(request: Request, db: Session = Depends(get_db)):
    """返回待确认预约数量（仅限已登录后台）"""
    if not request.session.get("admin"):
        return {"count": 0}
    from datetime import date as _date
    today_str = _date.today().isoformat()
    count = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.status == AppointmentStatus.pending.value,
            Appointment.appt_date >= today_str,
        )
        .scalar()
    ) or 0
    return {"count": count}


@app.get("/admin/appointments", response_class=HTMLResponse)
async def page_admin_appointments(
    request: Request,
    db: Session = Depends(get_db),
    df: str = Query(""),        # date_from  YYYY-MM-DD
    dt: str = Query(""),        # date_to    YYYY-MM-DD
    preset: str = Query("today"),  # today / 3days / week / month / custom
    appt_status: str = Query(""),
    appt_store: str = Query(""),
    appt_category: str = Query(""),
):
    if not _admin_ok(request):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)},
        )

    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── 日期范围推导 ──────────────────────────────────────────
    _preset = (preset or "today").strip()
    if _preset == "3days":
        df_d, dt_d = today, today + timedelta(days=2)
    elif _preset == "week":
        df_d, dt_d = today, today + timedelta(days=6)
    elif _preset == "month":
        df_d, dt_d = today, today + timedelta(days=29)
    elif _preset == "custom":
        try:
            df_d = datetime.strptime(df, "%Y-%m-%d").date() if df else today
        except ValueError:
            df_d = today
        try:
            dt_d = datetime.strptime(dt, "%Y-%m-%d").date() if dt else df_d + timedelta(days=6)
        except ValueError:
            dt_d = df_d + timedelta(days=6)
        if dt_d < df_d:
            dt_d = df_d
    else:  # today (default)
        _preset = "today"
        df_d, dt_d = today, today

    df_str = df_d.strftime("%Y-%m-%d")
    dt_str = dt_d.strftime("%Y-%m-%d")

    # ── 查询 ──────────────────────────────────────────────────
    q = (
        db.query(Appointment)
        .options(selectinload(Appointment.application))
        .filter(
            Appointment.appointment_date >= df_str,
            Appointment.appointment_date <= dt_str,
        )
    )
    if appt_status:
        q = q.filter(Appointment.status == appt_status)
    if appt_store:
        q = q.filter(Appointment.store == appt_store)
    if appt_category:
        q = q.filter(Appointment.category == appt_category)

    appointments_raw = q.order_by(
        Appointment.appointment_date, Appointment.appointment_time
    ).all()

    # ── 按日期→部门→时段分组 ──────────────────────────────────
    _SLOT_ORDER = ["morning", "afternoon", "evening", "other"]
    _SLOT_LABELS = {
        "morning":   "上午  09:00 – 12:00",
        "afternoon": "下午  12:00 – 18:00",
        "evening":   "晚上  18:00 – 22:00",
        "other":     "其他时段",
    }
    _WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    _BEAUTY_CATS = {"beauty", "grooming", "washcare"}
    _DEPT_ORDER  = ["medical", "beauty"]
    _DEPT_LABELS = {"medical": "医疗", "beauty": "美容"}

    # date → dept → slot → [appts]
    date_buckets: dict[str, dict[str, dict[str, list]]] = {}
    for appt in appointments_raw:
        d = appt.appointment_date
        if d not in date_buckets:
            date_buckets[d] = {
                "medical": {s: [] for s in _SLOT_ORDER},
                "beauty":  {s: [] for s in _SLOT_ORDER},
            }
        dept = "beauty" if (appt.category or "") in _BEAUTY_CATS else "medical"
        date_buckets[d][dept][_appt_time_slot(appt.appointment_time)].append(appt)

    def _date_display(d_str: str) -> str:
        try:
            d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            wd = _WEEKDAYS[d_obj.weekday()]
            label = f"{d_obj.month}月{d_obj.day}日（{wd}）"
            if d_str == today_str:
                label += " · 今天"
            elif d_str == tomorrow_str:
                label += " · 明天"
            return label
        except Exception:
            return d_str

    grouped_appointments = []
    _inactive = {AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value}
    for d_str in sorted(date_buckets):
        all_day: list = []
        dept_groups = []
        for dept_key in _DEPT_ORDER:
            dept_appts = [a for sl in _SLOT_ORDER for a in date_buckets[d_str][dept_key][sl]]
            all_day.extend(dept_appts)
            dept_slots = [
                {"key": sl, "label": _SLOT_LABELS[sl], "appts": date_buckets[d_str][dept_key][sl]}
                for sl in _SLOT_ORDER
                if date_buckets[d_str][dept_key][sl]
            ]
            if dept_slots:
                dept_groups.append({
                    "key":   dept_key,
                    "label": _DEPT_LABELS[dept_key],
                    "slots": dept_slots,
                    "total": len(dept_appts),
                })

        active   = [a for a in all_day if a.status not in _inactive]
        pending  = [a for a in all_day if a.status == AppointmentStatus.pending.value]
        tnr_ct   = sum(1 for a in active if a.category == AppointmentCategory.tnr.value)
        grouped_appointments.append({
            "date":          d_str,
            "date_display":  _date_display(d_str),
            "is_today":      d_str == today_str,
            "total":         len(all_day),
            "active_count":  len(active),
            "pending_count": len(pending),
            "tnr_count":     tnr_ct,
            "dept_groups":   dept_groups,
        })

    # ── 顶部统计（始终基于今日全量，不受筛选影响） ────────────
    _today_appts = (
        db.query(Appointment)
        .filter(
            Appointment.appointment_date == today_str,
            Appointment.status.notin_(list(_inactive)),
        )
        .all()
    )
    stats = {
        "today_active":  len(_today_appts),
        "pending_total": db.query(Appointment)
            .filter(
                Appointment.status == AppointmentStatus.pending.value,
                Appointment.appointment_date >= today_str,
            ).count(),
        "tnr_today":     sum(1 for a in _today_appts if a.category == AppointmentCategory.tnr.value),
        "tnr_daily_max": _TNR_DAILY_MAX,
    }

    return templates.TemplateResponse(
        "admin_appointments.html",
        {
            "request":              request,
            "title":                "预约管理",
            "appointments":         appointments_raw,          # 创建表单 JS 仍使用
            "grouped_appointments": grouped_appointments,
            "stats":                stats,
            "filters": {
                "date_from": df_str,
                "date_to":   dt_str,
                "preset":    _preset,
                "status":    appt_status,
                "store":     appt_store,
                "category":  appt_category,
            },
            "stores":     list(_CLINIC_STORES),
            "csrf_token": _get_csrf_token(request),
        },
    )


@app.get("/admin/api/tnr-application/{application_id}/for-appointment")
async def admin_api_tnr_application_for_appointment(
    application_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """后台新建预约时：按 TNR 申请编号拉取联系人、宠物、门店等用于自动填充（需已登录）。"""
    require_admin(request)
    row = db.get(Application, application_id)
    if not row:
        raise HTTPException(404, detail="申请不存在")
    store = (row.clinic_store or "").strip()
    if store not in _ALLOWED_CLINIC_STORES:
        store = ""
    g = (row.cat_gender or "unknown").strip().lower()
    if g not in ("male", "female", "unknown"):
        g = "unknown"
    appt_date = ""
    raw_at = (row.appointment_at or "").strip()
    if len(raw_at) >= 10 and raw_at[4] == "-" and raw_at[7] == "-":
        try:
            datetime.strptime(raw_at[:10], "%Y-%m-%d")
            appt_date = raw_at[:10]
        except ValueError:
            pass
    return {
        "ok": True,
        "application_id": row.id,
        "customer_name": (row.applicant_name or "").strip(),
        "phone": (row.phone or "").strip(),
        "pet_name": (row.cat_nickname or "").strip(),
        "pet_gender": g,
        "store": store,
        "appointment_date_suggestion": appt_date,
    }


@app.post("/admin/login")
async def admin_login(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    _require_csrf(request, csrf_token)
    username = username.strip()

    # 优先查 DB 账号
    user = db.query(AdminUser).filter(AdminUser.username == username, AdminUser.is_active == True).first()
    if user and _pwd_ctx.verify(password, user.password_hash):
        request.session["admin"] = True
        request.session["admin_role"] = user.role
        request.session["admin_username"] = user.username
        return RedirectResponse("/admin", status_code=303)

    # 兜底：环境变量密码（用于迁移期 / 紧急登录，用户名须为 admin）
    if username == "admin" and password == settings.admin_password:
        request.session["admin"] = True
        request.session["admin_role"] = "superadmin"
        request.session["admin_username"] = "admin"
        return RedirectResponse("/admin", status_code=303)

    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "title": "医院后台登录", "error": "账号或密码不正确", "csrf_token": _get_csrf_token(request)},
        status_code=401,
    )


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin", status_code=303)


# ── 账号管理（仅 superadmin）────────────────────────────────────────────

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    db: Session = Depends(get_db),
    msg: str = Query(""),
    err: str = Query(""),
):
    if not _admin_ok(request):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)},
        )
    require_superadmin(request)
    all_users = db.query(AdminUser).order_by(AdminUser.created_at).all()
    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "title": "账号管理",
            "active_users": [u for u in all_users if u.is_active],
            "inactive_users": [u for u in all_users if not u.is_active],
            "current_username": request.session.get("admin_username", ""),
            "csrf_token": _get_csrf_token(request),
            "msg": msg,
            "err": err,
        },
    )


@app.post("/admin/users/create", name="admin_users_create")
async def admin_users_create(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("staff"),
    csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    username = username.strip()
    if not username or not password:
        return RedirectResponse("/admin/users?err=用户名和密码不能为空", status_code=303)
    if len(password) < 6:
        return RedirectResponse("/admin/users?err=密码不能少于6位", status_code=303)
    if role not in ("superadmin", "staff"):
        role = "staff"
    existing = db.query(AdminUser).filter(AdminUser.username == username).first()
    if existing:
        return RedirectResponse(f"/admin/users?err=用户名已存在：{username}", status_code=303)
    db.add(AdminUser(username=username, password_hash=_pwd_ctx.hash(password), role=role, is_active=True))
    _audit(db, request, "admin_user_create", application_id=None, detail={"username": username, "role": role})
    db.commit()
    return RedirectResponse(f"/admin/users?msg=已创建账号：{username}", status_code=303)


@app.post("/admin/users/{user_id}/toggle", name="admin_users_toggle")
async def admin_users_toggle(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(404)
    if user.username == request.session.get("admin_username", ""):
        return RedirectResponse("/admin/users?err=不能停用当前登录账号", status_code=303)
    user.is_active = not user.is_active
    _audit(db, request, "admin_user_toggle", application_id=None, detail={"username": user.username, "active": user.is_active})
    db.commit()
    status_zh = "启用" if user.is_active else "停用"
    return RedirectResponse(f"/admin/users?msg=已{status_zh}账号：{user.username}", status_code=303)


@app.post("/admin/users/{user_id}/reset-password", name="admin_users_reset_password")
async def admin_users_reset_password(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    new_password: str = Form(...),
    csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    if not new_password or len(new_password) < 6:
        return RedirectResponse("/admin/users?err=新密码不能少于6位", status_code=303)
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(404)
    user.password_hash = _pwd_ctx.hash(new_password)
    _audit(db, request, "admin_user_reset_password", application_id=None, detail={"username": user.username})
    db.commit()
    return RedirectResponse(f"/admin/users?msg=已重置密码：{user.username}", status_code=303)


@app.post("/admin/users/{user_id}/set-role", name="admin_users_set_role")
async def admin_users_set_role(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Form(...),
    csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    if role not in ("superadmin", "staff"):
        return RedirectResponse("/admin/users?err=角色参数无效", status_code=303)
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(404)
    if user.username == request.session.get("admin_username", ""):
        return RedirectResponse("/admin/users?err=不能修改自己的角色", status_code=303)
    user.role = role
    _audit(db, request, "admin_user_set_role", application_id=None, detail={"username": user.username, "role": role})
    db.commit()
    role_zh = "超级管理员" if role == "superadmin" else "员工"
    return RedirectResponse(f"/admin/users?msg=已将「{user.username}」的角色改为{role_zh}", status_code=303)


# ── 员工档案 & 合同管理 ─────────────────────────────────────────────────

_STAFF_STATUS_ZH = {"probation": "试用中", "active": "在职", "resigned": "离职"}
_CONTRACT_TYPE_ZH = {"formal": "正式合同", "probation": "试用期合同", "parttime": "兼职合同", "labor": "劳务合同"}
_POSITION_OPTIONS = ["前台", "医生", "美容师", "助理", "收银", "其他"]
_STORE_OPTIONS = ["东环店", "横岗店"]


def _expiring_contracts(db: Session, days: int = 30) -> list:
    """返回 days 天内到期的合同（end_date 非空）。"""
    from datetime import date, timedelta
    today = date.today().isoformat()
    deadline = (date.today() + timedelta(days=days)).isoformat()
    rows = (
        db.query(Contract)
        .filter(Contract.end_date != "", Contract.end_date >= today, Contract.end_date <= deadline)
        .all()
    )
    return rows


@app.get("/admin/staff", response_class=HTMLResponse)
async def admin_staff_list(
    request: Request,
    db: Session = Depends(get_db),
    msg: str = Query(""),
    err: str = Query(""),
):
    if not _admin_ok(request):
        return templates.TemplateResponse("admin_login.html", {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)})
    active_staff = db.query(Staff).filter(Staff.status != StaffStatus.resigned.value).order_by(Staff.hire_date).all()
    resigned_staff = db.query(Staff).filter(Staff.status == StaffStatus.resigned.value).order_by(Staff.resign_date.desc()).all()
    expiring = _expiring_contracts(db)
    return templates.TemplateResponse("admin_staff_list.html", {
        "request": request, "title": "员工管理",
        "active_staff": active_staff, "resigned_staff": resigned_staff,
        "expiring": expiring, "status_zh": _STAFF_STATUS_ZH,
        "csrf_token": _get_csrf_token(request), "msg": msg, "err": err,
    })


@app.get("/admin/staff/create", response_class=HTMLResponse)
async def admin_staff_create_get(request: Request, db: Session = Depends(get_db)):
    if not _admin_ok(request):
        return templates.TemplateResponse("admin_login.html", {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)})
    require_superadmin(request)
    admin_users = db.query(AdminUser).filter(AdminUser.is_active == True).order_by(AdminUser.username).all()
    return templates.TemplateResponse("admin_staff_form.html", {
        "request": request, "title": "新增员工", "staff": None,
        "admin_users": admin_users, "position_options": _POSITION_OPTIONS,
        "store_options": _STORE_OPTIONS, "csrf_token": _get_csrf_token(request), "err": "",
    })


@app.post("/admin/staff/create", name="admin_staff_create")
async def admin_staff_create_post(
    request: Request, db: Session = Depends(get_db),
    name: str = Form(...), gender: str = Form(""), birthday: str = Form(""),
    phone: str = Form(""), id_number: str = Form(""), store: str = Form(""),
    position: str = Form(""), hire_date: str = Form(""), probation_end_date: str = Form(""),
    status: str = Form("active"), emergency_contact_name: str = Form(""),
    emergency_contact_phone: str = Form(""), emergency_contact_relation: str = Form(""),
    admin_user_id: str = Form(""), notes: str = Form(""), csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    name = name.strip()
    if not name:
        return RedirectResponse("/admin/staff/create?err=姓名不能为空", status_code=303)
    auid = int(admin_user_id) if admin_user_id.strip().isdigit() else None
    s = Staff(
        name=name, gender=gender, birthday=birthday, phone=phone.strip(),
        id_number=id_number.strip(), store=store, position=position,
        hire_date=hire_date, probation_end_date=probation_end_date, status=status,
        emergency_contact_name=emergency_contact_name, emergency_contact_phone=emergency_contact_phone,
        emergency_contact_relation=emergency_contact_relation, admin_user_id=auid, notes=notes,
    )
    db.add(s)
    _audit(db, request, "staff_create", application_id=None, detail={"name": name})
    db.commit()
    return RedirectResponse(f"/admin/staff/{s.id}?msg=员工档案已创建", status_code=303)


@app.get("/admin/staff/{staff_id}", response_class=HTMLResponse)
async def admin_staff_detail(
    staff_id: int, request: Request, db: Session = Depends(get_db),
    msg: str = Query(""), err: str = Query(""),
):
    if not _admin_ok(request):
        return templates.TemplateResponse("admin_login.html", {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)})
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404)
    contracts = db.query(Contract).filter(Contract.staff_id == staff_id).order_by(Contract.start_date.desc()).all()
    from datetime import date, timedelta
    today = date.today().isoformat()
    expiry_30 = (date.today() + timedelta(days=30)).isoformat()
    return templates.TemplateResponse("admin_staff_detail.html", {
        "request": request, "title": f"员工档案 · {staff.name}",
        "staff": staff, "contracts": contracts,
        "status_zh": _STAFF_STATUS_ZH, "contract_type_zh": _CONTRACT_TYPE_ZH,
        "csrf_token": _get_csrf_token(request), "msg": msg, "err": err,
        "is_superadmin": _is_superadmin(request),
        "now_date": today, "expiry_30": expiry_30,
    })


@app.get("/admin/staff/{staff_id}/edit", response_class=HTMLResponse)
async def admin_staff_edit_get(staff_id: int, request: Request, db: Session = Depends(get_db)):
    if not _admin_ok(request):
        return templates.TemplateResponse("admin_login.html", {"request": request, "title": "医院后台登录", "csrf_token": _get_csrf_token(request)})
    require_superadmin(request)
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404)
    admin_users = db.query(AdminUser).filter(AdminUser.is_active == True).order_by(AdminUser.username).all()
    return templates.TemplateResponse("admin_staff_form.html", {
        "request": request, "title": f"编辑员工 · {staff.name}", "staff": staff,
        "admin_users": admin_users, "position_options": _POSITION_OPTIONS,
        "store_options": _STORE_OPTIONS, "csrf_token": _get_csrf_token(request), "err": "",
    })


@app.post("/admin/staff/{staff_id}/edit", name="admin_staff_edit")
async def admin_staff_edit_post(
    staff_id: int, request: Request, db: Session = Depends(get_db),
    name: str = Form(...), gender: str = Form(""), birthday: str = Form(""),
    phone: str = Form(""), id_number: str = Form(""), store: str = Form(""),
    position: str = Form(""), hire_date: str = Form(""), probation_end_date: str = Form(""),
    status: str = Form("active"), resign_date: str = Form(""), resign_reason: str = Form(""),
    emergency_contact_name: str = Form(""), emergency_contact_phone: str = Form(""),
    emergency_contact_relation: str = Form(""), admin_user_id: str = Form(""),
    notes: str = Form(""), csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404)
    staff.name = name.strip()
    staff.gender = gender; staff.birthday = birthday; staff.phone = phone.strip()
    staff.id_number = id_number.strip(); staff.store = store; staff.position = position
    staff.hire_date = hire_date; staff.probation_end_date = probation_end_date
    staff.status = status; staff.resign_date = resign_date; staff.resign_reason = resign_reason
    staff.emergency_contact_name = emergency_contact_name
    staff.emergency_contact_phone = emergency_contact_phone
    staff.emergency_contact_relation = emergency_contact_relation
    staff.admin_user_id = int(admin_user_id) if admin_user_id.strip().isdigit() else None
    staff.notes = notes
    _audit(db, request, "staff_edit", application_id=None, detail={"staff_id": staff_id, "name": staff.name})
    db.commit()
    return RedirectResponse(f"/admin/staff/{staff_id}?msg=已保存", status_code=303)


@app.post("/admin/staff/{staff_id}/contracts/create", name="admin_contract_create")
async def admin_contract_create(
    staff_id: int, request: Request, db: Session = Depends(get_db),
    contract_type: str = Form("formal"), start_date: str = Form(""),
    end_date: str = Form(""), notes: str = Form(""),
    file: Optional[UploadFile] = File(None), csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404)
    file_path = ""
    original_filename = ""
    if file and file.filename:
        import aiofiles
        ext = Path(file.filename).suffix.lower()
        if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"):
            return RedirectResponse(f"/admin/staff/{staff_id}?err=合同文件仅支持 PDF/图片/Word", status_code=303)
        save_dir = Path(settings.upload_dir) / "contracts" / str(staff_id)
        save_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{secrets.token_hex(8)}{ext}"
        save_path = save_dir / fname
        async with aiofiles.open(save_path, "wb") as f:
            content = await file.read()
            await f.write(content)
        file_path = str(save_path)
        original_filename = file.filename
    c = Contract(
        staff_id=staff_id, contract_type=contract_type,
        start_date=start_date, end_date=end_date,
        file_path=file_path, original_filename=original_filename, notes=notes,
    )
    db.add(c)
    _audit(db, request, "contract_create", application_id=None, detail={"staff_id": staff_id, "type": contract_type})
    db.commit()
    return RedirectResponse(f"/admin/staff/{staff_id}?msg=合同已添加", status_code=303)


@app.post("/admin/staff/{staff_id}/contracts/{contract_id}/delete", name="admin_contract_delete")
async def admin_contract_delete(
    staff_id: int, contract_id: int, request: Request,
    db: Session = Depends(get_db), csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    c = db.query(Contract).filter(Contract.id == contract_id, Contract.staff_id == staff_id).first()
    if not c:
        raise HTTPException(404)
    if c.file_path and Path(c.file_path).exists():
        Path(c.file_path).unlink(missing_ok=True)
    db.delete(c)
    _audit(db, request, "contract_delete", application_id=None, detail={"staff_id": staff_id, "contract_id": contract_id})
    db.commit()
    return RedirectResponse(f"/admin/staff/{staff_id}?msg=合同已删除", status_code=303)


@app.get("/admin/staff/{staff_id}/contracts/{contract_id}/file")
async def admin_contract_file(
    staff_id: int, contract_id: int, request: Request, db: Session = Depends(get_db),
):
    require_admin(request)
    c = db.query(Contract).filter(Contract.id == contract_id, Contract.staff_id == staff_id).first()
    if not c or not c.file_path or not Path(c.file_path).exists():
        raise HTTPException(404)
    return FileResponse(c.file_path, filename=c.original_filename or Path(c.file_path).name)


@app.post("/admin/backup/create", name="admin_backup_create")
async def admin_backup_create(request: Request, db: Session = Depends(get_db), csrf_token: str = Form("")):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    try:
        out = create_backup_zip()
    except Exception as e:
        return RedirectResponse("/admin?backup_err=1&reason=" + quote(str(e)[:120], safe=""), status_code=303)
    _audit(db, request, "backup_create", application_id=None, detail={"file": out.name, "size": out.stat().st_size})
    db.commit()
    return RedirectResponse("/admin?backup_ok=1&file=" + quote(out.name, safe=""), status_code=303)


@app.get("/admin/backup/download/{filename}", name="admin_backup_download")
async def admin_backup_download(request: Request, filename: str, db: Session = Depends(get_db)):
    require_admin(request)
    require_superadmin(request)
    if not is_safe_backup_filename(filename):
        raise HTTPException(404)
    root = Path(settings.backup_dir).resolve()
    path = (root / filename).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(404)
    _audit(db, request, "backup_download", application_id=None, detail={"file": filename})
    db.commit()
    return FileResponse(path, filename=filename, media_type="application/zip")


def _rmtree_app_uploads(app_id: int) -> None:
    base = Path(settings.upload_dir) / str(app_id)
    if base.exists() and base.is_dir():
        shutil.rmtree(base, ignore_errors=True)


@app.get("/admin/purge", response_class=HTMLResponse)
async def admin_purge_get_hint():
    """避免误用 GET 打开 /admin/purge 时出现 JSON 404；清理必须用后台表单 POST。"""
    return HTMLResponse(
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/><title>数据清理</title></head>'
        '<body style="font-family:sans-serif;padding:1.5rem;">'
        "<p>数据清理仅接受 <strong>POST</strong>（请在医院后台页面内提交表单）。</p>"
        '<p><a href="/admin">返回医院后台</a></p></body></html>',
        status_code=200,
    )


async def _admin_purge_run(
    request: Request,
    db: Session,
    csrf_token: str,
    scope: str,
    confirm: str,
):
    """一键清理：scope=all/drafts/appointments/everything"""
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    scope = (scope or "all").strip().lower()
    if scope not in ("all", "drafts", "appointments", "everything"):
        return RedirectResponse("/admin?purge_err=1", status_code=303)
    confirm = (confirm or "").strip()

    CONFIRM_MAP = {
        "drafts":       "确认删除全部草稿",
        "all":          "确认删除全部申请数据",
        "appointments": "确认删除全部预约数据",
        "everything":   "确认删除全部数据",
    }
    if confirm != CONFIRM_MAP[scope]:
        return RedirectResponse("/admin?purge_err=1", status_code=303)

    n = 0
    if scope in ("drafts", "all", "everything"):
        if scope == "drafts":
            q = db.query(Application).filter(Application.status == ApplicationStatus.draft.value)
        else:
            q = db.query(Application)
        rows = q.all()
        n += len(rows)
        for row in rows:
            _rmtree_app_uploads(row.id)
            db.delete(row)
        if scope in ("all", "everything"):
            db.query(AuditLog).delete(synchronize_session=False)

    if scope in ("appointments", "everything"):
        appt_count = db.query(Appointment).delete(synchronize_session=False)
        n += appt_count

    db.commit()
    _audit(db, request, "purge_" + scope, application_id=None, detail={"deleted": n})
    db.commit()
    return RedirectResponse(f"/admin?purge_ok=1&deleted={n}&what={scope}", status_code=303)


@app.post("/admin/purge", name="admin_purge")
async def admin_purge(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    scope: str = Form("all"),
    confirm: str = Form(""),
):
    return await _admin_purge_run(request, db, csrf_token, scope, confirm)


@app.post("/admin/system/purge", name="admin_purge_system")
async def admin_purge_system(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    scope: str = Form("all"),
    confirm: str = Form(""),
):
    """备用地址：若 /admin/purge 被拦截，可改表单指向此路径。"""
    return await _admin_purge_run(request, db, csrf_token, scope, confirm)


@app.post("/admin/appointments/create", name="admin_appointment_create")
async def admin_appointment_create(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    category: str = Form(...),
    service_name: str = Form(...),
    customer_name: str = Form(...),
    phone: str = Form(...),
    pet_name: str = Form(...),
    pet_gender: str = Form(...),
    store: str = Form(...),
    appointment_date: str = Form(...),
    appointment_time: str = Form(...),
    duration_minutes: str = Form("30"),
    notes: str = Form(""),
    related_application_id: str = Form(""),
    redirect_after: str = Form("admin"),
    pet_size: str = Form(""),
    coat_length: str = Form(""),
    addon_services: list[str] = Form([]),
    is_proxy: str = Form(""),
    proxy_name: str = Form(""),
    proxy_phone: str = Form(""),
    proxy_relation: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    redirect_base = _admin_appointment_redirect_base(redirect_after)
    try:
        fields = _assert_appointment_fields(
            category=category,
            service_name=service_name,
            customer_name=customer_name,
            phone=phone,
            pet_name=pet_name,
            pet_gender=pet_gender,
            store=store,
            appointment_date=appointment_date,
            appointment_time=appointment_time,
            notes=notes,
            duration_minutes=duration_minutes,
        )
        related_id: int | None = None
        raw_related = (related_application_id or "").strip()
        if str(fields["category"]) == AppointmentCategory.tnr.value and raw_related:
            if not raw_related.isdigit():
                raise HTTPException(400, "关联 TNR 申请编号应为数字。")
            related_id = int(raw_related)
            exists = db.query(Application.id).filter(Application.id == related_id).scalar()
            if not exists:
                raise HTTPException(404, "关联的 TNR 申请不存在。")
        # TNR 规则校验
        tnr_err = _check_tnr_constraints(
            db,
            category=str(fields["category"]),
            store=str(fields["store"]),
            appointment_date=str(fields["appointment_date"]),
            appointment_time=str(fields["appointment_time"]),
        )
        if tnr_err:
            raise HTTPException(400, tnr_err)
        # 重复申请编号校验
        dup_err = _check_duplicate_application_appointment(db, related_id)
        if dup_err:
            raise HTTPException(400, dup_err)
        # 门诊时间限制校验
        time_err = _check_outpatient_time(str(fields["category"]), str(fields["appointment_time"]))
        if time_err:
            raise HTTPException(400, time_err)
        # 时段容量校验
        cap_err = _check_slot_capacity(
            db,
            store=str(fields["store"]),
            appointment_date=str(fields["appointment_date"]),
            appointment_time=str(fields["appointment_time"]),
            category=str(fields["category"]),
            service_name=str(fields["service_name"]),
        )
        if cap_err:
            raise HTTPException(400, cap_err)
        # 时间冲突检测
        conflict = _check_appointment_conflict(
            db,
            store=str(fields["store"]),
            appointment_date=str(fields["appointment_date"]),
            appointment_time=str(fields["appointment_time"]),
            duration_minutes=int(fields["duration_minutes"]),
        )
        if conflict:
            raise HTTPException(
                400,
                f"时间冲突：该门店 {conflict.appointment_date} {conflict.appointment_time} 已有预约"
                f"（#{conflict.id} {conflict.customer_name}），请换一个时间段。",
            )
        _is_beauty = str(fields["category"]) == AppointmentCategory.beauty.value
        _is_proxy_bool = bool(is_proxy and is_proxy.strip())
        row = Appointment(
            category=str(fields["category"]),
            status=AppointmentStatus.pending.value,
            service_name=str(fields["service_name"]),
            customer_name=str(fields["customer_name"]),
            phone=str(fields["phone"]),
            pet_name=str(fields["pet_name"]),
            pet_gender=str(fields["pet_gender"]),
            store=str(fields["store"]),
            appointment_date=str(fields["appointment_date"]),
            appointment_time=str(fields["appointment_time"]),
            duration_minutes=int(fields["duration_minutes"]),
            notes=str(fields["notes"]),
            source="admin",
            related_application_id=related_id,
            pet_size=(pet_size.strip() or None) if _is_beauty else None,
            coat_length=(coat_length.strip() or None) if _is_beauty else None,
            addon_services=(",".join(s.strip() for s in addon_services if s.strip()) or None) if _is_beauty else None,
            is_proxy=_is_proxy_bool,
            proxy_name=proxy_name.strip() if _is_proxy_bool else "",
            proxy_phone=proxy_phone.strip() if _is_proxy_bool else "",
            proxy_relation=proxy_relation.strip() if _is_proxy_bool else "",
        )
        db.add(row)
        db.flush()
        _audit(
            db,
            request,
            "appointment_create",
            application_id=related_id,
            detail={
                "appointment_id": row.id,
                "category": row.category,
                "service_name": row.service_name,
                "store": row.store,
                "appointment_date": row.appointment_date,
                "appointment_time": row.appointment_time,
            },
        )
        db.commit()
        return RedirectResponse(redirect_base + f"?appointment_ok=create#appt-{row.id}", status_code=303)
    except HTTPException as e:
        db.rollback()
        return RedirectResponse(
            redirect_base + "?appointment_err=" + quote(str(e.detail)[:160], safe=""),
            status_code=303,
        )


@app.post("/admin/appointments/{appointment_id}/status", name="admin_appointment_status")
async def admin_appointment_status(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    status: str = Form(...),
    redirect_after: str = Form("admin"),
    cancel_reason: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    redirect_base = _admin_appointment_redirect_base(redirect_after)
    status = (status or "").strip()
    anchor = f"appt-{appointment_id}"
    if status not in _ALLOWED_APPOINTMENT_STATUSES:
        return RedirectResponse(redirect_base + "?appointment_err=" + quote("无效的预约状态", safe="") + f"#{anchor}", status_code=303)
    row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not row:
        return RedirectResponse(redirect_base + "?appointment_err=" + quote("预约记录不存在", safe="") + f"#{anchor}", status_code=303)
    old_appt_status = row.status
    row.status = status
    row.updated_at = datetime.utcnow()
    reason_clean = (cancel_reason or "").strip()[:300]
    audit_detail: dict = {"appointment_id": row.id, "old_status": old_appt_status, "status": row.status}
    if reason_clean:
        audit_detail["cancel_reason"] = reason_clean
        # 将取消原因追加到备注，方便列表页直接查看
        existing_notes = (row.notes or "").strip()
        row.notes = (existing_notes + f"\n[取消原因] {reason_clean}").strip()
    _audit(
        db,
        request,
        "appointment_status_update",
        application_id=row.related_application_id,
        detail=audit_detail,
    )
    # 同步关联 TNR 申请状态
    if row.related_application_id:
        app_row = db.get(Application, row.related_application_id)
        if app_row:
            if status == AppointmentStatus.confirmed.value and app_row.status in (
                ApplicationStatus.approved.value,
                ApplicationStatus.pre_approved.value,
            ):
                app_row.status = ApplicationStatus.scheduled.value
                app_row.appointment_at = row.appointment_date
                app_row.updated_at = datetime.utcnow()
            elif status == AppointmentStatus.cancelled.value and app_row.status == ApplicationStatus.scheduled.value:
                app_row.status = ApplicationStatus.approved.value
                app_row.updated_at = datetime.utcnow()
            elif status == AppointmentStatus.no_show.value:
                app_row.status = ApplicationStatus.no_show.value
                app_row.updated_at = datetime.utcnow()
    db.commit()
    # 预约确认/取消后推送通知给用户（#5）
    openid_for_push = (row.wechat_openid or "").strip()
    if openid_for_push and status in (AppointmentStatus.confirmed.value, AppointmentStatus.cancelled.value):
        status_label = "已确认，请按约定时间到院" if status == AppointmentStatus.confirmed.value else "已取消"
        push_appointment_status(
            db,
            appointment_id=row.id,
            openid=openid_for_push,
            status_text=status_label,
            service_name=row.service_name or "",
            store=row.store or "",
            appointment_date=row.appointment_date or "",
            appointment_time=row.appointment_time or "",
            phone=row.phone or "",
            customer_name=row.customer_name or "",
            note=reason_clean or status_label,
        )
    return RedirectResponse(redirect_base + f"?appointment_ok=status#{anchor}", status_code=303)


@app.post("/admin/appointments/{appointment_id}/reschedule", name="admin_appointment_reschedule")
async def admin_appointment_reschedule(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    next: str = Form("/admin/appointments"),
    new_store: str = Form(""),
    new_date: str = Form(...),
    new_time: str = Form(...),
    new_duration: str = Form(""),
    reschedule_reason: str = Form(""),
):
    """后台改约：修改预约的门店、日期、时间，自动做冲突检测并记录通知日志。"""
    require_admin(request)
    _require_csrf(request, csrf_token)
    _anchor = f"appt-{appointment_id}"
    row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not row:
        return _admin_appointment_redirect(next, err="预约记录不存在", anchor=_anchor)
    if row.status in (AppointmentStatus.cancelled.value, AppointmentStatus.completed.value):
        return _admin_appointment_redirect(next, err="已完成或已取消的预约不能改约", anchor=_anchor)

    new_date = (new_date or "").strip()
    new_time = (new_time or "").strip()
    if not new_date or not new_time:
        return _admin_appointment_redirect(next, err="请填写新的预约日期和时间", anchor=_anchor)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", new_date):
        return _admin_appointment_redirect(next, err="日期格式应为 YYYY-MM-DD", anchor=_anchor)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", new_time):
        return _admin_appointment_redirect(next, err="时间格式应为 HH:MM", anchor=_anchor)
    try:
        date_obj = datetime.strptime(new_date, "%Y-%m-%d").date()
    except ValueError:
        return _admin_appointment_redirect(next, err="无效的日期", anchor=_anchor)
    if date_obj < datetime.now().date():
        return _admin_appointment_redirect(next, err="改约日期不能早于今天", anchor=_anchor)

    target_store = (new_store or "").strip() or row.store
    if target_store not in _ALLOWED_CLINIC_STORES:
        return _admin_appointment_redirect(next, err="无效的门店", anchor=_anchor)
    dur_raw = (new_duration or "").strip()
    target_duration = int(dur_raw) if dur_raw and dur_raw.isdigit() and 10 <= int(dur_raw) <= 480 else row.duration_minutes

    # TNR 规则校验
    tnr_err = _check_tnr_constraints(
        db,
        category=row.category,
        store=target_store,
        appointment_date=new_date,
        appointment_time=new_time,
        exclude_id=appointment_id,
    )
    if tnr_err:
        return _admin_appointment_redirect(next, err=tnr_err, anchor=_anchor)
    # 门诊时间限制校验（改约）
    time_err = _check_outpatient_time(row.category, new_time)
    if time_err:
        return _admin_appointment_redirect(next, err=time_err, anchor=_anchor)
    # 时段容量校验（改约）
    cap_err = _check_slot_capacity(
        db,
        store=target_store,
        appointment_date=new_date,
        appointment_time=new_time,
        category=row.category,
        service_name=row.service_name or "",
        exclude_id=appointment_id,
    )
    if cap_err:
        return _admin_appointment_redirect(next, err=cap_err, anchor=_anchor)
    # 冲突检测（排除自身）
    conflict = _check_appointment_conflict(
        db,
        store=target_store,
        appointment_date=new_date,
        appointment_time=new_time,
        duration_minutes=target_duration,
        exclude_id=appointment_id,
    )
    if conflict:
        return _admin_appointment_redirect(
            next,
            err=f"时间冲突：该门店 {conflict.appointment_date} {conflict.appointment_time} 已有预约"
            f"（#{conflict.id} {conflict.customer_name}），请换一个时间段。",
            anchor=_anchor,
        )

    old_store = row.store
    old_date = row.appointment_date
    old_time = row.appointment_time
    old_duration = row.duration_minutes

    row.store = target_store
    row.appointment_date = new_date
    row.appointment_time = new_time
    row.duration_minutes = target_duration
    row.updated_at = datetime.utcnow()

    reason_text = (reschedule_reason or "").strip()[:500]
    detail = {
        "appointment_id": row.id,
        "old": {"store": old_store, "date": old_date, "time": old_time, "duration": old_duration},
        "new": {"store": row.store, "date": row.appointment_date, "time": row.appointment_time, "duration": row.duration_minutes},
        "reason": reason_text,
    }
    _audit(db, request, "appointment_reschedule", application_id=row.related_application_id, detail=detail)

    # 同步关联申请的预约日期
    if row.related_application_id:
        app_row = db.get(Application, row.related_application_id)
        if app_row and app_row.status == ApplicationStatus.scheduled.value:
            app_row.appointment_at = new_date
            app_row.updated_at = datetime.utcnow()
        notify_payload = (
            f"预约改约通知：#{row.id} {row.customer_name} 由 {old_store} {old_date} {old_time} "
            f"改为 {row.store} {row.appointment_date} {row.appointment_time}"
        )
        if reason_text:
            notify_payload += f"（原因：{reason_text}）"
        from app.models import NotificationLog
        db.add(NotificationLog(
            application_id=row.related_application_id,
            channel="log",
            payload=notify_payload,
            success=True,
        ))

    db.commit()
    return _admin_appointment_redirect(next, ok="reschedule", anchor=_anchor)


@app.post("/admin/app/{app_id}/manual-approve")
async def manual_approve(app_id: int, request: Request, db: Session = Depends(get_db), csrf_token: str = Form("")):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.pending_ai.value,
            ApplicationStatus.pending_manual.value,
            ApplicationStatus.pre_approved.value,
        },
        "人工通过",
    )
    row.status = ApplicationStatus.approved.value
    _audit(db, request, "manual_approve", application_id=app_id)
    db.commit()
    notify_application_result(db, app_id, row.phone, row.applicant_name, approved=True)
    push_application_result(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        applicant_name=row.applicant_name,
        status_text="审核已通过",
        phone_masked=row.phone,
        note="请按约定时间携带猫咪到院",
        submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/app/{app_id}/reject")
async def manual_reject(
    app_id: int,
    request: Request,
    reason: str = Form(""),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.pending_ai.value,
            ApplicationStatus.pending_manual.value,
            ApplicationStatus.pre_approved.value,
            ApplicationStatus.approved.value,
            ApplicationStatus.scheduled.value,
        },
        "拒绝",
    )
    row.status = ApplicationStatus.rejected.value
    row.reject_reason = reason.strip()
    _audit(db, request, "manual_reject", application_id=app_id, detail={"reason": row.reject_reason})
    db.commit()
    notify_application_result(db, app_id, row.phone, row.applicant_name, approved=False, extra=reason)
    push_rejection_notice(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        cat_nickname=row.cat_nickname or "",
        reason=(reason or "不符合申请条件")[:20],
        action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/app/{app_id}/verify-cat")
async def verify_cat(app_id: int, request: Request, db: Session = Depends(get_db), csrf_token: str = Form("")):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.approved.value,
            ApplicationStatus.scheduled.value,
        },
        "到院核对",
    )
    row.staff_cat_verified = True
    row.status = ApplicationStatus.arrived_verified.value
    _audit(db, request, "verify_cat", application_id=app_id)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/app/{app_id}/surgery-done")
async def surgery_done(app_id: int, request: Request, db: Session = Depends(get_db), csrf_token: str = Form("")):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.approved.value,
            ApplicationStatus.arrived_verified.value,
            ApplicationStatus.scheduled.value,
        },
        "标记手术完成",
    )
    if not _application_has_surgery_before_and_after(db, app_id):
        return RedirectResponse(
            "/admin?surgery_media_err="
            + quote(
                "标记手术完成前，须在本申请下各上传至少 1 条术前资料与 1 条术后资料（照片或视频均可）。",
                safe="",
            ),
            status_code=303,
        )
    row.status = ApplicationStatus.surgery_completed.value
    _audit(db, request, "surgery_done", application_id=app_id)
    db.commit()
    notify_application_result(
        db,
        app_id,
        row.phone,
        row.applicant_name,
        approved=True,
        extra="手术已完成。请遵医嘱护理；公猫放归时间请听从医嘱。若同意公开展示，您可在本院 TNR 展示页查看术前术后资料（脱敏处理）。",
    )
    push_surgery_done(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        cat_name=row.cat_nickname or "猫咪",
        note="手术已完成，请按医嘱护理",
        action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/api/wechat/login")
async def api_wechat_login(payload: dict = Body(...)):
    """小程序端：传 {code: js_code}，换取 openid。"""
    code = (payload or {}).get("code", "")
    if not code:
        raise HTTPException(400, "missing code")
    try:
        data = wechat_code2session(code)
    except Exception as e:
        raise HTTPException(400, str(e))
    # 生产环境不建议把 session_key 返回给前端；这里只返回 openid 供演示
    return {"openid": data.get("openid", "")}


@app.get("/api/wechat/my-tnr-status")
async def api_my_tnr_status(openid: str = Query(""), db: Session = Depends(get_db)):
    """小程序端：传 openid，返回该用户是否有可预约的已通过 TNR 申请。"""
    if not openid.strip():
        return {"has_approved": False, "approved_apps": []}
    _APPROVED_STATUSES = {
        ApplicationStatus.approved.value,
        ApplicationStatus.pre_approved.value,
        ApplicationStatus.scheduled.value,
    }
    rows = (
        db.query(Application)
        .filter(
            Application.wechat_openid == openid.strip(),
            Application.status.in_(_APPROVED_STATUSES),
        )
        .order_by(Application.created_at.desc())
        .limit(10)
        .all()
    )
    apps = [{"id": r.id, "status": r.status, "cat_nickname": r.cat_nickname} for r in rows]
    return {"has_approved": len(apps) > 0, "approved_apps": apps}


@app.get("/api/appointments/config")
async def api_appointments_config():
    return _appointment_catalog()


@app.get("/api/appointments/beauty-slots")
async def api_beauty_slots(
    service:     str = Query(""),
    pet_size:    str = Query(""),
    coat_length: str = Query(""),
    addons:      str = Query(""),   # 逗号分隔的附加项目
    date:        str = Query(""),   # YYYY-MM-DD
    store:       str = Query(""),
    db: Session = Depends(get_db),
):
    """
    美容预约：计算估算时长 + 可预约时段。
    时段生成考虑猫进烘干机可并发小型犬洗护的规则。
    """
    duration = _calc_beauty_duration(service, pet_size, coat_length, addons)

    # 时长显示字符串
    h, m = divmod(duration, 60)
    if h == 0:
        dur_display = f"{m}分钟"
    elif m:
        dur_display = f"{h}小时{m}分钟"
    else:
        dur_display = f"{h}小时"

    slots: list[str] = []
    if date and store:
        try:
            datetime.strptime(date, "%Y-%m-%d")
            slots = _beauty_slots_for_date(db, store, date, service, duration)
        except Exception:
            slots = []

    return {
        "duration_minutes": duration,
        "duration_display": dur_display,
        "available_slots":  slots,
        "disclaimer": (
            "以上占用时间为估算时间。"
            "如动物不配合或毛量超出预估，实际服务时长可能有所浮动，"
            "具体以门店实际执行时间为准。"
        ),
    }


@app.post("/api/appointments/create")
async def api_appointments_create(payload: dict = Body(...), db: Session = Depends(get_db)):
    openid = await _resolve_wechat_openid(payload)
    fields = _assert_appointment_fields(
        category=(payload or {}).get("category", ""),
        service_name=(payload or {}).get("service_name", ""),
        customer_name=(payload or {}).get("customer_name", ""),
        phone=(payload or {}).get("phone", ""),
        pet_name=(payload or {}).get("pet_name", ""),
        pet_gender=(payload or {}).get("pet_gender", ""),
        store=(payload or {}).get("store", ""),
        appointment_date=(payload or {}).get("appointment_date", ""),
        appointment_time=(payload or {}).get("appointment_time", ""),
        notes=(payload or {}).get("notes", ""),
        duration_minutes=str((payload or {}).get("duration_minutes", "30")),
    )
    related_id: int | None = None
    raw_related = (payload or {}).get("related_application_id")
    if raw_related not in (None, ""):
        try:
            related_id = int(raw_related)
        except (TypeError, ValueError):
            raise HTTPException(400, "关联申请编号格式不正确。")
        exists = db.query(Application.id).filter(Application.id == related_id).scalar()
        if not exists:
            raise HTTPException(404, "关联的 TNR 申请不存在。")
    # TNR 规则校验
    tnr_err = _check_tnr_constraints(
        db,
        category=str(fields["category"]),
        store=str(fields["store"]),
        appointment_date=str(fields["appointment_date"]),
        appointment_time=str(fields["appointment_time"]),
    )
    if tnr_err:
        raise HTTPException(400, tnr_err)
    # 重复申请编号校验
    dup_err = _check_duplicate_application_appointment(db, related_id)
    if dup_err:
        raise HTTPException(400, dup_err)
    # 时段容量校验
    # 门诊时间限制校验
    time_err = _check_outpatient_time(str(fields["category"]), str(fields["appointment_time"]))
    if time_err:
        raise HTTPException(400, time_err)
    cap_err = _check_slot_capacity(
        db,
        store=str(fields["store"]),
        appointment_date=str(fields["appointment_date"]),
        appointment_time=str(fields["appointment_time"]),
        category=str(fields["category"]),
        service_name=str(fields["service_name"]),
    )
    if cap_err:
        raise HTTPException(400, cap_err)
    # 时间冲突检测
    conflict = _check_appointment_conflict(
        db,
        store=str(fields["store"]),
        appointment_date=str(fields["appointment_date"]),
        appointment_time=str(fields["appointment_time"]),
        duration_minutes=int(fields["duration_minutes"]),
    )
    if conflict:
        raise HTTPException(
            400,
            f"时间冲突：该门店 {conflict.appointment_date} {conflict.appointment_time} 已有预约"
            f"（#{conflict.id} {conflict.customer_name}），请换一个时间段。",
        )
    _is_beauty_api = str(fields["category"]) == AppointmentCategory.beauty.value
    _pet_size_raw    = ((payload or {}).get("pet_size", "") or "").strip()
    _coat_length_raw = ((payload or {}).get("coat_length", "") or "").strip()
    _addon_raw       = ((payload or {}).get("addon_services", "") or "").strip()
    _is_proxy_api    = bool((payload or {}).get("is_proxy", False))
    _proxy_name_raw  = ((payload or {}).get("proxy_name", "") or "").strip()
    _proxy_phone_raw = ((payload or {}).get("proxy_phone", "") or "").strip()
    _proxy_rel_raw   = ((payload or {}).get("proxy_relation", "") or "").strip()
    row = Appointment(
        wechat_openid=openid,
        category=str(fields["category"]),
        status=AppointmentStatus.pending.value,
        service_name=str(fields["service_name"]),
        customer_name=str(fields["customer_name"]),
        phone=str(fields["phone"]),
        pet_name=str(fields["pet_name"]),
        pet_gender=str(fields["pet_gender"]),
        store=str(fields["store"]),
        appointment_date=str(fields["appointment_date"]),
        appointment_time=str(fields["appointment_time"]),
        duration_minutes=int(fields["duration_minutes"]),
        source="miniapp",
        notes=str(fields["notes"]),
        related_application_id=related_id,
        pet_size    =(_pet_size_raw    or None) if _is_beauty_api else None,
        coat_length =(_coat_length_raw or None) if _is_beauty_api else None,
        addon_services=(_addon_raw     or None) if _is_beauty_api else None,
        is_proxy=_is_proxy_api,
        proxy_name=_proxy_name_raw if _is_proxy_api else "",
        proxy_phone=_proxy_phone_raw if _is_proxy_api else "",
        proxy_relation=_proxy_rel_raw if _is_proxy_api else "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "appointment": _serialize_appointment(row)}


@app.post("/api/wechat/my-apps")
async def api_wechat_my_apps(payload: dict = Body(...), db: Session = Depends(get_db)):
    """小程序端：传 {code} 或 {openid}，返回该 openid 的申请列表（用于“我的订单”）。"""
    code = (payload or {}).get("code", "") or ""
    openid = (payload or {}).get("openid", "") or ""
    if code:
        try:
            data = wechat_code2session(code)
        except Exception as e:
            raise HTTPException(400, str(e))
        openid = data.get("openid", "") or ""
    openid = (openid or "").strip()
    if not openid:
        raise HTTPException(400, "missing openid")

    def mask_phone(s: str) -> str:
        t = (s or "").strip()
        if len(t) < 7:
            return t
        return t[:3] + "****" + t[-4:]

    rows = (
        db.query(Application)
        .filter(Application.wechat_openid == openid)
        .order_by(Application.created_at.desc())
        .limit(50)
        .all()
    )
    items = []
    for row in rows:
        notes = (row.reject_reason or "").strip()
        if len(notes) > 80:
            notes = notes[:80] + "…"
        hn = (row.health_note or "").strip()
        if len(hn) > 60:
            hn = hn[:60] + "…"
        items.append(
            {
                "id": row.id,
                "status": row.status,
                "clinic_store": row.clinic_store,
                "appointment_at": row.appointment_at,
                "phone_masked": mask_phone(row.phone),
                "note": notes,
                "cat_nickname": row.cat_nickname or "",
                "cat_gender": row.cat_gender or "",
                "age_estimate": row.age_estimate or "",
                "health_note_brief": hn,
                "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
                "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "",
            }
        )
    return {"openid": openid, "items": items}


@app.post("/api/wechat/my-appointments")
async def api_wechat_my_appointments(payload: dict = Body(...), db: Session = Depends(get_db)):
    openid = await _resolve_wechat_openid(payload)
    rows = (
        db.query(Appointment)
        .filter(Appointment.wechat_openid == openid)
        .order_by(Appointment.created_at.desc())
        .limit(50)
        .all()
    )
    return {"openid": openid, "items": [_serialize_appointment(x) for x in rows]}


@app.post("/api/wechat/appointments/{appointment_id}/cancel")
async def api_wechat_appointment_cancel(
    appointment_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """小程序端用户取消自己的预约（仅限 pending/confirmed 状态，且 openid 匹配）。"""
    openid = await _resolve_wechat_openid(payload)
    row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not row:
        raise HTTPException(404, "预约记录不存在")
    if (row.wechat_openid or "") != openid:
        raise HTTPException(403, "无权操作此预约")
    if row.status not in (AppointmentStatus.pending.value, AppointmentStatus.confirmed.value):
        raise HTTPException(400, f"当前状态（{row.status}）不可取消，请联系医院处理")
    row.status = AppointmentStatus.cancelled.value
    row.updated_at = datetime.utcnow()
    # 若关联 TNR 申请且已处于 scheduled 状态，回退为 approved
    if row.related_application_id:
        app_row = db.get(Application, row.related_application_id)
        if app_row and app_row.status == ApplicationStatus.scheduled.value:
            app_row.status = ApplicationStatus.approved.value
            app_row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "id": appointment_id}


@app.post("/api/wechat/claim-apps")
async def api_wechat_claim_apps(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    小程序端：用 {openid}+{phone}+{id_number} 找回历史订单。
    仅对 phone+id_number 匹配且 wechat_openid 为空的记录进行补写，不覆盖已有 openid 的记录。
    """
    code = (payload or {}).get("code", "") or ""
    openid = (payload or {}).get("openid", "") or ""
    phone = (payload or {}).get("phone", "") or ""
    id_number = (payload or {}).get("id_number", "") or ""

    if code and not openid:
        try:
            data = wechat_code2session(code)
        except Exception as e:
            raise HTTPException(400, str(e))
        openid = data.get("openid", "") or ""

    openid = (openid or "").strip()
    phone = (phone or "").strip()
    idn = (id_number or "").strip().upper()

    if not openid:
        raise HTTPException(400, "missing openid")
    if not re.fullmatch(r"1\d{10}", phone):
        raise HTTPException(400, "手机号格式不正确")
    if len(idn) == 18:
        if not re.fullmatch(r"\d{17}[\dX]", idn):
            raise HTTPException(400, "身份证号格式不正确")
    elif len(idn) == 15:
        if not re.fullmatch(r"\d{15}", idn):
            raise HTTPException(400, "身份证号格式不正确")
    else:
        raise HTTPException(400, "身份证号格式不正确")

    rows = (
        db.query(Application)
        .filter(Application.phone == phone)
        .filter(Application.id_number == idn)
        .filter(or_(Application.wechat_openid == None, Application.wechat_openid == ""))
        .order_by(Application.created_at.desc())
        .limit(200)
        .all()
    )
    n = 0
    for row in rows:
        row.wechat_openid = openid
        n += 1
    if n:
        db.commit()
    return {"ok": True, "updated": n}


def _video_ext(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if ext in (".mp4", ".webm", ".mov", ".mkv") else ".mp4"


def _image_ext(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"


@app.post("/admin/app/{app_id}/upload-surgery")
async def upload_surgery(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    before_images: list[UploadFile] | None = File(None),
    after_images: list[UploadFile] | None = File(None),
    before_videos: list[UploadFile] | None = File(None),
    after_videos: list[UploadFile] | None = File(None),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    base = Path(settings.upload_dir) / str(app_id)
    base.mkdir(parents=True, exist_ok=True)

    async def save_batch(files: list[UploadFile] | None, file_prefix: str, kind: str, *, is_video: bool = False):
        if not files:
            return
        for uf in files:
            if not uf.filename:
                continue
            ext = _video_ext(uf.filename) if is_video else _image_ext(uf.filename)
            dest = base / f"{file_prefix}_{secrets.token_hex(6)}{ext}"
            dest.write_bytes(await uf.read())
            db.add(
                MediaFile(
                    application_id=app_id,
                    kind=kind,
                    stored_path=str(dest),
                    original_name=uf.filename,
                )
            )

    before_images_n = len(before_images or [])
    after_images_n = len(after_images or [])
    before_videos_n = len(before_videos or [])
    after_videos_n = len(after_videos or [])

    await save_batch(before_images, "surg_bi", MediaKind.surgery_before.value)
    await save_batch(after_images, "surg_ai", MediaKind.surgery_after.value)
    await save_batch(before_videos, "surg_bv", MediaKind.surgery_before.value, is_video=True)
    await save_batch(after_videos, "surg_av", MediaKind.surgery_after.value, is_video=True)
    _audit(
        db,
        request,
        "upload_surgery",
        application_id=app_id,
        detail={
            "before_images": before_images_n,
            "after_images": after_images_n,
            "before_videos": before_videos_n,
            "after_videos": after_videos_n,
        },
    )
    db.commit()
    return RedirectResponse("/admin", status_code=303)


def _media_public_ok(m: MediaFile, app_row: Application) -> bool:
    if app_row.status != ApplicationStatus.surgery_completed.value:
        return False
    if not app_row.showcase_consent:
        return False
    return m.kind in (MediaKind.surgery_before.value, MediaKind.surgery_after.value)


@app.get("/file/{media_id}")
async def serve_file(media_id: int, request: Request, db: Session = Depends(get_db)):
    m = db.get(MediaFile, media_id)
    if not m:
        raise HTTPException(404)
    app_row = db.get(Application, m.application_id)
    if not app_row:
        raise HTTPException(404)
    path = Path(m.stored_path).resolve()
    root = Path(settings.upload_dir).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(404)

    admin = _admin_ok(request)
    if m.kind in (MediaKind.application_image.value, MediaKind.application_video.value):
        if not admin:
            raise HTTPException(403)
    elif not _media_public_ok(m, app_row) and not admin:
        raise HTTPException(403)

    ctype, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=ctype or "application/octet-stream")


@app.get("/api/showcase")
async def api_showcase(request: Request, db: Session = Depends(get_db)):
    """小程序爱心展示 JSON 接口：返回已完成手术且同意公开展示的条目。"""
    base = str(request.base_url).rstrip("/")
    q = (
        db.query(Application)
        .options(selectinload(Application.media))
        .filter(Application.status == ApplicationStatus.surgery_completed.value)
        .filter(Application.showcase_consent.is_(True))
        .order_by(Application.updated_at.desc())
    )
    items = []
    for a in q.all():
        before_imgs = [f"{base}/file/{m.id}" for m in a.media
                       if m.kind == MediaKind.surgery_before.value and not m.stored_path.lower().endswith(('.mp4', '.mov', '.avi'))]
        after_imgs  = [f"{base}/file/{m.id}" for m in a.media
                       if m.kind == MediaKind.surgery_after.value and not m.stored_path.lower().endswith(('.mp4', '.mov', '.avi'))]
        before_vids = [f"{base}/file/{m.id}" for m in a.media
                       if m.kind == MediaKind.surgery_before.value and m.stored_path.lower().endswith(('.mp4', '.mov', '.avi'))]
        after_vids  = [f"{base}/file/{m.id}" for m in a.media
                       if m.kind == MediaKind.surgery_after.value and m.stored_path.lower().endswith(('.mp4', '.mov', '.avi'))]
        if not (before_imgs or after_imgs or before_vids or after_vids):
            continue
        # 姓氏保留，名字用 * 替代
        name = a.applicant_name.strip() if a.applicant_name else ""
        if len(name) >= 2:
            masked_name = name[0] + "*" * (len(name) - 1)
        elif len(name) == 1:
            masked_name = name
        else:
            masked_name = "—"
        items.append({
            "id": a.id,
            "cat_nickname": a.cat_nickname or "无名猫咪",
            "cat_gender": a.cat_gender,
            "address": a.address or "",
            "store": a.clinic_store or "",
            "surgery_date": a.updated_at.strftime("%Y-%m-%d") if a.updated_at else "",
            "applicant_masked": masked_name,
            "before_images": before_imgs,
            "after_images": after_imgs,
            "before_videos": before_vids,
            "after_videos": after_vids,
        })
    return {"items": items, "total": len(items)}


@app.get("/showcase", response_class=HTMLResponse)
async def page_showcase(request: Request, db: Session = Depends(get_db)):
    q = (
        db.query(Application)
        .options(selectinload(Application.media))
        .filter(Application.status == ApplicationStatus.surgery_completed.value)
        .filter(Application.showcase_consent.is_(True))
        .order_by(Application.updated_at.desc())
    )
    items = []
    for a in q.all():
        before = [x for x in a.media if x.kind == MediaKind.surgery_before.value]
        after = [x for x in a.media if x.kind == MediaKind.surgery_after.value]
        if before or after:
            items.append({"app": a, "before": before, "after": after})
    return templates.TemplateResponse(
        "showcase.html",
        {"request": request, "title": "公布展示 · TNR 术前术后", "items": items},
    )


@app.post("/admin/app/{app_id}/toggle-showcase")
async def toggle_showcase(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    consent: str = Form("false"),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    row.showcase_consent = consent.lower() in ("true", "1", "on", "yes")
    _audit(db, request, "toggle_showcase", application_id=app_id, detail={"consent": row.showcase_consent})
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/app/{app_id}/mark-scheduled")
async def mark_scheduled(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    appointment_at: str = Form(""),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.approved.value,
            ApplicationStatus.pre_approved.value,
        },
        "标记已预约",
    )
    row.status = ApplicationStatus.scheduled.value
    if appointment_at.strip():
        row.appointment_at = appointment_at.strip()
    _audit(db, request, "mark_scheduled", application_id=app_id, detail={"appointment_at": row.appointment_at})
    db.commit()
    push_application_result(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        applicant_name=row.applicant_name,
        status_text="已预约",
        phone_masked=row.phone,
        note="请按约定时间携带猫咪到院",
        submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/app/{app_id}/mark-cancelled")
async def mark_cancelled(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    reason: str = Form(""),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.approved.value,
            ApplicationStatus.pre_approved.value,
            ApplicationStatus.scheduled.value,
        },
        "取消",
    )
    row.status = ApplicationStatus.cancelled.value
    if reason.strip():
        row.reject_reason = reason.strip()
    _audit(db, request, "mark_cancelled", application_id=app_id, detail={"reason": row.reject_reason})
    db.commit()
    push_application_result(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        applicant_name=row.applicant_name,
        status_text="已取消",
        phone_masked=row.phone,
        note=(row.reject_reason or "如需帮助请联系医院")[:20],
        submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/app/{app_id}/mark-no-show")
async def mark_no_show(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    row = db.get(Application, app_id)
    if not row:
        raise HTTPException(404)
    _require_status_in(
        row,
        {
            ApplicationStatus.scheduled.value,
        },
        "标记爽约",
    )
    row.status = ApplicationStatus.no_show.value
    _audit(db, request, "mark_no_show", application_id=app_id)
    db.commit()
    push_application_result(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        applicant_name=row.applicant_name,
        status_text="爽约",
        phone_masked=row.phone,
        note="如需改期请联系医院",
        submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        action_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/wechat/test-send")
async def admin_wechat_test_send(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    application_id: int = Form(...),
    template: str = Form("application_result"),  # application_result / surgery_done
    openid: str = Form(""),
):
    raise HTTPException(status_code=410, detail="该功能已移除")
