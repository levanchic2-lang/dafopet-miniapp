from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
import shutil
import subprocess
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

# 启动时若缺 Pillow 则自动安装（openpyxl 图片嵌入依赖）
try:
    import PIL  # noqa: F401
except ImportError:
    try:
        subprocess.run(
            [__import__("sys").executable, "-m", "pip", "install", "Pillow", "-q"],
            capture_output=True, timeout=120,
        )
    except Exception:
        pass

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
    Customer,
    MediaFile,
    MediaKind,
    Pet,
    Staff,
    StaffStatus,
    Prescription,
    PrescriptionItem,
    SalesOrder,
    SalesOrderItem,
    Visit,
    InventoryItem,
    InventoryTransaction,
    InventoryBatch,
    StocktakeSession,
    StocktakeItem,
    RabiesVaccineRecord,
    AdoptionPet,
    Invoice,
    InvoiceItem,
    Vaccination,
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
    """每天 08:00 检查手术预约并推送前一天+当天提醒。
    先等到下一个 08:00 再执行，避免服务重启时立即触发误推。
    """
    while True:
        now = datetime.now()
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            _run_surgery_reminders()
        except Exception:
            pass


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


def _upsert_customer(db: Session, name: str, phone: str, openid: str = "", id_number: str = "", address: str = "", source: str = "") -> "Customer":
    """查找或创建客户档案，始终合并最新信息。"""
    phone = (phone or "").strip()
    cust = db.query(Customer).filter(Customer.phone == phone).first() if phone else None
    if not cust:
        # 尝试通过 openid 查找（openid 非空时）
        if openid and openid.strip():
            cust = db.query(Customer).filter(Customer.wechat_openid == openid.strip()).first()
    if cust:
        # 合并更新
        if name and not cust.name:
            cust.name = name[:120]
        if openid and not cust.wechat_openid:
            cust.wechat_openid = openid.strip()
        if id_number and not cust.id_number:
            cust.id_number = id_number[:40]
        if address and not cust.address:
            cust.address = address[:500]
    else:
        cust = Customer(
            name=(name or "")[:120],
            phone=phone[:40],
            wechat_openid=(openid or "").strip()[:64],
            id_number=(id_number or "")[:40],
            address=(address or "")[:500],
            source=(source or "")[:40],
        )
        db.add(cust)
        db.flush()  # get id without commit
    return cust


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


def _admin_back(request: Request, app_id: int, msg: str = "") -> RedirectResponse:
    """操作完成后跳回后台，保留当前搜索/翻页参数，并定位到对应申请卡片。"""
    from urllib.parse import urlparse, urlencode, parse_qs
    referer = request.headers.get("referer", "")
    qs_keep: dict[str, str] = {}
    if referer:
        try:
            parsed = urlparse(referer)
            params = parse_qs(parsed.query, keep_blank_values=False)
            qs_keep = {k: v[0] for k, v in params.items()
                       if k in ("q", "page", "store", "status", "page_size")}
        except Exception:
            pass
    if msg:
        qs_keep["msg"] = msg
    qs = ("?" + urlencode(qs_keep)) if qs_keep else ""
    return RedirectResponse(f"/admin{qs}#app-{app_id}", status_code=303)


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
        "wechat_tmpl_surgery_reminder": settings.wechat_tmpl_surgery_reminder,
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
                "description": "适合绝育、骨科、软组织、眼科、神经外科等各类手术预约。",
                "booking_tip": "请使用下方「预约日期 / 预约时间」自主选择到院时段；后续可按手术台与麻醉安排细化。",
                "supports_related_application": False,
                "time_slots": [],
                "services": [
                    {"name": "绝育手术",    "duration_minutes": 60,  "description": "用于常规绝育手术预约。"},
                    {"name": "骨科手术",    "duration_minutes": 120, "description": "用于骨折、关节、脊椎等骨科手术预约。"},
                    {"name": "软组织手术",  "duration_minutes": 90,  "description": "用于皮肤、肿瘤切除、消化道等软组织手术预约。"},
                    {"name": "眼科手术",    "duration_minutes": 60,  "description": "用于眼睑、角膜、晶体等眼科手术预约。"},
                    {"name": "神经外科手术","duration_minutes": 120, "description": "用于脑部、脊髓等神经外科手术预约。"},
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
    is_proxy: str = Form(""),
    proxy_name: str = Form(""),
    proxy_phone: str = Form(""),
    proxy_relation: str = Form(""),
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

    # ── 重复提交检测 ──
    _DUP_STATUS_ZH = {
        "draft": "草稿", "pending_ai": "审核中", "pending_manual": "待人工审核",
        "pre_approved": "预通过", "approved": "已通过", "scheduled": "已预约",
        "no_show": "爽约", "arrived_verified": "到院核对中",
    }

    # 1. 审核未完成时（未到「已通过」之前）不允许再提交任何申请
    _PENDING_STATUSES = [
        ApplicationStatus.draft.value,
        ApplicationStatus.pending_ai.value,
        ApplicationStatus.pending_manual.value,
        ApplicationStatus.pre_approved.value,
    ]
    pending_dup = (
        db.query(Application)
        .filter(Application.phone == f["phone"])
        .filter(Application.status.in_(_PENDING_STATUSES))
        .order_by(Application.id.desc())
        .first()
    )
    if pending_dup:
        status_label = _DUP_STATUS_ZH.get(pending_dup.status, pending_dup.status)
        raise HTTPException(
            409,
            f"您已有一份申请正在审核中（编号 #{pending_dup.id}，当前状态：{status_label}），"
            f"请等待审核通过后再提交新的申请。如需取消，请联系医院前台。",
        )

    # 2. 同一手机号 + 同一猫咪名称，不能重复提交（终结状态除外）
    _TERMINAL_STATUSES = [
        ApplicationStatus.rejected.value,
        ApplicationStatus.cancelled.value,
        ApplicationStatus.surgery_completed.value,
    ]
    same_cat_dup = (
        db.query(Application)
        .filter(Application.phone == f["phone"])
        .filter(Application.cat_nickname == f["cat_nickname"])
        .filter(Application.status.notin_(_TERMINAL_STATUSES))
        .order_by(Application.id.desc())
        .first()
    )
    if same_cat_dup:
        status_label = _DUP_STATUS_ZH.get(same_cat_dup.status, same_cat_dup.status)
        raise HTTPException(
            409,
            f"「{f['cat_nickname']}」已有进行中的申请（编号 #{same_cat_dup.id}，当前状态：{status_label}），"
            f"请勿为同一只猫重复提交申请。",
        )

    # ── 自动创建/合并客户档案 ──
    try:
        _cust = _upsert_customer(
            db,
            name=f["applicant_name"],
            phone=f["phone"],
            openid=wechat_openid.strip(),
            id_number=f["id_number"],
            address=f["address"],
            source="tnr",
        )
        _cust_id = _cust.id
    except Exception:
        _cust_id = None

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
        is_proxy=is_proxy.lower() in ("true", "1", "on", "yes"),
        proxy_name=proxy_name.strip()[:120],
        proxy_phone=proxy_phone.strip()[:40],
        proxy_relation=proxy_relation.strip()[:40],
        status=ApplicationStatus.draft.value,
        customer_id=_cust_id,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)

    # ── 自动创建宠物档案 ──
    if _cust_id and f.get("cat_nickname"):
        try:
            _pet = Pet(
                customer_id=_cust_id,
                name=f["cat_nickname"][:120],
                species="cat",
                gender=f.get("cat_gender", "unknown"),
                birthday_estimate=f.get("age_estimate", "")[:40],
                is_stray=True,
                notes=f.get("health_note", "")[:500],
            )
            db.add(_pet)
            db.flush()
            app_row.pet_id = _pet.id
            db.commit()
        except Exception:
            pass

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
            Appointment.appointment_date >= today_str,
        )
        .scalar()
    ) or 0
    return {"count": count}


@app.get("/api/admin/feedback-count")
async def api_admin_feedback_count(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return {"count": 0}
    from app.models import Feedback
    count = db.query(func.count(Feedback.id)).filter(Feedback.status == "pending").scalar() or 0
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


_DEPLOY_TOKEN_FILE = Path("/srv/tnr-app/deploy_token.txt")

@app.post("/api/webhook/deploy")
async def webhook_deploy(request: Request):
    token = request.headers.get("X-Deploy-Token", "")
    try:
        expected = _DEPLOY_TOKEN_FILE.read_text().strip()
    except Exception:
        raise HTTPException(status_code=503, detail="deploy token not configured")
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    subprocess.Popen(
        "sleep 3 && git -C /srv/tnr-app/releases/current pull origin main && systemctl restart tnr-app",
        shell=True, start_new_session=True,
    )
    return {"status": "deploying"}


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin", status_code=303)


# ── 账号管理（仅 superadmin）────────────────────────────────────────────

@app.get("/admin/changelog", response_class=HTMLResponse)
async def admin_changelog_page(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login", status_code=303)
    import subprocess, re

    # ── 提交中文描述映射表（短哈希 → 中文说明）──────────────────
    _COMMIT_ZH: dict[str, str] = {
        "853e7be": "问题反馈页新增两家门店联系电话、微信及地址",
        "5447ee7": "修复：预约看板突破页面宽度限制，真正撑满全屏",
        "5468e59": "预约列表改版为三栏看板（上午 / 下午 / 晚上）",
        "cb8460c": "补充美容预约规则和各体型时长对照表至预约规则说明",
        "25c5731": "新增重复提交检测；TNR 提交成功后自动订阅手术完成通知",
        "1e8b16e": "修复：代申请人信息输入框高度过小、文字不显示",
        "0b25cf5": "修复：提交 TNR/手术预约时同步订阅手术完成通知模板",
        "4216c8c": "新增客户问题反馈功能（文字 + 截图上传，后台管理与处理）",
        "15a3f57": "修复：Application 模型缺失代预约字段导致提交失败",
        "0e4c84c": "修复：TNR 提交失败时前端显示详细错误信息",
        "e936282": "后台新增开发日志页面（自动从 Git 记录生成）",
        "67f6784": "新增专属手术提醒订阅消息模板（预约提醒）",
        "db64cc6": "修复：服务重启后不再误发手术提醒推送",
        "8433391": "新增客户自助改约功能（仅限待确认状态）",
        "338c13f": "TNR 申请页新增代预约功能",
        "b52b2d0": "修复：时段容量池说明在后台页面不显示的问题",
        "4b0d67a": "时段容量池按时长等比调整（上午9 / 下午18 / 晚上12 单位）",
        "fc28caa": "TNR/手术容量单位调整为4，预约管理页新增容量池规则说明",
        "caa7a7e": "修复：TNR/手术不再占用时段容量池，避免连续手术被误拒",
        "5315231": "新增代预约功能，记录代预约人姓名、电话及与实际申请人关系",
        "cdebebc": "后台新预约待确认通知：导航橙色角标 + 页面提示条",
        "d60f47d": "修复：爱心展示入口移至「我的预约」按钮下方",
        "7b3af92": "小程序新增 TNR 爱心展示页面",
        "439c2b0": "新增员工档案与合同管理模块，优化账号管理",
        "bf68614": "修复：固定 bcrypt 版本以兼容 passlib 1.7.4",
        "28a860e": "新增多账号权限管理系统",
        "94e3369": "补全预约规则两条缺失说明",
        "0f6a818": "更新预约规则说明，同步当前通知开放状态",
        "b548fe8": "预约页体验优化四项改进",
        "025cb96": "安全加固后台登录页，修复 textarea 跳顶问题",
        "1ed1c53": "优化订阅通知错误处理，添加诊断日志并修复编码问题",
        "5ecc4cf": "修复：遵守微信每次最多3个模板限制，拆分订阅时机",
        "357caa2": "新增「待人工审核」小程序推送通知",
        "7708658": "新增「审核不通过」小程序推送通知",
        "59f0201": "修复：爱心展示默认关闭，手术完成后需管理员手动授权才公开",
        "3023e71": "预约页 TNR 板块仅对申请已通过的用户开放",
        "7b2f570": "保存并展示定位文字地址，删除调试提示文字",
        "fb578bd": "修复：模板订阅改为 API + Storage 合并取值，互补不互斥",
        "027dacf": "修复：订阅授权每次先从 API 拉取全部模板，Storage 仅降级备用",
        "f982c6b": "修复：订阅授权始终从 API 拉取模板 ID，避免 Storage 缓存导致漏订阅",
        "24bf5bf": "新增预约通知模板订阅；/api/wechat/config 返回预约模板字段",
        "34c91aa": "预约通知改用独立模板，正确映射各字段",
        "a1b84a9": "修复预约通知日志：加 rollback 防止 NOT NULL 报错崩溃",
        "3256ed1": "数据清理功能完善：支持删除预约、一键清空全部数据",
        "5626011": "修复提交成功页：状态显示中文，AI 结论为空时不显示",
        "dbc0739": "期望手术日期改为可选字段",
        "b943bb1": "网页端降级为备用入口，申请页顶部加小程序引导",
        "3381914": "功能完善七项改进 + HTTPS 上线配置",
        "bbb7f1d": "项目初始化导入",
    }

    commits = []
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H|%h|%s|%an|%ad", "--date=format:%Y-%m-%d %H:%M", "-500"],
            capture_output=True, text=True, timeout=8,
            cwd=Path(__file__).parent.parent,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 4)
            if len(parts) == 5:
                full_hash, short_hash, subject, author, date = parts
                # 提取类型前缀（feat/fix/chore/…）
                m = re.match(r"^(feat|fix|chore|refactor|docs|style|test|perf|build|ci|revert)(\(.+?\))?:\s*(.+)$", subject)
                if m:
                    kind = m.group(1)
                    scope = (m.group(2) or "").strip("()")
                    msg_raw = m.group(3)
                else:
                    kind = "update"
                    scope = ""
                    msg_raw = subject
                # 优先使用中文说明，无则保留原文
                msg = _COMMIT_ZH.get(short_hash, msg_raw)
                commits.append({
                    "full_hash": full_hash,
                    "short_hash": short_hash,
                    "subject": subject,
                    "msg": msg,
                    "kind": kind,
                    "scope": scope,
                    "author": author,
                    "date": date,
                })
    except Exception as e:
        commits = [{"short_hash": "—", "msg": f"无法读取 git log：{e}", "kind": "error", "scope": "", "author": "", "date": "", "subject": "", "full_hash": ""}]
    return templates.TemplateResponse("admin_changelog.html", {
        "request": request,
        "title": "开发日志",
        "commits": commits,
    })


@app.get("/admin/hr", response_class=HTMLResponse)
async def admin_hr_page(
    request: Request,
    db: Session = Depends(get_db),
    msg: str = Query(""),
    err: str = Query(""),
):
    if not _admin_ok(request):
        return RedirectResponse("/admin/login")
    active_staff = db.query(Staff).filter(Staff.status != StaffStatus.resigned.value).order_by(Staff.hire_date).all()
    resigned_staff = db.query(Staff).filter(Staff.status == StaffStatus.resigned.value).order_by(Staff.resign_date.desc()).all()
    expiring = _expiring_contracts(db)
    all_users = db.query(AdminUser).order_by(AdminUser.created_at).all()
    # 尚未绑定账号的在职员工（可供新建账号时选择）
    linked_staff_ids = {s.admin_user_id for s in active_staff if s.admin_user_id}
    unlinked_staff = [s for s in active_staff if s.id not in linked_staff_ids]
    # 账号 → 员工姓名映射（用于列表显示）
    staff_by_user_id = {s.admin_user_id: s for s in active_staff if s.admin_user_id}
    return templates.TemplateResponse("admin_hr.html", {
        "request": request, "title": "人事管理",
        "active_staff": active_staff, "resigned_staff": resigned_staff,
        "expiring": expiring,
        "active_users": [u for u in all_users if u.is_active],
        "inactive_users": [u for u in all_users if not u.is_active],
        "unlinked_staff": unlinked_staff,
        "staff_by_user_id": staff_by_user_id,
        "current_username": request.session.get("admin_username", ""),
        "csrf_token": _get_csrf_token(request),
        "msg": msg, "err": err,
    })


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
    staff_id: str = Form(""),
    csrf_token: str = Form(""),
):
    require_admin(request)
    require_superadmin(request)
    _require_csrf(request, csrf_token)
    username = username.strip()
    if not username or not password:
        return RedirectResponse("/admin/hr?err=用户名和密码不能为空", status_code=303)
    if len(password) < 6:
        return RedirectResponse("/admin/hr?err=密码不能少于6位", status_code=303)
    if role not in ("superadmin", "staff"):
        role = "staff"
    existing = db.query(AdminUser).filter(AdminUser.username == username).first()
    if existing:
        return RedirectResponse(f"/admin/hr?err=用户名已存在：{username}", status_code=303)
    new_user = AdminUser(username=username, password_hash=_pwd_ctx.hash(password), role=role, is_active=True)
    db.add(new_user)
    db.flush()  # 获取 new_user.id
    if staff_id:
        try:
            sid = int(staff_id)
            staff_row = db.get(Staff, sid)
            if staff_row:
                staff_row.admin_user_id = new_user.id
        except (ValueError, TypeError):
            pass
    _audit(db, request, "admin_user_create", application_id=None, detail={"username": username, "role": role})
    db.commit()
    return RedirectResponse(f"/admin/hr?msg=已创建账号：{username}", status_code=303)


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
        return RedirectResponse("/admin/hr?err=不能停用当前登录账号", status_code=303)
    user.is_active = not user.is_active
    _audit(db, request, "admin_user_toggle", application_id=None, detail={"username": user.username, "active": user.is_active})
    db.commit()
    status_zh = "启用" if user.is_active else "停用"
    return RedirectResponse(f"/admin/hr?msg=已{status_zh}账号：{user.username}", status_code=303)


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
        return RedirectResponse("/admin/hr?err=新密码不能少于6位", status_code=303)
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(404)
    user.password_hash = _pwd_ctx.hash(new_password)
    _audit(db, request, "admin_user_reset_password", application_id=None, detail={"username": user.username})
    db.commit()
    return RedirectResponse(f"/admin/hr?msg=已重置密码：{user.username}", status_code=303)


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
        return RedirectResponse("/admin/hr?err=角色参数无效", status_code=303)
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(404)
    if user.username == request.session.get("admin_username", ""):
        return RedirectResponse("/admin/hr?err=不能修改自己的角色", status_code=303)
    user.role = role
    _audit(db, request, "admin_user_set_role", application_id=None, detail={"username": user.username, "role": role})
    db.commit()
    role_zh = "超级管理员" if role == "superadmin" else "员工"
    return RedirectResponse(f"/admin/hr?msg=已将「{user.username}」的角色改为{role_zh}", status_code=303)


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


@app.post("/admin/media/{media_id}/delete", name="admin_media_delete")
async def admin_media_delete(
    media_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    if form.get("csrf_token") != request.session.get("csrf_token"):
        return RedirectResponse("/admin?err=csrf", status_code=303)
    m = db.get(MediaFile, media_id)
    if not m:
        return RedirectResponse("/admin?err=文件不存在", status_code=303)
    app_id = m.application_id
    try:
        p = Path(m.stored_path)
        if p.exists():
            p.unlink()
    except Exception:
        pass
    db.delete(m)
    db.commit()
    return _admin_back(request, app_id, "文件已删除")


@app.post("/admin/application/{app_id}/edit-cat", name="admin_edit_cat")
async def admin_edit_cat(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    if form.get("csrf_token") != request.session.get("csrf_token"):
        return RedirectResponse("/admin?err=csrf", status_code=303)
    row = db.get(Application, app_id)
    if not row:
        return RedirectResponse("/admin?err=申请不存在", status_code=303)
    cat_nickname = (form.get("cat_nickname") or "").strip()
    cat_gender = (form.get("cat_gender") or "").strip()
    age_estimate = (form.get("age_estimate") or "").strip()
    health_note = (form.get("health_note") or "").strip()
    clinic_store = (form.get("clinic_store") or "").strip()
    if cat_nickname:
        row.cat_nickname = cat_nickname
    if cat_gender in ("male", "female", "unknown"):
        row.cat_gender = cat_gender
    row.age_estimate = age_estimate
    row.health_note = health_note
    if clinic_store:
        row.clinic_store = clinic_store
    db.commit()
    return _admin_back(request, app_id, f"猫咪信息已更新")


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
        # ── 自动创建/合并客户档案 ──
        _admin_appt_cust_id = None
        try:
            _admin_appt_cust = _upsert_customer(
                db,
                name=str(fields["customer_name"]),
                phone=str(fields["phone"]),
                source=str(fields["category"]),
            )
            _admin_appt_cust_id = _admin_appt_cust.id
        except Exception:
            _admin_appt_cust_id = None
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
            customer_id=_admin_appt_cust_id,
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
    return _admin_back(request, app_id)


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
    return _admin_back(request, app_id)


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
    return _admin_back(request, app_id)


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
            )
            + f"#app-{app_id}",
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
    return _admin_back(request, app_id)


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
    # ── 自动创建/合并客户档案 ──
    _appt_cust_id = None
    try:
        _appt_cust = _upsert_customer(
            db,
            name=str(fields["customer_name"]),
            phone=str(fields["phone"]),
            openid=openid,
            source=str(fields["category"]),
        )
        _appt_cust_id = _appt_cust.id
    except Exception:
        _appt_cust_id = None
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
        customer_id=_appt_cust_id,
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


@app.post("/api/wechat/appointments/{appointment_id}/get")
async def api_wechat_appointment_get(
    appointment_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """小程序端获取单条预约详情（openid 校验）。"""
    openid = await _resolve_wechat_openid(payload)
    row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not row:
        raise HTTPException(404, "预约记录不存在")
    if (row.wechat_openid or "") != openid:
        raise HTTPException(403, "无权查看此预约")
    return _serialize_appointment(row)


@app.post("/api/wechat/appointments/{appointment_id}/update")
async def api_wechat_appointment_update(
    appointment_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """小程序端修改自己的预约（仅限 pending 状态，openid 校验，时段容量重新检验）。"""
    openid = await _resolve_wechat_openid(payload)
    row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not row:
        raise HTTPException(404, "预约记录不存在")
    if (row.wechat_openid or "") != openid:
        raise HTTPException(403, "无权操作此预约")
    if row.status != AppointmentStatus.pending.value:
        raise HTTPException(400, "只有「待确认」状态的预约才能修改，如需调整请联系医院")

    new_date  = str(payload.get("appointment_date", "") or row.appointment_date).strip()
    new_time  = str(payload.get("appointment_time", "") or row.appointment_time).strip()
    new_store = str(payload.get("store", "") or row.store or "").strip()

    # 若日期、时间或门店有变，重新做容量检查（exclude 自身，避免自我碰撞）
    if new_date != row.appointment_date or new_time != row.appointment_time or new_store != (row.store or ""):
        err = _check_slot_capacity(
            db, new_store, new_date, new_time,
            row.category, row.service_name or "",
            exclude_id=appointment_id,
        )
        if err:
            raise HTTPException(400, err)

    row.appointment_date = new_date
    row.appointment_time = new_time
    if new_store:
        row.store = new_store
    if "customer_name" in payload and payload["customer_name"]:
        row.customer_name = str(payload["customer_name"])[:80]
    if "phone" in payload and payload["phone"]:
        row.phone = str(payload["phone"])[:20]
    if "pet_name" in payload and payload["pet_name"]:
        row.pet_name = str(payload["pet_name"])[:80]
    if "pet_gender" in payload and payload["pet_gender"]:
        row.pet_gender = str(payload["pet_gender"])[:20]
    if "notes" in payload:
        row.notes = str(payload.get("notes") or "")[:500]

    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return {"ok": True, "appointment": _serialize_appointment(row)}


@app.post("/api/wechat/feedback/create")
async def api_wechat_feedback_create(payload: dict = Body(...), db: Session = Depends(get_db)):
    from app.models import Feedback
    openid = str((payload or {}).get("openid", "") or "").strip()
    content = str((payload or {}).get("content", "") or "").strip()
    if not content:
        raise HTTPException(400, "请填写反馈内容")
    fb = Feedback(openid=openid, content=content[:2000])
    db.add(fb)
    db.commit()
    db.refresh(fb)
    # create upload dir
    fb_dir = Path(settings.upload_dir) / "feedback" / str(fb.id)
    fb_dir.mkdir(parents=True, exist_ok=True)
    return {"id": fb.id, "ok": True}


@app.post("/api/wechat/feedback/{feedback_id}/upload")
async def api_wechat_feedback_upload(
    feedback_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    from app.models import Feedback
    import json as _json
    fb = db.get(Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, "反馈记录不存在")
    ext = Path(file.filename or "img.jpg").suffix.lower() or ".jpg"
    safe_ext = ext if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") else ".jpg"
    fb_dir = Path(settings.upload_dir) / "feedback" / str(feedback_id)
    fb_dir.mkdir(parents=True, exist_ok=True)
    existing = _json.loads(fb.image_paths or "[]")
    if len(existing) >= 6:
        raise HTTPException(400, "最多上传6张截图")
    fname = f"{len(existing)+1}{safe_ext}"
    dest = fb_dir / fname
    content_bytes = await file.read()
    dest.write_bytes(content_bytes)
    stored = str(dest)
    existing.append(stored)
    fb.image_paths = _json.dumps(existing, ensure_ascii=False)
    db.commit()
    return {"ok": True, "path": stored}


@app.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_page(request: Request, status: str = "", db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login", status_code=303)
    from app.models import Feedback
    q = db.query(Feedback).order_by(Feedback.created_at.desc())
    if status == "pending":
        q = q.filter(Feedback.status == "pending")
    elif status == "resolved":
        q = q.filter(Feedback.status == "resolved")
    items = q.limit(100).all()
    # Build image URLs for each feedback
    import json as _json
    feed_list = []
    for fb in items:
        paths = _json.loads(fb.image_paths or "[]")
        img_urls = []
        for p in paths:
            # Convert stored path to URL
            try:
                rel = Path(p).relative_to(Path(settings.upload_dir))
                img_urls.append("/uploads/" + str(rel).replace("\\", "/"))
            except Exception:
                pass
        feed_list.append({"fb": fb, "img_urls": img_urls})
    pending_count = db.query(Feedback).filter(Feedback.status == "pending").count()
    return templates.TemplateResponse("admin_feedback.html", {
        "request": request,
        "title": "客户反馈",
        "feed_list": feed_list,
        "pending_count": pending_count,
        "status_filter": status,
        "csrf_token": _get_csrf_token(request),
    })


@app.post("/admin/feedback/{feedback_id}/resolve")
async def admin_feedback_resolve(
    feedback_id: int,
    request: Request,
    admin_note: str = Form(""),
    db: Session = Depends(get_db),
):
    if not request.session.get("admin"):
        raise HTTPException(403)
    from app.models import Feedback
    fb = db.get(Feedback, feedback_id)
    if not fb:
        raise HTTPException(404)
    fb.status = "resolved"
    fb.admin_note = admin_note.strip()[:500]
    fb.resolved_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/feedback?msg=已处理#{feedback_id}", status_code=303)


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


def _compress_image(src: Path, max_px: int = 1920, quality: int = 85) -> Path:
    try:
        from PIL import Image, ExifTags
        img = Image.open(src)
        # 按 EXIF 自动旋转
        try:
            exif = img._getexif()
            if exif:
                for tag, val in exif.items():
                    if ExifTags.TAGS.get(tag) == "Orientation":
                        if val == 3:
                            img = img.rotate(180, expand=True)
                        elif val == 6:
                            img = img.rotate(270, expand=True)
                        elif val == 8:
                            img = img.rotate(90, expand=True)
                        break
        except Exception:
            pass
        if max(img.width, img.height) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        dest = src.with_suffix(".jpg")
        img.save(dest, "JPEG", quality=quality, optimize=True)
        if src.suffix.lower() not in (".jpg", ".jpeg"):
            src.unlink(missing_ok=True)
        return dest
    except Exception as e:
        logging.warning(f"图片压缩失败，保留原文件：{e}")
        return src


def _transcode_to_h264(src: Path) -> Path:
    """
    用 ffmpeg 将视频转码为 H.264 MP4（最广兼容格式）。
    转码成功返回新路径（.mp4），失败则返回原路径（不影响上传流程）。
    """
    dest = src.with_suffix(".mp4")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",  # 元数据前置，支持边下边播
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # 确保宽高为偶数
                str(dest),
            ],
            timeout=300,
            capture_output=True,
        )
        if result.returncode == 0 and dest.exists():
            if src.suffix.lower() != ".mp4":
                src.unlink(missing_ok=True)  # 删除原始文件节省空间
            return dest
        else:
            logging.warning(f"ffmpeg 转码失败：{result.stderr.decode(errors='replace')[:300]}")
            return src
    except FileNotFoundError:
        logging.warning("ffmpeg 未安装，跳过转码。建议服务器执行：apt install ffmpeg")
        return src
    except Exception as e:
        logging.warning(f"ffmpeg 转码异常：{e}")
        return src


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
            if is_video:
                dest = _transcode_to_h264(dest)
            else:
                dest = _compress_image(dest)  # 压缩图片至 1920px/JPEG85，加快加载
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
    return _admin_back(request, app_id)


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
    # 统一将常见视频格式映射为 video/mp4，确保浏览器和小程序正常播放
    ext = path.suffix.lower()
    if ext in (".mp4", ".m4v", ".mov"):
        ctype = "video/mp4"
    elif ext in (".webm",):
        ctype = "video/webm"
    elif ext in (".avi",):
        ctype = "video/x-msvideo"
    return FileResponse(path, media_type=ctype or "application/octet-stream", headers={"Accept-Ranges": "bytes"})


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
    return _admin_back(request, app_id)


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
    return _admin_back(request, app_id)


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
    return _admin_back(request, app_id)


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
    return _admin_back(request, app_id)


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


# ══════════════════════ 客户档案 CRM ══════════════════════

@app.get("/admin/customers", response_class=HTMLResponse)
async def page_admin_customers(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(""),
    page: int = Query(1),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    PAGE_SIZE = 30
    query = db.query(Customer)
    q = q.strip()
    if q:
        query = query.filter(
            or_(
                Customer.name.ilike(f"%{q}%"),
                Customer.phone.ilike(f"%{q}%"),
            )
        )
    total = query.count()
    customers = query.order_by(Customer.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return templates.TemplateResponse(
        request,
        "admin_customers.html",
        {
            "customers": customers,
            "q": q,
            "page": page,
            "total": total,
            "page_size": PAGE_SIZE,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        },
    )


@app.post("/admin/customers/create")
async def admin_customer_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
    source: str = Form("manual"),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    cust = Customer(
        name=name.strip()[:120],
        phone=phone.strip()[:40],
        address=address.strip()[:500],
        notes=notes.strip(),
        source=source.strip()[:40] or "manual",
    )
    db.add(cust)
    db.commit()
    return RedirectResponse(f"/admin/customers/{cust.id}?msg=客户已创建", status_code=303)


@app.get("/admin/customers/{customer_id}", response_class=HTMLResponse)
async def page_admin_customer_detail(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(404, "客户不存在")
    pets = db.query(Pet).filter(Pet.customer_id == customer_id).order_by(Pet.id.desc()).all()
    applications = db.query(Application).filter(Application.customer_id == customer_id).order_by(Application.id.desc()).limit(50).all()
    appointments = db.query(Appointment).filter(Appointment.customer_id == customer_id).order_by(Appointment.id.desc()).limit(50).all()
    visits = db.query(Visit).filter(Visit.customer_id == customer_id).order_by(Visit.visit_date.desc(), Visit.id.desc()).limit(100).all()
    visits_by_pet: dict[int, list] = {}
    visits_no_pet = []
    for vis in visits:
        if vis.pet_id:
            visits_by_pet.setdefault(vis.pet_id, []).append(vis)
        else:
            visits_no_pet.append(vis)
    pet_map = {p.id: p for p in pets}
    cust_sales_orders = db.query(SalesOrder).filter(SalesOrder.customer_id == customer_id).order_by(SalesOrder.id.desc()).limit(100).all()
    _SO_STATUS_ZH_LOCAL = {"pending": "待付款", "paid": "已收款", "cancelled": "已取消"}
    # 疫苗记录（按宠物分组）
    vacc_list = db.query(Vaccination).filter(Vaccination.customer_id == customer_id).order_by(Vaccination.vaccinated_date.desc()).all()
    vaccs_by_pet: dict[int, list] = {}
    for v in vacc_list:
        if v.pet_id:
            vaccs_by_pet.setdefault(v.pet_id, []).append(v)
    from datetime import date, timedelta
    today_str = date.today().isoformat()
    soon_str  = (date.today() + timedelta(days=7)).isoformat()
    return templates.TemplateResponse(
        request,
        "admin_customer_detail.html",
        {
            "cust": cust,
            "pets": pets,
            "applications": applications,
            "appointments": appointments,
            "visits": visits,
            "visits_by_pet": visits_by_pet,
            "visits_no_pet": visits_no_pet,
            "pet_map": pet_map,
            "visit_type_zh": _VISIT_TYPE_ZH,
            "sales_orders": cust_sales_orders,
            "so_status_zh": _SO_STATUS_ZH_LOCAL,
            "vaccs_by_pet": vaccs_by_pet,
            "vacc_type_zh": _VACC_TYPE_ZH,
            "today_str": today_str,
            "soon_str": soon_str,
            "csrf_token": _get_csrf_token(request),
        },
    )


@app.post("/admin/customers/{customer_id}/edit")
async def admin_customer_edit(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(404, "客户不存在")
    cust.name = name.strip()[:120]
    cust.phone = phone.strip()[:40]
    cust.address = address.strip()[:500]
    cust.notes = notes.strip()
    db.commit()
    return RedirectResponse(f"/admin/customers/{customer_id}?msg=已保存", status_code=303)


@app.post("/admin/customers/{customer_id}/pets/add")
async def admin_customer_add_pet(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    species: str = Form("cat"),
    breed: str = Form(""),
    gender: str = Form("unknown"),
    birthday_estimate: str = Form(""),
    is_neutered: str = Form(""),
    color_pattern: str = Form(""),
    is_stray: str = Form(""),
    microchip_id: str = Form(""),
    notes: str = Form(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(404, "客户不存在")
    pet = Pet(
        customer_id=customer_id,
        name=name.strip()[:120],
        species=species.strip()[:40] or "cat",
        breed=breed.strip()[:80],
        gender=gender.strip()[:10] or "unknown",
        birthday_estimate=birthday_estimate.strip()[:40],
        is_neutered=is_neutered.lower() in ("1", "true", "on", "yes"),
        color_pattern=color_pattern.strip()[:80],
        is_stray=is_stray.lower() in ("1", "true", "on", "yes"),
        microchip_id=microchip_id.strip()[:40],
        notes=notes.strip(),
    )
    db.add(pet)
    db.commit()
    return RedirectResponse(f"/admin/customers/{customer_id}?msg=宠物已添加", status_code=303)


@app.post("/admin/customers/{customer_id}/pets/{pet_id}/edit")
async def admin_customer_edit_pet(
    customer_id: int,
    pet_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    species: str = Form("cat"),
    breed: str = Form(""),
    gender: str = Form("unknown"),
    birthday_estimate: str = Form(""),
    is_neutered: str = Form(""),
    color_pattern: str = Form(""),
    is_stray: str = Form(""),
    microchip_id: str = Form(""),
    notes: str = Form(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    pet = db.get(Pet, pet_id)
    if not pet or pet.customer_id != customer_id:
        raise HTTPException(404, "宠物不存在")
    pet.name = name.strip()[:120]
    pet.species = species.strip()[:40] or "cat"
    pet.breed = breed.strip()[:80]
    pet.gender = gender.strip()[:10] or "unknown"
    pet.birthday_estimate = birthday_estimate.strip()[:40]
    pet.is_neutered = is_neutered.lower() in ("1", "true", "on", "yes")
    pet.color_pattern = color_pattern.strip()[:80]
    pet.is_stray = is_stray.lower() in ("1", "true", "on", "yes")
    pet.microchip_id = microchip_id.strip()[:40]
    pet.notes = notes.strip()
    db.commit()
    return RedirectResponse(f"/admin/customers/{customer_id}?msg=宠物已更新", status_code=303)


# ---------------------------------------------------------------------------
# Phase 2 — 就诊病历 (Visits)
# ---------------------------------------------------------------------------

_VISIT_TYPE_ZH = {
    "outpatient": "门诊",
    "followup": "复诊",
    "postop": "术后复查",
    "vaccine": "疫苗接种",
    "surgery_consult": "手术咨询",
    "other": "其他",
}


@app.get("/admin/visits", response_class=HTMLResponse)
async def page_admin_visits(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(""),
    visit_type: str = Query(""),
    vet: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    pet_id: int = Query(0),
    customer_id: int = Query(0),
    page: int = Query(1),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    query = db.query(Visit)
    if q:
        query = query.join(Customer, Visit.customer_id == Customer.id, isouter=True)\
                     .join(Pet, Visit.pet_id == Pet.id, isouter=True)\
                     .filter(or_(
                         Customer.name.ilike(f"%{q}%"),
                         Customer.phone.ilike(f"%{q}%"),
                         Pet.name.ilike(f"%{q}%"),
                         Visit.diagnosis.ilike(f"%{q}%"),
                     ))
    if visit_type:
        query = query.filter(Visit.visit_type == visit_type)
    if vet:
        query = query.filter(Visit.vet_name.ilike(f"%{vet}%"))
    if date_from:
        query = query.filter(Visit.visit_date >= date_from)
    if date_to:
        query = query.filter(Visit.visit_date <= date_to)
    if pet_id:
        query = query.filter(Visit.pet_id == pet_id)
    if customer_id:
        query = query.filter(Visit.customer_id == customer_id)
    total = query.count()
    page_size = 30
    visits = query.order_by(Visit.visit_date.desc(), Visit.id.desc())\
                  .offset((page - 1) * page_size).limit(page_size).all()
    # 预加载 customer/pet 名字
    cust_map = {}
    pet_map = {}
    for v in visits:
        if v.customer_id and v.customer_id not in cust_map:
            c = db.get(Customer, v.customer_id)
            if c:
                cust_map[v.customer_id] = c
        if v.pet_id and v.pet_id not in pet_map:
            p = db.get(Pet, v.pet_id)
            if p:
                pet_map[v.pet_id] = p
    return templates.TemplateResponse(request, "admin_visits.html", {
        "visits": visits,
        "cust_map": cust_map,
        "pet_map": pet_map,
        "visit_type_zh": _VISIT_TYPE_ZH,
        "total": total,
        "page": page,
        "page_size": page_size,
        "filters": {"q": q, "visit_type": visit_type, "vet": vet,
                    "date_from": date_from, "date_to": date_to},
    })


@app.get("/admin/visits/create", response_class=HTMLResponse)
async def page_admin_visit_create(
    request: Request,
    db: Session = Depends(get_db),
    customer_id: int = Query(0),
    pet_id: int = Query(0),
    appointment_id: int = Query(0),
    search_q: str = Query(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    cust = db.get(Customer, customer_id) if customer_id else None
    pet = db.get(Pet, pet_id) if pet_id else None
    appt = db.get(Appointment, appointment_id) if appointment_id else None
    pets = db.query(Pet).filter(Pet.customer_id == customer_id).all() if customer_id else []
    vets = db.query(Staff.name).filter(
        Staff.status.in_(["active", "probation"]),
        Staff.position.ilike("%医%")
    ).all()
    vet_names = [v[0] for v in vets]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # 客户搜索结果
    search_results = []
    if search_q and not customer_id:
        search_results = db.query(Customer).filter(
            or_(
                Customer.name.ilike(f"%{search_q}%"),
                Customer.phone.ilike(f"%{search_q}%"),
            )
        ).limit(10).all()
    return templates.TemplateResponse(request, "admin_visit_form.html", {
        "cust": cust,
        "pet": pet,
        "pets": pets,
        "appt": appt,
        "vet_names": vet_names,
        "visit_type_zh": _VISIT_TYPE_ZH,
        "today": today,
        "csrf_token": _get_csrf_token(request),
        "mode": "create",
        "search_q": search_q,
        "search_results": search_results,
    })


@app.post("/admin/visits/create")
async def admin_visit_create(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    customer_id: int = Form(0),
    pet_id: int = Form(0),
    appointment_id: int = Form(0),
    visit_date: str = Form(""),
    visit_type: str = Form("outpatient"),
    chief_complaint: str = Form(""),
    physical_exam: str = Form(""),
    diagnosis: str = Form(""),
    treatment_plan: str = Form(""),
    notes: str = Form(""),
    vet_name: str = Form(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    _require_csrf(request, csrf_token)
    v = Visit(
        customer_id=customer_id or None,
        pet_id=pet_id or None,
        appointment_id=appointment_id or None,
        visit_date=visit_date.strip()[:20],
        visit_type=visit_type.strip()[:40] or "outpatient",
        chief_complaint=chief_complaint.strip(),
        physical_exam=physical_exam.strip(),
        diagnosis=diagnosis.strip(),
        treatment_plan=treatment_plan.strip(),
        notes=notes.strip(),
        vet_name=vet_name.strip()[:80],
        created_by=request.session.get("admin_username", "admin"),
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    # 如果是从预约完成时创建，更新预约状态
    if appointment_id:
        appt = db.get(Appointment, appointment_id)
        if appt and appt.status == AppointmentStatus.confirmed.value:
            appt.status = AppointmentStatus.completed.value
            db.commit()
    if customer_id:
        return RedirectResponse(f"/admin/customers/{customer_id}?msg=就诊记录已创建", status_code=303)
    return RedirectResponse(f"/admin/visits/{v.id}?msg=就诊记录已创建", status_code=303)


@app.get("/admin/visits/{visit_id}", response_class=HTMLResponse)
async def page_admin_visit_detail(
    visit_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(404, "就诊记录不存在")
    cust = db.get(Customer, v.customer_id) if v.customer_id else None
    pet = db.get(Pet, v.pet_id) if v.pet_id else None
    pets = db.query(Pet).filter(Pet.customer_id == v.customer_id).all() if v.customer_id else []
    vets = db.query(Staff.name).filter(
        Staff.status.in_(["active", "probation"]),
        Staff.position.ilike("%医%")
    ).all()
    vet_names = [v2[0] for v2 in vets]
    prescriptions = db.query(Prescription).filter(Prescription.visit_id == visit_id).order_by(Prescription.id.desc()).all()
    sales_orders = db.query(SalesOrder).filter(SalesOrder.visit_id == visit_id).order_by(SalesOrder.id.desc()).all()
    invoices = db.query(Invoice).filter(Invoice.visit_id == visit_id).order_by(Invoice.id.desc()).all()
    _PRESC_STATUS_ZH = {"draft": "草稿", "issued": "已开具", "dispensed": "已发药"}
    _SO_STATUS_ZH = {"pending": "待付款", "paid": "已收款", "cancelled": "已取消"}
    return templates.TemplateResponse(request, "admin_visit_form.html", {
        "visit": v,
        "cust": cust,
        "pet": pet,
        "pets": pets,
        "vet_names": vet_names,
        "visit_type_zh": _VISIT_TYPE_ZH,
        "prescriptions": prescriptions,
        "sales_orders": sales_orders,
        "invoices": invoices,
        "presc_status_zh": _PRESC_STATUS_ZH,
        "so_status_zh": _SO_STATUS_ZH,
        "inv_status_zh": _INV_STATUS_ZH,
        "csrf_token": _get_csrf_token(request),
        "mode": "edit",
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/visits/{visit_id}/edit")
async def admin_visit_edit(
    visit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    pet_id: int = Form(0),
    visit_date: str = Form(""),
    visit_type: str = Form("outpatient"),
    chief_complaint: str = Form(""),
    physical_exam: str = Form(""),
    diagnosis: str = Form(""),
    treatment_plan: str = Form(""),
    notes: str = Form(""),
    vet_name: str = Form(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    _require_csrf(request, csrf_token)
    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(404, "就诊记录不存在")
    v.pet_id = pet_id or v.pet_id
    v.visit_date = visit_date.strip()[:20]
    v.visit_type = visit_type.strip()[:40] or "outpatient"
    v.chief_complaint = chief_complaint.strip()
    v.physical_exam = physical_exam.strip()
    v.diagnosis = diagnosis.strip()
    v.treatment_plan = treatment_plan.strip()
    v.notes = notes.strip()
    v.vet_name = vet_name.strip()[:80]
    db.commit()
    return RedirectResponse(f"/admin/visits/{visit_id}?msg=已保存", status_code=303)


@app.post("/admin/visits/{visit_id}/delete")
async def admin_visit_delete(
    visit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    _require_csrf(request, csrf_token)
    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(404, "就诊记录不存在")
    customer_id = v.customer_id
    db.delete(v)
    db.commit()
    if customer_id:
        return RedirectResponse(f"/admin/customers/{customer_id}?msg=就诊记录已删除", status_code=303)
    return RedirectResponse("/admin/visits?msg=就诊记录已删除", status_code=303)


# ---------------------------------------------------------------------------
# Phase 3 — 处方单 (Prescriptions)
# ---------------------------------------------------------------------------

_PRESC_STATUS_ZH = {"draft": "草稿", "issued": "已开具", "dispensed": "已发药"}
_DRUG_TYPE_ZH = {"oral": "口服", "topical": "外用", "injection": "注射", "eye_drop": "滴眼", "other": "其他"}


def _parse_presc_items(form_data) -> list[dict]:
    items = []
    i = 0
    while True:
        name = form_data.get(f"drug_name_{i}", "").strip()
        if not name and i > 20:
            break
        if name:
            try:
                qty_num = float(form_data.get(f"quantity_num_{i}", 1) or 1)
                unit_price = float(form_data.get(f"unit_price_{i}", 0) or 0)
            except ValueError:
                qty_num, unit_price = 1.0, 0.0
            raw_item_id = form_data.get(f"item_id_{i}", "").strip()
            item_id = int(raw_item_id) if raw_item_id and raw_item_id.isdigit() else None
            subtotal = round(qty_num * unit_price, 2)
            items.append({
                "item_id": item_id,
                "drug_name": name,
                "drug_type": form_data.get(f"drug_type_{i}", "oral").strip(),
                "dosage": form_data.get(f"dosage_{i}", "").strip(),
                "frequency": form_data.get(f"frequency_{i}", "").strip(),
                "duration_days": form_data.get(f"duration_days_{i}", "").strip(),
                "quantity_num": qty_num,
                "quantity": form_data.get(f"quantity_{i}", "").strip(),
                "unit_price": unit_price,
                "subtotal": subtotal,
                "instructions": form_data.get(f"instructions_{i}", "").strip(),
            })
        i += 1
    return items


def _deduct_inventory(db: Session, item_id: int, qty: float, ref_type: str, ref_id: int, operator: str, note: str = "") -> None:
    """出库：减少库存，写流水。"""
    inv = db.get(InventoryItem, item_id)
    if not inv or inv.is_service:
        return
    before = inv.stock_qty
    inv.stock_qty = round(before - qty, 4)
    db.add(InventoryTransaction(
        item_id=item_id, tx_type="out", qty=qty,
        qty_before=before, qty_after=inv.stock_qty,
        unit_price=inv.sell_price,
        ref_type=ref_type, ref_id=ref_id,
        operator=operator, note=note,
    ))


def _restore_inventory(db: Session, item_id: int, qty: float, ref_type: str, ref_id: int, operator: str, note: str = "") -> None:
    """退回库存（删单时）：增加库存，写流水。"""
    inv = db.get(InventoryItem, item_id)
    if not inv or inv.is_service:
        return
    before = inv.stock_qty
    inv.stock_qty = round(before + qty, 4)
    db.add(InventoryTransaction(
        item_id=item_id, tx_type="return", qty=qty,
        qty_before=before, qty_after=inv.stock_qty,
        unit_price=inv.sell_price,
        ref_type=ref_type, ref_id=ref_id,
        operator=operator, note=note,
    ))


@app.get("/admin/prescriptions/create", response_class=HTMLResponse)
async def page_admin_presc_create(
    request: Request,
    db: Session = Depends(get_db),
    visit_id: int = Query(0),
    customer_id: int = Query(0),
    pet_id: int = Query(0),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    visit = db.get(Visit, visit_id) if visit_id else None
    if visit:
        customer_id = customer_id or visit.customer_id or 0
        pet_id = pet_id or visit.pet_id or 0
    cust = db.get(Customer, customer_id) if customer_id else None
    pet = db.get(Pet, pet_id) if pet_id else None
    pets = db.query(Pet).filter(Pet.customer_id == customer_id).all() if customer_id else []
    vets = db.query(Staff.name).filter(
        Staff.status.in_(["active", "probation"]),
        Staff.position.ilike("%医%")
    ).all()
    vet_names = [v[0] for v in vets]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return templates.TemplateResponse(request, "admin_prescription_form.html", {
        "presc": None, "visit": visit, "cust": cust, "pet": pet, "pets": pets,
        "vet_names": vet_names, "drug_type_zh": _DRUG_TYPE_ZH,
        "presc_status_zh": _PRESC_STATUS_ZH,
        "today": today, "csrf_token": _get_csrf_token(request), "mode": "create",
    })


@app.post("/admin/prescriptions/create")
async def admin_presc_create(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    visit_id = int(form.get("visit_id", 0) or 0)
    customer_id = int(form.get("customer_id", 0) or 0)
    pet_id = int(form.get("pet_id", 0) or 0)
    parsed_items = _parse_presc_items(form)
    total = round(sum(it["subtotal"] for it in parsed_items), 2)
    operator = request.session.get("admin_username", "admin")
    presc = Prescription(
        visit_id=visit_id or None,
        customer_id=customer_id or None,
        pet_id=pet_id or None,
        prescribed_date=str(form.get("prescribed_date", "")).strip()[:20],
        vet_name=str(form.get("vet_name", "")).strip()[:80],
        status=str(form.get("status", "issued")).strip(),
        total_amount=total,
        notes=str(form.get("notes", "")).strip(),
        created_by=operator,
    )
    db.add(presc)
    db.flush()
    for it in parsed_items:
        db.add(PrescriptionItem(prescription_id=presc.id, **it))
        if it["item_id"] and it["quantity_num"] > 0:
            _deduct_inventory(db, it["item_id"], it["quantity_num"],
                              "prescription", presc.id, operator, f"处方#{presc.id}")
    db.commit()
    return RedirectResponse(f"/admin/visits/{visit_id}?msg=处方单已开具" if visit_id else f"/admin/prescriptions/{presc.id}?msg=处方单已创建", status_code=303)


@app.get("/admin/prescriptions/{presc_id}", response_class=HTMLResponse)
async def page_admin_presc_detail(presc_id: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    presc = db.get(Prescription, presc_id)
    if not presc:
        raise HTTPException(404, "处方单不存在")
    visit = db.get(Visit, presc.visit_id) if presc.visit_id else None
    cust = db.get(Customer, presc.customer_id) if presc.customer_id else None
    pet = db.get(Pet, presc.pet_id) if presc.pet_id else None
    pets = db.query(Pet).filter(Pet.customer_id == presc.customer_id).all() if presc.customer_id else []
    vets = db.query(Staff.name).filter(Staff.status.in_(["active", "probation"]), Staff.position.ilike("%医%")).all()
    vet_names = [v[0] for v in vets]
    return templates.TemplateResponse(request, "admin_prescription_form.html", {
        "presc": presc, "visit": visit, "cust": cust, "pet": pet, "pets": pets,
        "vet_names": vet_names, "drug_type_zh": _DRUG_TYPE_ZH,
        "presc_status_zh": _PRESC_STATUS_ZH,
        "csrf_token": _get_csrf_token(request), "mode": "edit",
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/prescriptions/{presc_id}/edit")
async def admin_presc_edit(presc_id: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    presc = db.get(Prescription, presc_id)
    if not presc:
        raise HTTPException(404)
    operator = request.session.get("admin_username", "admin")
    # 先把旧明细的库存退回
    for old in presc.items:
        if old.item_id and old.quantity_num > 0:
            _restore_inventory(db, old.item_id, old.quantity_num,
                               "prescription", presc_id, operator, f"编辑处方#{presc_id}退回")
        db.delete(old)
    db.flush()
    parsed_items = _parse_presc_items(form)
    total = round(sum(it["subtotal"] for it in parsed_items), 2)
    presc.prescribed_date = str(form.get("prescribed_date", "")).strip()[:20]
    presc.vet_name = str(form.get("vet_name", "")).strip()[:80]
    presc.pet_id = int(form.get("pet_id", 0) or 0) or presc.pet_id
    presc.status = str(form.get("status", "issued")).strip()
    presc.total_amount = total
    presc.notes = str(form.get("notes", "")).strip()
    for it in parsed_items:
        db.add(PrescriptionItem(prescription_id=presc_id, **it))
        if it["item_id"] and it["quantity_num"] > 0:
            _deduct_inventory(db, it["item_id"], it["quantity_num"],
                              "prescription", presc_id, operator, f"处方#{presc_id}")
    db.commit()
    return RedirectResponse(f"/admin/prescriptions/{presc_id}?msg=已保存", status_code=303)


@app.post("/admin/prescriptions/{presc_id}/delete")
async def admin_presc_delete(presc_id: int, request: Request, db: Session = Depends(get_db),
                              csrf_token: str = Form("")):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    _require_csrf(request, csrf_token)
    presc = db.get(Prescription, presc_id)
    if not presc:
        raise HTTPException(404)
    operator = request.session.get("admin_username", "admin")
    visit_id = presc.visit_id
    for it in presc.items:
        if it.item_id and it.quantity_num > 0:
            _restore_inventory(db, it.item_id, it.quantity_num,
                               "prescription", presc_id, operator, f"删除处方#{presc_id}退回")
    db.delete(presc)
    db.commit()
    return RedirectResponse(f"/admin/visits/{visit_id}?msg=处方单已删除" if visit_id else "/admin/visits", status_code=303)


# ---------------------------------------------------------------------------
# 发药工作台 (Dispensing Workbench)
# ---------------------------------------------------------------------------

@app.get("/admin/dispensing", response_class=HTMLResponse)
async def admin_dispensing(
    request: Request, db: Session = Depends(get_db),
    q: str = Query(""),
    status: str = Query("issued"),
    vet: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    quick: str = Query(""),        # today / week / month
):
    require_admin(request)
    from datetime import date as _date, timedelta as _timedelta

    # 快捷时间范围
    today = _date.today()
    if quick == "today":
        date_from = date_to = today.isoformat()
    elif quick == "week":
        date_from = (today - _timedelta(days=today.weekday())).isoformat()
        date_to = today.isoformat()
    elif quick == "month":
        date_from = today.replace(day=1).isoformat()
        date_to = today.isoformat()

    qry = db.query(Prescription)
    if status and status != "all":
        qry = qry.filter(Prescription.status == status)
    if vet:
        qry = qry.filter(Prescription.vet_name == vet)
    if date_from:
        qry = qry.filter(Prescription.prescribed_date >= date_from)
    if date_to:
        qry = qry.filter(Prescription.prescribed_date <= date_to)
    if q:
        # 搜索患者姓名 / 宠物名 / 药品名（通过 subquery）
        drug_ids = db.query(PrescriptionItem.prescription_id).filter(
            PrescriptionItem.drug_name.ilike(f"%{q}%")
        ).subquery()
        pet_ids = db.query(Pet.id).filter(Pet.name.ilike(f"%{q}%")).subquery()
        cust_ids = db.query(Customer.id).filter(
            or_(Customer.name.ilike(f"%{q}%"), Customer.phone.ilike(f"%{q}%"))
        ).subquery()
        qry = qry.filter(or_(
            Prescription.id.in_(drug_ids),
            Prescription.pet_id.in_(pet_ids),
            Prescription.customer_id.in_(cust_ids),
        ))

    # 待发药队列：最早开单排前；其余倒序
    if status == "issued":
        prescs = qry.order_by(Prescription.prescribed_date.asc(), Prescription.id.asc()).limit(200).all()
    else:
        prescs = qry.order_by(Prescription.id.desc()).limit(200).all()

    # 预载关联数据
    cust_map = {}
    pet_map = {}
    for p in prescs:
        if p.customer_id and p.customer_id not in cust_map:
            c = db.get(Customer, p.customer_id)
            if c:
                cust_map[p.customer_id] = c
        if p.pet_id and p.pet_id not in pet_map:
            pt = db.get(Pet, p.pet_id)
            if pt:
                pet_map[p.pet_id] = pt

    # 统计数字
    pending_count = db.query(Prescription).filter(Prescription.status == "issued").count()
    today_str = today.isoformat()
    today_dispensed = db.query(Prescription).filter(
        Prescription.status == "dispensed",
        Prescription.prescribed_date == today_str,
    ).count()
    month_start = today.replace(day=1).isoformat()
    month_total = db.query(Prescription).filter(
        Prescription.prescribed_date >= month_start,
    ).count()

    # 在职医生列表
    vets = [r[0] for r in db.query(Prescription.vet_name).filter(
        Prescription.vet_name != ""
    ).distinct().order_by(Prescription.vet_name).all()]

    return templates.TemplateResponse("admin_dispensing.html", {
        "request": request,
        "prescs": prescs, "cust_map": cust_map, "pet_map": pet_map,
        "q": q, "status": status, "vet": vet,
        "date_from": date_from, "date_to": date_to, "quick": quick,
        "pending_count": pending_count,
        "today_dispensed": today_dispensed,
        "month_total": month_total,
        "vets": vets,
        "presc_status_zh": _PRESC_STATUS_ZH,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": "发药工作台",
    })


@app.post("/admin/dispensing/{presc_id}/dispense")
async def admin_dispensing_dispense(
    presc_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    presc = db.get(Prescription, presc_id)
    if not presc or presc.status != "issued":
        raise HTTPException(400, "处方单状态不符，无法发药")
    presc.status = "dispensed"
    db.commit()
    return RedirectResponse("/admin/dispensing?msg=已确认发药", status_code=303)


@app.post("/admin/dispensing/{presc_id}/undispense")
async def admin_dispensing_undispense(
    presc_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    if request.session.get("admin_role") != "superadmin":
        raise HTTPException(403, "仅超级管理员可撤回发药")
    presc = db.get(Prescription, presc_id)
    if not presc or presc.status != "dispensed":
        raise HTTPException(400, "仅已发药的处方单可撤回")
    presc.status = "issued"
    db.commit()
    return RedirectResponse(f"/admin/dispensing?msg=已撤回至待发药&status=all", status_code=303)


@app.post("/admin/dispensing/bulk-dispense")
async def admin_dispensing_bulk(
    request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    form = await request.form()
    ids = [int(v) for k, v in form.multi_items() if k == "presc_ids" and v.isdigit()]
    if not ids:
        return RedirectResponse("/admin/dispensing?err=未选择处方单", status_code=303)
    updated = 0
    for pid in ids:
        p = db.get(Prescription, pid)
        if p and p.status == "issued":
            p.status = "dispensed"
            updated += 1
    db.commit()
    return RedirectResponse(f"/admin/dispensing?msg=已批量确认发药+{updated}+张", status_code=303)


# ---------------------------------------------------------------------------
# Phase 3 — 销售单 (Sales Orders)
# ---------------------------------------------------------------------------

_SO_STATUS_ZH = {"pending": "待付款", "paid": "已收款", "cancelled": "已取消"}
_SO_ITEM_TYPE_ZH = {"product": "商品", "service": "服务", "medication": "药品", "vaccine": "疫苗"}
_PAYMENT_METHOD_OPTIONS = ["现金", "微信", "支付宝", "银行卡", "挂账"]


def _parse_so_items(form_data) -> list[dict]:
    items = []
    i = 0
    while True:
        name = form_data.get(f"item_name_{i}", "").strip()
        if not name and i > 20:
            break
        if name:
            try:
                unit_price = float(form_data.get(f"unit_price_{i}", 0) or 0)
                quantity = float(form_data.get(f"quantity_{i}", 1) or 1)
            except ValueError:
                unit_price, quantity = 0.0, 1.0
            subtotal = round(unit_price * quantity, 2)
            raw_item_id = form_data.get(f"item_id_{i}", "").strip()
            item_id = int(raw_item_id) if raw_item_id and raw_item_id.isdigit() else None
            items.append({
                "item_id": item_id,
                "item_name": name,
                "item_type": form_data.get(f"item_type_{i}", "product").strip(),
                "unit_price": unit_price,
                "quantity": quantity,
                "subtotal": subtotal,
                "notes": form_data.get(f"item_notes_{i}", "").strip(),
            })
        i += 1
    return items


@app.get("/admin/sales-orders/create", response_class=HTMLResponse)
async def page_admin_so_create(
    request: Request, db: Session = Depends(get_db),
    customer_id: int = Query(0), visit_id: int = Query(0), pet_id: int = Query(0),
    search_q: str = Query(""),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    visit = db.get(Visit, visit_id) if visit_id else None
    if visit:
        customer_id = customer_id or visit.customer_id or 0
        pet_id = pet_id or visit.pet_id or 0
    cust = db.get(Customer, customer_id) if customer_id else None
    pet = db.get(Pet, pet_id) if pet_id else None
    pets = db.query(Pet).filter(Pet.customer_id == customer_id).all() if customer_id else []
    search_results = []
    if search_q and not customer_id:
        search_results = db.query(Customer).filter(
            or_(Customer.name.ilike(f"%{search_q}%"), Customer.phone.ilike(f"%{search_q}%"))
        ).limit(10).all()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return templates.TemplateResponse(request, "admin_sales_order_form.html", {
        "order": None, "visit": visit, "cust": cust, "pet": pet, "pets": pets,
        "so_status_zh": _SO_STATUS_ZH, "item_type_zh": _SO_ITEM_TYPE_ZH,
        "payment_methods": _PAYMENT_METHOD_OPTIONS,
        "today": today, "csrf_token": _get_csrf_token(request), "mode": "create",
        "search_q": search_q, "search_results": search_results,
    })


@app.post("/admin/sales-orders/create")
async def admin_so_create(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    customer_id = int(form.get("customer_id", 0) or 0)
    visit_id = int(form.get("visit_id", 0) or 0)
    pet_id = int(form.get("pet_id", 0) or 0)
    items = _parse_so_items(form)
    total = round(sum(it["subtotal"] for it in items), 2)
    operator = request.session.get("admin_username", "admin")
    order = SalesOrder(
        customer_id=customer_id or None,
        visit_id=visit_id or None,
        pet_id=pet_id or None,
        order_date=str(form.get("order_date", "")).strip()[:20],
        status=str(form.get("status", "pending")).strip(),
        total_amount=total,
        payment_method=str(form.get("payment_method", "")).strip()[:40],
        notes=str(form.get("notes", "")).strip(),
        created_by=operator,
    )
    db.add(order)
    db.flush()
    for it in items:
        db.add(SalesOrderItem(order_id=order.id, **it))
        if it["item_id"] and it["quantity"] > 0:
            _deduct_inventory(db, it["item_id"], it["quantity"],
                              "sales_order", order.id, operator, f"销售单#{order.id}")
    db.commit()
    return RedirectResponse(f"/admin/visits/{visit_id}?msg=销售单已创建" if visit_id else f"/admin/sales-orders/{order.id}?msg=销售单已创建", status_code=303)


@app.get("/admin/sales-orders", response_class=HTMLResponse)
async def page_admin_so_list(
    request: Request, db: Session = Depends(get_db),
    q: str = Query(""), status: str = Query(""),
    date_from: str = Query(""), date_to: str = Query(""),
    page: int = Query(1),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    query = db.query(SalesOrder)
    if q:
        query = query.join(Customer, SalesOrder.customer_id == Customer.id, isouter=True).filter(
            or_(Customer.name.ilike(f"%{q}%"), Customer.phone.ilike(f"%{q}%"))
        )
    if status:
        query = query.filter(SalesOrder.status == status)
    if date_from:
        query = query.filter(SalesOrder.order_date >= date_from)
    if date_to:
        query = query.filter(SalesOrder.order_date <= date_to)
    total = query.count()
    page_size = 30
    orders = query.order_by(SalesOrder.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    cust_map = {}
    for o in orders:
        if o.customer_id and o.customer_id not in cust_map:
            c = db.get(Customer, o.customer_id)
            if c:
                cust_map[o.customer_id] = c
    return templates.TemplateResponse(request, "admin_sales_orders.html", {
        "orders": orders, "cust_map": cust_map,
        "so_status_zh": _SO_STATUS_ZH,
        "total": total, "page": page, "page_size": page_size,
        "filters": {"q": q, "status": status, "date_from": date_from, "date_to": date_to},
    })


@app.get("/admin/sales-orders/{order_id}", response_class=HTMLResponse)
async def page_admin_so_detail(order_id: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    order = db.get(SalesOrder, order_id)
    if not order:
        raise HTTPException(404, "销售单不存在")
    visit = db.get(Visit, order.visit_id) if order.visit_id else None
    cust = db.get(Customer, order.customer_id) if order.customer_id else None
    pet = db.get(Pet, order.pet_id) if order.pet_id else None
    pets = db.query(Pet).filter(Pet.customer_id == order.customer_id).all() if order.customer_id else []
    return templates.TemplateResponse(request, "admin_sales_order_form.html", {
        "order": order, "visit": visit, "cust": cust, "pet": pet, "pets": pets,
        "so_status_zh": _SO_STATUS_ZH, "item_type_zh": _SO_ITEM_TYPE_ZH,
        "payment_methods": _PAYMENT_METHOD_OPTIONS,
        "csrf_token": _get_csrf_token(request), "mode": "edit",
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/sales-orders/{order_id}/edit")
async def admin_so_edit(order_id: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    order = db.get(SalesOrder, order_id)
    if not order:
        raise HTTPException(404)
    operator = request.session.get("admin_username", "admin")
    # 先退回旧明细库存
    for old in order.items:
        if old.item_id and old.quantity > 0:
            _restore_inventory(db, old.item_id, old.quantity,
                               "sales_order", order_id, operator, f"编辑销售单#{order_id}退回")
        db.delete(old)
    db.flush()
    items = _parse_so_items(form)
    total = round(sum(it["subtotal"] for it in items), 2)
    order.order_date = str(form.get("order_date", "")).strip()[:20]
    order.status = str(form.get("status", "pending")).strip()
    order.payment_method = str(form.get("payment_method", "")).strip()[:40]
    order.total_amount = total
    order.notes = str(form.get("notes", "")).strip()
    order.pet_id = int(form.get("pet_id", 0) or 0) or order.pet_id
    for it in items:
        db.add(SalesOrderItem(order_id=order_id, **it))
        if it["item_id"] and it["quantity"] > 0:
            _deduct_inventory(db, it["item_id"], it["quantity"],
                              "sales_order", order_id, operator, f"销售单#{order_id}")
    db.commit()
    return RedirectResponse(f"/admin/sales-orders/{order_id}?msg=已保存", status_code=303)


@app.post("/admin/sales-orders/{order_id}/pay")
async def admin_so_pay(order_id: int, request: Request, db: Session = Depends(get_db),
                        csrf_token: str = Form(""), payment_method: str = Form("")):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    _require_csrf(request, csrf_token)
    order = db.get(SalesOrder, order_id)
    if not order:
        raise HTTPException(404)
    order.status = "paid"
    if payment_method:
        order.payment_method = payment_method.strip()[:40]
    db.commit()
    return RedirectResponse(f"/admin/sales-orders/{order_id}?msg=已标记收款", status_code=303)


@app.post("/admin/sales-orders/{order_id}/delete")
async def admin_so_delete(order_id: int, request: Request, db: Session = Depends(get_db),
                           csrf_token: str = Form("")):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    _require_csrf(request, csrf_token)
    order = db.get(SalesOrder, order_id)
    if not order:
        raise HTTPException(404)
    operator = request.session.get("admin_username", "admin")
    visit_id = order.visit_id
    for it in order.items:
        if it.item_id and it.quantity > 0:
            _restore_inventory(db, it.item_id, it.quantity,
                               "sales_order", order_id, operator, f"删除销售单#{order_id}退回")
    db.delete(order)
    db.commit()
    return RedirectResponse(f"/admin/visits/{visit_id}?msg=销售单已删除" if visit_id else "/admin/sales-orders", status_code=303)


# ─────────────────────────────────────────────
#  库存管理 Inventory
# ─────────────────────────────────────────────

INVENTORY_CATEGORIES = {
    "medication": {"label": "药品", "subs": {
        "controlled": "麻药/精神类",
        "general":    "普通药品",
        "vaccine":    "疫苗",
        "antiparasitic": "驱虫",
    }},
    "consumable": {"label": "耗材", "subs": {
        "general": "普通耗材",
    }},
    "product": {"label": "商品", "subs": {
        "general": "普通商品",
    }},
    "grooming": {"label": "美容", "subs": {
        "washcare": "洗护",
        "styling":  "造型",
        "addon":    "附加服务",
    }},
    "lab": {"label": "化验", "subs": {
        "routine_lab":  "常规化验",
        "external_lab": "院外送检",
    }},
    "imaging": {"label": "影像", "subs": {
        "dr":        "DR",
        "ct":        "CT",
        "mri":       "核磁共振",
        "ultrasound":"B超",
    }},
    "microscopy": {"label": "显微镜", "subs": {
        "optical":   "常规光学显微镜",
        "electron":  "电子显微镜",
    }},
}


@app.get("/api/inventory/search")
async def api_inventory_search(
    q: str = Query(""),
    category: str = Query(""),
    db: Session = Depends(get_db),
):
    """JSON autocomplete for inventory items — used by prescription/sales order forms."""
    query = db.query(InventoryItem).filter(InventoryItem.is_active == True)
    if category:
        query = query.filter(InventoryItem.category == category)
    if q:
        query = query.filter(InventoryItem.name.ilike(f"%{q}%"))
    items = query.order_by(InventoryItem.name).limit(30).all()
    return [
        {
            "id": it.id,
            "name": it.name,
            "category": it.category,
            "unit": it.unit,
            "sell_price": it.sell_price,
            "stock_qty": it.stock_qty,
            "is_service": it.is_service,
            "is_controlled": it.is_controlled,
        }
        for it in items
    ]


@app.get("/admin/inventory", response_class=HTMLResponse)
async def admin_inventory_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    category: str = "",
    low_stock: str = "",
    zero_stock: str = "",
    controlled: str = "",
    service_only: str = "",
    expiry_alert: str = "",
    page: int = 1,
):
    require_admin(request)
    from datetime import date as _date, timedelta as _timedelta
    page_size = 50
    query = db.query(InventoryItem).filter(InventoryItem.is_active == True)
    if q:
        query = query.filter(
            or_(InventoryItem.name.ilike(f"%{q}%"),
                InventoryItem.supplier.ilike(f"%{q}%"))
        )
    if category:
        query = query.filter(InventoryItem.category == category)
    if low_stock == "1":
        query = query.filter(
            InventoryItem.is_service == False,
            InventoryItem.stock_qty <= InventoryItem.low_stock_min,
            InventoryItem.low_stock_min > 0,
        )
    if zero_stock == "1":
        query = query.filter(InventoryItem.is_service == False, InventoryItem.stock_qty <= 0)
    if controlled == "1":
        query = query.filter(InventoryItem.is_controlled == True)
    if service_only == "1":
        query = query.filter(InventoryItem.is_service == True)
    if expiry_alert == "1":
        alert_date = (_date.today() + _timedelta(days=90)).isoformat()
        expiry_ids = (db.query(InventoryBatch.item_id)
                      .filter(InventoryBatch.is_depleted == False,
                              InventoryBatch.expiry_date != "",
                              InventoryBatch.expiry_date <= alert_date)
                      .distinct().subquery())
        query = query.filter(InventoryItem.id.in_(expiry_ids))
    total = query.count()
    items = query.order_by(InventoryItem.category, InventoryItem.name).offset((page - 1) * page_size).limit(page_size).all()
    total_pages = max(1, (total + page_size - 1) // page_size)
    low_count = db.query(InventoryItem).filter(
        InventoryItem.is_active == True,
        InventoryItem.is_service == False,
        InventoryItem.stock_qty <= InventoryItem.low_stock_min,
        InventoryItem.low_stock_min > 0,
    ).count()
    zero_count = db.query(InventoryItem).filter(
        InventoryItem.is_active == True,
        InventoryItem.is_service == False,
        InventoryItem.stock_qty <= 0,
    ).count()
    _alert_date = (_date.today() + _timedelta(days=90)).isoformat()
    expiry_count = (db.query(InventoryBatch.item_id)
                    .filter(InventoryBatch.is_depleted == False,
                            InventoryBatch.expiry_date != "",
                            InventoryBatch.expiry_date <= _alert_date)
                    .distinct().count())
    return templates.TemplateResponse("admin_inventory.html", {
        "request": request, "items": items, "total": total,
        "page": page, "total_pages": total_pages,
        "q": q, "category": category, "low_stock": low_stock,
        "zero_stock": zero_stock, "controlled": controlled, "service_only": service_only,
        "expiry_alert": expiry_alert,
        "categories": INVENTORY_CATEGORIES, "low_count": low_count, "zero_count": zero_count,
        "expiry_count": expiry_count,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": "库存管理",
    })


@app.get("/admin/inventory/create", response_class=HTMLResponse)
async def admin_inventory_create_form(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return templates.TemplateResponse("admin_inventory_form.html", {
        "request": request, "item": None,
        "categories": INVENTORY_CATEGORIES,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": "新增品目",
    })


@app.post("/admin/inventory/create")
async def admin_inventory_create(
    request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    name: str = Form(""), category: str = Form("medication"),
    subcategory: str = Form(""), is_service: str = Form("0"),
    is_controlled: str = Form("0"),
    unit: str = Form("个"), unit2: str = Form(""), unit2_ratio: float = Form(1.0),
    sell_price: float = Form(0.0), cost_price: float = Form(0.0),
    stock_qty: float = Form(0.0), low_stock_min: float = Form(0.0),
    supplier: str = Form(""), notes: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    if not name.strip():
        raise HTTPException(400, "品名不能为空")
    operator = request.session.get("admin", "")
    item = InventoryItem(
        name=name.strip(), category=category, subcategory=subcategory,
        is_service=(is_service == "1"), is_controlled=(is_controlled == "1"),
        unit=unit, unit2=unit2, unit2_ratio=unit2_ratio,
        sell_price=sell_price, cost_price=cost_price,
        stock_qty=stock_qty, low_stock_min=low_stock_min,
        supplier=supplier, notes=notes, created_by=operator,
    )
    db.add(item)
    db.flush()
    # 若初始库存 > 0，记录入库流水
    if stock_qty > 0 and not (is_service == "1"):
        db.add(InventoryTransaction(
            item_id=item.id, tx_type="in", qty=stock_qty,
            qty_before=0, qty_after=stock_qty,
            unit_price=cost_price, ref_type="manual",
            operator=operator, note="初始库存录入",
        ))
    db.commit()
    return RedirectResponse(f"/admin/inventory?msg=已创建：{item.name}", status_code=303)


@app.get("/admin/inventory/{item_id}/edit", response_class=HTMLResponse)
async def admin_inventory_edit_form(item_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(404)
    return templates.TemplateResponse("admin_inventory_form.html", {
        "request": request, "item": item,
        "categories": INVENTORY_CATEGORIES,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": f"编辑品目：{item.name}",
    })


@app.get("/admin/inventory/{item_id}", response_class=HTMLResponse)
async def admin_inventory_detail(item_id: int, request: Request, db: Session = Depends(get_db),
                                  page: int = 1):
    require_admin(request)
    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(404)
    page_size = 30
    txs = (db.query(InventoryTransaction)
           .filter(InventoryTransaction.item_id == item_id)
           .order_by(InventoryTransaction.created_at.desc())
           .offset((page - 1) * page_size).limit(page_size).all())
    tx_total = db.query(InventoryTransaction).filter(InventoryTransaction.item_id == item_id).count()
    batches = (db.query(InventoryBatch)
               .filter(InventoryBatch.item_id == item_id)
               .order_by(InventoryBatch.expiry_date)
               .all())
    from datetime import date as _date, timedelta as _timedelta
    today_str = _date.today().isoformat()
    alert_date_str = (_date.today() + _timedelta(days=90)).isoformat()
    return templates.TemplateResponse("admin_inventory_detail.html", {
        "request": request, "item": item, "txs": txs,
        "tx_total": tx_total, "page": page,
        "tx_pages": max(1, (tx_total + page_size - 1) // page_size),
        "batches": batches, "today_str": today_str, "alert_date_str": alert_date_str,
        "categories": INVENTORY_CATEGORIES,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": f"品目详情：{item.name}",
    })


@app.post("/admin/inventory/{item_id}/edit")
async def admin_inventory_edit(
    item_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    name: str = Form(""), category: str = Form("medication"),
    subcategory: str = Form(""), is_service: str = Form("0"),
    is_controlled: str = Form("0"),
    unit: str = Form("个"), unit2: str = Form(""), unit2_ratio: float = Form(1.0),
    sell_price: float = Form(0.0), cost_price: float = Form(0.0),
    low_stock_min: float = Form(0.0),
    supplier: str = Form(""), notes: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(404)
    item.name = name.strip() or item.name
    item.category = category; item.subcategory = subcategory
    item.is_service = (is_service == "1"); item.is_controlled = (is_controlled == "1")
    item.unit = unit; item.unit2 = unit2; item.unit2_ratio = unit2_ratio
    item.sell_price = sell_price; item.cost_price = cost_price
    item.low_stock_min = low_stock_min
    item.supplier = supplier; item.notes = notes
    item.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/inventory/{item_id}?msg=已保存", status_code=303)


@app.post("/admin/inventory/{item_id}/stock-in")
async def admin_inventory_stock_in(
    item_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    qty: float = Form(...), unit_price: float = Form(0.0),
    batch_no: str = Form(""), expiry_date: str = Form(""),
    note: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    item = db.get(InventoryItem, item_id)
    if not item or item.is_service:
        raise HTTPException(404)
    if qty <= 0:
        raise HTTPException(400, "入库数量必须大于 0")
    from datetime import date as _date
    before = item.stock_qty
    item.stock_qty += qty
    item.updated_at = datetime.utcnow()
    tx_note = note or ("手动入库" + (f"（批次：{batch_no}）" if batch_no else ""))
    db.add(InventoryTransaction(
        item_id=item_id, tx_type="in", qty=qty,
        qty_before=before, qty_after=item.stock_qty,
        unit_price=unit_price or item.cost_price,
        ref_type="manual", operator=request.session.get("admin", ""),
        note=tx_note,
    ))
    if expiry_date:
        db.add(InventoryBatch(
            item_id=item_id,
            batch_no=batch_no,
            quantity=qty,
            expiry_date=expiry_date,
            received_date=_date.today().isoformat(),
            notes=note,
        ))
    db.commit()
    return RedirectResponse(f"/admin/inventory/{item_id}?msg=入库成功+{qty}{item.unit}", status_code=303)


@app.post("/admin/inventory/{item_id}/adjust")
async def admin_inventory_adjust(
    item_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    new_qty: float = Form(...), note: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    item = db.get(InventoryItem, item_id)
    if not item or item.is_service:
        raise HTTPException(404)
    before = item.stock_qty
    diff = new_qty - before
    item.stock_qty = new_qty
    item.updated_at = datetime.utcnow()
    db.add(InventoryTransaction(
        item_id=item_id, tx_type="adjust", qty=abs(diff),
        qty_before=before, qty_after=new_qty,
        unit_price=0, ref_type="manual",
        operator=request.session.get("admin", ""),
        note=note or f"盘点调整（{'+' if diff >= 0 else ''}{diff:.1f}）",
    ))
    db.commit()
    return RedirectResponse(f"/admin/inventory/{item_id}?msg=库存已调整", status_code=303)


@app.post("/admin/inventory/{item_id}/batch/{batch_id}/adjust")
async def admin_batch_adjust(
    item_id: int, batch_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    new_qty: float = Form(...),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    batch = (db.query(InventoryBatch)
             .filter(InventoryBatch.id == batch_id, InventoryBatch.item_id == item_id)
             .first())
    if not batch:
        raise HTTPException(404)
    item = db.get(InventoryItem, item_id)
    diff = new_qty - batch.quantity
    before = item.stock_qty
    batch.quantity = new_qty
    if new_qty <= 0:
        batch.is_depleted = True
    item.stock_qty = max(0.0, item.stock_qty + diff)
    item.updated_at = datetime.utcnow()
    db.add(InventoryTransaction(
        item_id=item_id, tx_type="adjust", qty=abs(diff),
        qty_before=before, qty_after=item.stock_qty,
        unit_price=0, ref_type="batch",
        operator=request.session.get("admin", ""),
        note=f"批次{batch.batch_no or batch_id}数量修正（{'+' if diff >= 0 else ''}{diff:.1f}）",
    ))
    db.commit()
    return RedirectResponse(f"/admin/inventory/{item_id}?msg=批次已更新", status_code=303)


@app.post("/admin/inventory/{item_id}/batch/{batch_id}/deplete")
async def admin_batch_deplete(
    item_id: int, batch_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    batch = (db.query(InventoryBatch)
             .filter(InventoryBatch.id == batch_id, InventoryBatch.item_id == item_id)
             .first())
    if not batch:
        raise HTTPException(404)
    item = db.get(InventoryItem, item_id)
    before = item.stock_qty
    remaining = batch.quantity
    batch.quantity = 0
    batch.is_depleted = True
    if remaining > 0:
        item.stock_qty = max(0.0, item.stock_qty - remaining)
        item.updated_at = datetime.utcnow()
        db.add(InventoryTransaction(
            item_id=item_id, tx_type="adjust", qty=remaining,
            qty_before=before, qty_after=item.stock_qty,
            unit_price=0, ref_type="batch",
            operator=request.session.get("admin", ""),
            note=f"批次{batch.batch_no or batch_id}标记耗尽",
        ))
    db.commit()
    return RedirectResponse(f"/admin/inventory/{item_id}?msg=批次已标记耗尽", status_code=303)


# ──────────────────────────────────────────────────────────────
# 循环盘点
# ──────────────────────────────────────────────────────────────

@app.get("/admin/stocktake", response_class=HTMLResponse)
async def admin_stocktake_list(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    from datetime import date as _date, timedelta as _timedelta
    sessions = (db.query(StocktakeSession)
                .order_by(StocktakeSession.created_at.desc())
                .limit(30).all())
    # 统计每个大类最近盘点时间
    cycle_stats = []
    today = _date.today()
    for cat_key, cat_info in INVENTORY_CATEGORIES.items():
        item_cnt = db.query(InventoryItem).filter(
            InventoryItem.is_active == True,
            InventoryItem.is_service == False,
            InventoryItem.category == cat_key,
        ).count()
        if item_cnt == 0:
            continue
        last_row = (db.query(InventoryItem.last_counted_at)
                    .filter(InventoryItem.is_active == True,
                            InventoryItem.is_service == False,
                            InventoryItem.category == cat_key,
                            InventoryItem.last_counted_at.isnot(None))
                    .order_by(InventoryItem.last_counted_at.desc())
                    .first())
        last_dt = last_row[0].date() if last_row and last_row[0] else None
        days_ago = (today - last_dt).days if last_dt else None
        cycle_stats.append({
            "key": cat_key,
            "label": cat_info["label"],
            "item_count": item_cnt,
            "last_counted": last_dt,
            "days_ago": days_ago,
        })
    open_sessions = [s for s in sessions if s.status == "open"]
    return templates.TemplateResponse("admin_stocktake.html", {
        "request": request, "sessions": sessions,
        "open_sessions": open_sessions,
        "cycle_stats": cycle_stats,
        "categories": INVENTORY_CATEGORIES,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": "循环盘点",
    })


@app.post("/admin/stocktake/create")
async def admin_stocktake_create(
    request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    category_filter: str = Form(""),
    name: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    operator = request.session.get("admin", "")
    query = db.query(InventoryItem).filter(
        InventoryItem.is_active == True,
        InventoryItem.is_service == False,
    )
    if category_filter:
        query = query.filter(InventoryItem.category == category_filter)
    items = query.order_by(InventoryItem.category, InventoryItem.name).all()
    if not items:
        return RedirectResponse("/admin/stocktake?err=该类别暂无品目", status_code=303)
    cat_label = INVENTORY_CATEGORIES.get(category_filter, {}).get("label", "全部") if category_filter else "全部"
    from datetime import date as _date
    session_name = name.strip() or f"{_date.today()} {cat_label}盘点"
    sess = StocktakeSession(
        name=session_name,
        category_filter=category_filter,
        operator=operator,
        item_count=len(items),
    )
    db.add(sess)
    db.flush()
    for it in items:
        db.add(StocktakeItem(
            session_id=sess.id,
            item_id=it.id,
            item_name=it.name,
            category=it.category,
            unit=it.unit,
            system_qty=it.stock_qty,
        ))
    db.commit()
    return RedirectResponse(f"/admin/stocktake/{sess.id}", status_code=303)


@app.get("/admin/stocktake/{session_id}", response_class=HTMLResponse)
async def admin_stocktake_session(
    session_id: int, request: Request, db: Session = Depends(get_db),
    q: str = "",
):
    require_admin(request)
    sess = db.get(StocktakeSession, session_id)
    if not sess:
        raise HTTPException(404)
    items_q = db.query(StocktakeItem).filter(StocktakeItem.session_id == session_id)
    if q:
        items_q = items_q.filter(StocktakeItem.item_name.ilike(f"%{q}%"))
    sit_items = items_q.order_by(StocktakeItem.category, StocktakeItem.item_name).all()
    counted = sum(1 for x in sit_items if x.actual_qty is not None)
    return templates.TemplateResponse("admin_stocktake_session.html", {
        "request": request, "sess": sess, "sit_items": sit_items,
        "q": q, "counted": counted,
        "categories": INVENTORY_CATEGORIES,
        "csrf_token": request.session.get("csrf_token", ""),
        "title": f"盘点：{sess.name}",
    })


@app.post("/admin/stocktake/{session_id}/save")
async def admin_stocktake_save(
    session_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    sess = db.get(StocktakeSession, session_id)
    if not sess or sess.status != "open":
        raise HTTPException(400, "盘点已完成或不存在")
    form = await request.form()
    sit_map = {si.id: si for si in db.query(StocktakeItem).filter(StocktakeItem.session_id == session_id).all()}
    for key, val in form.items():
        if key.startswith("actual_"):
            try:
                si_id = int(key.split("_", 1)[1])
                si = sit_map.get(si_id)
                if si and val.strip() != "":
                    si.actual_qty = float(val)
                    si.variance = si.actual_qty - si.system_qty
                elif si and val.strip() == "":
                    si.actual_qty = None
                    si.variance = 0.0
            except (ValueError, IndexError):
                pass
        elif key.startswith("notes_"):
            try:
                si_id = int(key.split("_", 1)[1])
                si = sit_map.get(si_id)
                if si:
                    si.notes = val
            except (ValueError, IndexError):
                pass
    db.commit()
    return RedirectResponse(f"/admin/stocktake/{session_id}?msg=已暂存", status_code=303)


@app.post("/admin/stocktake/{session_id}/submit")
async def admin_stocktake_submit(
    session_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    sess = db.get(StocktakeSession, session_id)
    if not sess or sess.status != "open":
        raise HTTPException(400, "盘点已完成或不存在")
    form = await request.form()
    sit_map = {si.id: si for si in db.query(StocktakeItem).filter(StocktakeItem.session_id == session_id).all()}
    # 先把表单值写入
    for key, val in form.items():
        if key.startswith("actual_"):
            try:
                si_id = int(key.split("_", 1)[1])
                si = sit_map.get(si_id)
                if si and val.strip() != "":
                    si.actual_qty = float(val)
                    si.variance = si.actual_qty - si.system_qty
                elif si and val.strip() == "":
                    si.actual_qty = None
                    si.variance = 0.0
            except (ValueError, IndexError):
                pass
        elif key.startswith("notes_"):
            try:
                si_id = int(key.split("_", 1)[1])
                si = sit_map.get(si_id)
                if si:
                    si.notes = val
            except (ValueError, IndexError):
                pass
    operator = request.session.get("admin", "")
    now = datetime.utcnow()
    variance_count = 0
    for si in sit_map.values():
        if si.actual_qty is None:
            continue
        inv_item = db.get(InventoryItem, si.item_id) if si.item_id else None
        if not inv_item:
            continue
        # 以实盘数量直接覆盖当前系统库存（基准是提交时的当前值，不是建单快照）
        # 这样盘点期间正常发生的出入库不会被重复计算
        before = inv_item.stock_qty
        after = si.actual_qty
        diff = after - before          # 与"当前"系统值的差，而非与快照的差
        si.variance = diff             # 更新为真实差异
        if abs(diff) > 0.001:
            variance_count += 1
            inv_item.stock_qty = after
            inv_item.updated_at = now
            db.add(InventoryTransaction(
                item_id=inv_item.id, tx_type="adjust", qty=abs(diff),
                qty_before=before, qty_after=after,
                unit_price=0, ref_type="stocktake", ref_id=session_id,
                operator=operator,
                note=f"循环盘点#{session_id}（{'+' if diff >= 0 else ''}{diff:.1f}）",
            ))
            si.is_adjusted = True
        inv_item.last_counted_at = now
    sess.variance_count = variance_count
    sess.status = "completed"
    sess.completed_at = now
    db.commit()
    return RedirectResponse(f"/admin/stocktake/{session_id}?msg=盘点已提交", status_code=303)


@app.post("/admin/inventory/{item_id}/deactivate")
async def admin_inventory_deactivate(
    item_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(404)
    item.is_active = False
    db.commit()
    return RedirectResponse("/admin/inventory?msg=已下架", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
#  狂犬疫苗免疫登记  Rabies Vaccine Registration
# ─────────────────────────────────────────────────────────────────────────────

_INVALID_NAMES = {"先生", "女士", "mr", "mrs", "ms", "主人", "不详"}
_SIG_DIR = Path("data/signatures")
_SIG_DIR.mkdir(parents=True, exist_ok=True)

_RABIES_STATUS_ZH = {
    "owner_pending": "待医护填写",
    "staff_pending": "待完成签字",
    "completed": "已完成",
}


def _is_invalid_name(name: str) -> bool:
    n = name.strip().lower().replace(" ", "").replace(".", "")
    return n in _INVALID_NAMES or n in {"先生", "女士"}


def _save_signature(data_url: str, prefix: str) -> str:
    """将 base64 data URL 保存为 PNG 文件，返回相对路径。"""
    import base64
    if not data_url or not data_url.startswith("data:image/"):
        return ""
    try:
        header, b64 = data_url.split(",", 1)
        img_bytes = base64.b64decode(b64)
        fname = f"{prefix}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.png"
        fpath = _SIG_DIR / fname
        fpath.write_bytes(img_bytes)
        return str(fpath)
    except Exception:
        return ""


# ── 公开 API：手机号查询客户 ──────────────────────────────────────────────────

@app.get("/api/customer/lookup")
async def api_customer_lookup(phone: str = Query(""), db: Session = Depends(get_db)):
    if not phone or len(phone) < 6:
        return {"found": False}
    cust = db.query(Customer).filter(Customer.phone == phone.strip()).first()
    if not cust:
        return {"found": False}
    pets = db.query(Pet).filter(Pet.customer_id == cust.id).all()
    invalid_name = _is_invalid_name(cust.name)
    return {
        "found": True,
        "customer_id": cust.id,
        "name": "" if invalid_name else cust.name,
        "name_invalid": invalid_name,
        "address": cust.address,
        "pets": [
            {
                "id": p.id,
                "name": p.name,
                "breed": p.breed,
                "gender": p.gender,
                "birthday_estimate": p.birthday_estimate,
                "color_pattern": p.color_pattern,
                "species": p.species,
            }
            for p in pets
        ],
    }


# ── 公开表单：主人填写 ────────────────────────────────────────────────────────

@app.get("/rabies", response_class=HTMLResponse)
async def page_rabies_form(request: Request):
    return templates.TemplateResponse(request, "rabies_form.html", {
        "msg": request.query_params.get("msg"),
    })


@app.post("/rabies")
async def submit_rabies_form(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    owner_name    = str(form.get("owner_name", "")).strip()
    owner_phone   = str(form.get("owner_phone", "")).strip()
    owner_address = str(form.get("owner_address", "")).strip()
    animal_name   = str(form.get("animal_name", "")).strip()
    animal_breed  = str(form.get("animal_breed", "")).strip()
    animal_dob    = str(form.get("animal_dob", "")).strip()
    animal_gender = str(form.get("animal_gender", "")).strip()
    animal_color  = str(form.get("animal_color", "")).strip()
    owner_sig_data = str(form.get("owner_signature", "")).strip()
    customer_id_raw = str(form.get("customer_id", "")).strip()
    pet_id_raw      = str(form.get("pet_id", "")).strip()

    # 校验姓名
    if _is_invalid_name(owner_name) or not owner_name:
        return RedirectResponse("/rabies?msg=请填写真实姓名（不可填写先生/女士）", status_code=303)
    if not owner_phone:
        return RedirectResponse("/rabies?msg=请填写手机号", status_code=303)
    if not owner_sig_data or len(owner_sig_data) < 100:
        return RedirectResponse("/rabies?msg=请完成签名", status_code=303)

    # 保存签名
    sig_path = _save_signature(owner_sig_data, f"owner_{owner_phone}")

    # 查找或创建客户
    customer_id = int(customer_id_raw) if customer_id_raw.isdigit() else None
    if not customer_id:
        cust = db.query(Customer).filter(Customer.phone == owner_phone).first()
        if cust:
            customer_id = cust.id
            # 如果档案名字是无效的，更新为真实姓名
            if _is_invalid_name(cust.name):
                cust.name = owner_name
            if owner_address and not cust.address:
                cust.address = owner_address
        else:
            cust = Customer(name=owner_name, phone=owner_phone, address=owner_address, source="rabies")
            db.add(cust)
            db.flush()
            customer_id = cust.id
    else:
        cust = db.get(Customer, customer_id)
        if cust and _is_invalid_name(cust.name):
            cust.name = owner_name

    # 查找或创建宠物
    pet_id = int(pet_id_raw) if pet_id_raw.isdigit() else None
    if not pet_id and animal_name:
        pet = Pet(
            customer_id=customer_id,
            name=animal_name,
            breed=animal_breed,
            gender=animal_gender,
            birthday_estimate=animal_dob,
            color_pattern=animal_color,
            species="dog",
        )
        db.add(pet)
        db.flush()
        pet_id = pet.id
    elif pet_id:
        pet = db.get(Pet, pet_id)
        if pet:
            if animal_color:
                pet.color_pattern = animal_color
            if animal_dob:
                pet.birthday_estimate = animal_dob

    record = RabiesVaccineRecord(
        customer_id=customer_id,
        pet_id=pet_id,
        owner_name=owner_name,
        owner_address=owner_address,
        owner_phone=owner_phone,
        animal_name=animal_name,
        animal_breed=animal_breed,
        animal_dob=animal_dob,
        animal_gender=animal_gender,
        animal_color=animal_color,
        owner_signature_path=sig_path,
        owner_signed_at=datetime.utcnow(),
        status="staff_pending",
        clinic_store=str(form.get("clinic_store", "横岗店")).strip() or "横岗店",
    )
    db.add(record)
    db.commit()
    return RedirectResponse(f"/rabies/done?id={record.id}", status_code=303)


@app.get("/rabies/done", response_class=HTMLResponse)
async def page_rabies_done(request: Request, id: int = Query(0), db: Session = Depends(get_db)):
    rec = db.get(RabiesVaccineRecord, id) if id else None
    return templates.TemplateResponse(request, "rabies_done.html", {"rec": rec})


@app.post("/api/rabies/submit")
async def api_rabies_submit(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    owner_name    = str(body.get("owner_name", "")).strip()
    owner_phone   = str(body.get("owner_phone", "")).strip()
    owner_address = str(body.get("owner_address", "")).strip()
    animal_name   = str(body.get("animal_name", "")).strip()
    animal_breed  = str(body.get("animal_breed", "")).strip()
    animal_dob    = str(body.get("animal_dob", "")).strip()
    animal_gender = str(body.get("animal_gender", "")).strip()
    animal_color  = str(body.get("animal_color", "")).strip()
    owner_sig_data  = str(body.get("owner_signature", "")).strip()
    customer_id_raw = body.get("customer_id")
    pet_id_raw      = body.get("pet_id")

    if _is_invalid_name(owner_name) or not owner_name:
        raise HTTPException(400, detail="请填写真实姓名（不可填写先生/女士）")
    if not owner_phone:
        raise HTTPException(400, detail="请填写手机号")
    if not owner_address:
        raise HTTPException(400, detail="请填写联系地址")
    if not animal_name:
        raise HTTPException(400, detail="请填写动物名称")
    if not animal_breed:
        raise HTTPException(400, detail="请填写动物品种")
    if not animal_dob:
        raise HTTPException(400, detail="请选择动物出生年月")
    if not animal_color:
        raise HTTPException(400, detail="请填写动物毛色")
    if not owner_sig_data or len(owner_sig_data) < 100:
        raise HTTPException(400, detail="请完成签名")

    sig_path = _save_signature(owner_sig_data, f"owner_{owner_phone}")

    customer_id = int(customer_id_raw) if isinstance(customer_id_raw, int) or (isinstance(customer_id_raw, str) and customer_id_raw.isdigit()) else None
    if not customer_id:
        cust = db.query(Customer).filter(Customer.phone == owner_phone).first()
        if cust:
            customer_id = cust.id
            if _is_invalid_name(cust.name):
                cust.name = owner_name
            if owner_address and not cust.address:
                cust.address = owner_address
        else:
            cust = Customer(name=owner_name, phone=owner_phone, address=owner_address, source="rabies")
            db.add(cust)
            db.flush()
            customer_id = cust.id
    else:
        cust = db.get(Customer, customer_id)
        if cust and _is_invalid_name(cust.name):
            cust.name = owner_name

    pet_id = int(pet_id_raw) if isinstance(pet_id_raw, int) or (isinstance(pet_id_raw, str) and str(pet_id_raw).isdigit()) else None
    if not pet_id and animal_name:
        pet = Pet(
            customer_id=customer_id,
            name=animal_name,
            breed=animal_breed,
            gender=animal_gender,
            birthday_estimate=animal_dob,
            color_pattern=animal_color,
            species="dog",
        )
        db.add(pet)
        db.flush()
        pet_id = pet.id
    elif pet_id:
        pet = db.get(Pet, pet_id)
        if pet:
            if animal_color:
                pet.color_pattern = animal_color
            if animal_dob:
                pet.birthday_estimate = animal_dob

    record = RabiesVaccineRecord(
        customer_id=customer_id,
        pet_id=pet_id,
        owner_name=owner_name,
        owner_address=owner_address,
        owner_phone=owner_phone,
        animal_name=animal_name,
        animal_breed=animal_breed,
        animal_dob=animal_dob,
        animal_gender=animal_gender,
        animal_color=animal_color,
        owner_signature_path=sig_path,
        owner_signed_at=datetime.utcnow(),
        status="staff_pending",
        clinic_store=str(body.get("clinic_store", "横岗店")).strip() or "横岗店",
    )
    db.add(record)
    db.commit()
    return {"id": record.id, "status": record.status}


# ── 后台：收费单 ─────────────────────────────────────────────────────────────

_INV_STATUS_ZH = {"unpaid": "待收款", "paid": "已收款", "cancelled": "已取消"}
_INV_PAY_ZH    = {
    "cash": "现金", "wechat": "微信支付", "alipay": "支付宝",
    "card": "刷卡", "groupbuy": "团购", "prepaid": "预付款",
}


def _gen_invoice_no(db: Session) -> str:
    """生成收费单号：YYYYMMDD-N（当天第几张）"""
    from datetime import date
    today_str = date.today().strftime("%Y%m%d")
    count = db.query(func.count(Invoice.id)).filter(
        Invoice.invoice_no.like(f"{today_str}-%")
    ).scalar() or 0
    return f"{today_str}-{count + 1}"


@app.get("/admin/invoices", response_class=HTMLResponse)
async def admin_invoices_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
):
    require_admin(request)
    query = db.query(Invoice).order_by(Invoice.id.desc())
    if status:
        query = query.filter(Invoice.payment_status == status)
    if q:
        from sqlalchemy import or_
        cids = [c.id for c in db.query(Customer.id).filter(
            or_(Customer.name.ilike(f"%{q}%"), Customer.phone.ilike(f"%{q}%"))
        ).all()]
        query = query.filter(Invoice.customer_id.in_(cids))
    invoices = query.limit(200).all()
    cust_map = {}
    for inv in invoices:
        if inv.customer_id and inv.customer_id not in cust_map:
            cust_map[inv.customer_id] = db.get(Customer, inv.customer_id)
    return templates.TemplateResponse(request, "admin_invoices.html", {
        "invoices": invoices,
        "cust_map": cust_map,
        "inv_status_zh": _INV_STATUS_ZH,
        "inv_pay_zh": _INV_PAY_ZH,
        "q": q,
        "status": status,
        "csrf_token": _get_csrf_token(request),
    })


@app.get("/admin/invoices/create", response_class=HTMLResponse)
async def admin_invoice_create_page(
    request: Request,
    db: Session = Depends(get_db),
    visit_id: int = 0,
    customer_id: int = 0,
):
    require_admin(request)
    from datetime import date
    visit, cust, pet = None, None, None
    prefill_items = []

    if visit_id:
        visit = db.get(Visit, visit_id)
        if visit:
            if visit.customer_id:
                cust = db.get(Customer, visit.customer_id)
            if visit.pet_id:
                pet = db.get(Pet, visit.pet_id)
            # 预填：处方单
            prescs = db.query(Prescription).filter(
                Prescription.visit_id == visit_id,
                Prescription.status != "draft",
            ).all()
            for p in prescs:
                for item in p.items:
                    prefill_items.append({
                        "ref_type": "prescription",
                        "ref_id": p.id,
                        "description": f"[处方#{p.id}] {item.drug_name}",
                        "quantity": item.quantity_num,
                        "unit_price": item.unit_price,
                        "subtotal": item.subtotal,
                    })
            # 预填：销售单
            sos = db.query(SalesOrder).filter(
                SalesOrder.visit_id == visit_id,
                SalesOrder.status != "cancelled",
            ).all()
            for so in sos:
                for item in so.items:
                    prefill_items.append({
                        "ref_type": "sales_order",
                        "ref_id": so.id,
                        "description": f"[销售单#{so.id}] {item.item_name}",
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "subtotal": item.subtotal,
                    })
    elif customer_id:
        cust = db.get(Customer, customer_id)

    return templates.TemplateResponse(request, "admin_invoice_detail.html", {
        "mode": "create",
        "visit": visit,
        "cust": cust,
        "pet": pet,
        "prefill_items": prefill_items,
        "today": date.today().isoformat(),
        "inv_status_zh": _INV_STATUS_ZH,
        "inv_pay_zh": _INV_PAY_ZH,
        "csrf_token": _get_csrf_token(request),
        "msg": None,
    })


@app.post("/admin/invoices/create")
async def admin_invoice_create(
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    _check_csrf(request, await request.form())
    form = await request.form()
    from datetime import date

    visit_id    = int(form.get("visit_id") or 0) or None
    customer_id = int(form.get("customer_id") or 0) or None
    pet_id      = int(form.get("pet_id") or 0) or None
    invoice_date = str(form.get("invoice_date") or date.today().isoformat())
    discount     = float(form.get("discount_amount") or 0)
    notes        = str(form.get("notes") or "")
    admin_name   = request.session.get("admin", "")

    # 明细行
    descs      = form.getlist("desc[]")
    qtys       = form.getlist("qty[]")
    prices     = form.getlist("price[]")
    ref_types  = form.getlist("ref_type[]")
    ref_ids    = form.getlist("ref_id[]")

    subtotal = 0.0
    line_items = []
    for i, desc in enumerate(descs):
        desc = desc.strip()
        if not desc:
            continue
        qty   = float(qtys[i]) if i < len(qtys) else 1.0
        price = float(prices[i]) if i < len(prices) else 0.0
        sub   = round(qty * price, 2)
        subtotal += sub
        line_items.append(InvoiceItem(
            ref_type    = ref_types[i] if i < len(ref_types) else "manual",
            ref_id      = int(ref_ids[i]) if i < len(ref_ids) and ref_ids[i] else None,
            description = desc,
            quantity    = qty,
            unit_price  = price,
            subtotal    = sub,
        ))

    total = round(subtotal - discount, 2)
    inv = Invoice(
        invoice_no      = _gen_invoice_no(db),
        customer_id     = customer_id,
        visit_id        = visit_id,
        pet_id          = pet_id,
        invoice_date    = invoice_date,
        subtotal        = round(subtotal, 2),
        discount_amount = discount,
        total_amount    = total,
        payment_status  = "unpaid",
        notes           = notes,
        created_by      = admin_name,
    )
    db.add(inv)
    db.flush()
    for li in line_items:
        li.invoice_id = inv.id
        db.add(li)
    db.commit()
    return RedirectResponse(f"/admin/invoices/{inv.id}?msg=收费单已创建", status_code=303)


@app.get("/admin/invoices/{inv_id}", response_class=HTMLResponse)
async def admin_invoice_detail(
    inv_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    inv = db.get(Invoice, inv_id)
    if not inv:
        raise HTTPException(404, "收费单不存在")
    cust  = db.get(Customer, inv.customer_id) if inv.customer_id else None
    pet   = db.get(Pet,      inv.pet_id)      if inv.pet_id      else None
    visit = db.get(Visit,    inv.visit_id)    if inv.visit_id    else None
    return templates.TemplateResponse(request, "admin_invoice_detail.html", {
        "mode": "view",
        "inv": inv,
        "cust": cust,
        "pet": pet,
        "visit": visit,
        "inv_status_zh": _INV_STATUS_ZH,
        "inv_pay_zh": _INV_PAY_ZH,
        "csrf_token": _get_csrf_token(request),
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/invoices/{inv_id}/pay")
async def admin_invoice_pay(
    inv_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    _check_csrf(request, await request.form())
    form = await request.form()
    inv = db.get(Invoice, inv_id)
    if not inv:
        raise HTTPException(404)
    inv.payment_method = str(form.get("payment_method") or "cash")
    inv.payment_status = "paid"
    inv.paid_at        = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/invoices/{inv_id}?msg=收款成功", status_code=303)


@app.post("/admin/invoices/{inv_id}/cancel")
async def admin_invoice_cancel(
    inv_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    _check_csrf(request, await request.form())
    inv = db.get(Invoice, inv_id)
    if inv and inv.payment_status == "unpaid":
        inv.payment_status = "cancelled"
        db.commit()
    return RedirectResponse(f"/admin/invoices/{inv_id}?msg=收费单已取消", status_code=303)


@app.get("/admin/invoices/{inv_id}/print", response_class=HTMLResponse)
async def admin_invoice_print(
    inv_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    inv = db.get(Invoice, inv_id)
    if not inv:
        raise HTTPException(404)
    cust  = db.get(Customer, inv.customer_id) if inv.customer_id else None
    pet   = db.get(Pet,      inv.pet_id)      if inv.pet_id      else None
    visit = db.get(Visit,    inv.visit_id)    if inv.visit_id    else None
    from app.database import get_settings
    clinic_name = "大风动物医院"
    try:
        settings_obj = get_settings()
        clinic_name = getattr(settings_obj, "clinic_name", clinic_name)
    except Exception:
        pass
    return templates.TemplateResponse(request, "admin_invoice_print.html", {
        "inv": inv,
        "cust": cust,
        "pet": pet,
        "visit": visit,
        "inv_status_zh": _INV_STATUS_ZH,
        "inv_pay_zh": _INV_PAY_ZH,
        "clinic_name": clinic_name,
    })


@app.post("/admin/invoices/{inv_id}/delete")
async def admin_invoice_delete(
    inv_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    _check_csrf(request, await request.form())
    inv = db.get(Invoice, inv_id)
    if inv:
        db.delete(inv)
        db.commit()
    return RedirectResponse("/admin/invoices?msg=已删除", status_code=303)


# ── 后台：疫苗档案管理 ───────────────────────────────────────────────────────

_VACC_TYPE_ZH = {
    "rabies":    "狂犬疫苗",
    "combo_3":   "猫三联",
    "combo_6":   "猫六联",
    "canine_8":  "犬八联",
    "deworming": "驱虫",
    "other":     "其他",
}
_DOSE_ZH = {1: "第1针", 2: "第2针", 3: "第3针", 99: "加强针"}


@app.get("/admin/vaccinations", response_class=HTMLResponse)
async def admin_vaccinations_list(
    request: Request, db: Session = Depends(get_db),
    q: str = Query(""), filter: str = Query("all"),
    vaccine_type: str = Query(""),
):
    require_admin(request)
    from datetime import date, timedelta
    from sqlalchemy import case as sa_case
    today = date.today().isoformat()
    soon  = (date.today() + timedelta(days=7)).isoformat()

    # 按到期日升序，NULL/空值排末尾
    due_order = sa_case(
        (Vaccination.next_due_date.is_(None), "9999-99-99"),
        (Vaccination.next_due_date == "",      "9999-99-99"),
        else_=Vaccination.next_due_date,
    )
    query = db.query(Vaccination).order_by(due_order.asc(), Vaccination.id.desc())

    if q:
        pet_ids  = [p.id for p in db.query(Pet.id).filter(Pet.name.ilike(f"%{q}%")).all()]
        cust_ids = [c.id for c in db.query(Customer.id).filter(
            or_(Customer.name.ilike(f"%{q}%"), Customer.phone.ilike(f"%{q}%"))
        ).all()]
        query = query.filter(or_(
            Vaccination.pet_id.in_(pet_ids),
            Vaccination.customer_id.in_(cust_ids),
        ))
    if vaccine_type:
        query = query.filter(Vaccination.vaccine_type == vaccine_type)
    if filter == "soon":
        query = query.filter(
            Vaccination.next_due_date != "",
            Vaccination.next_due_date <= soon,
            Vaccination.next_due_date >= today,
        )
    elif filter == "overdue":
        query = query.filter(
            Vaccination.next_due_date != "",
            Vaccination.next_due_date < today,
        )

    records = query.limit(300).all()
    return templates.TemplateResponse(request, "admin_vaccinations.html", {
        "records": records, "q": q, "filter": filter,
        "vaccine_type": vaccine_type,
        "vacc_type_zh": _VACC_TYPE_ZH, "dose_zh": _DOSE_ZH,
        "today": today, "soon": soon,
        "title": "疫苗管理",
        "msg": request.query_params.get("msg"),
        "csrf_token": _get_csrf_token(request),
    })


@app.get("/admin/vaccinations/create", response_class=HTMLResponse)
async def admin_vaccination_create_page(
    request: Request, db: Session = Depends(get_db),
    pet_id: int = 0, customer_id: int = 0,
):
    require_admin(request)
    from datetime import date
    pet  = db.get(Pet, pet_id)  if pet_id  else None
    cust = db.get(Customer, customer_id) if customer_id else (
        db.get(Customer, pet.customer_id) if pet and pet.customer_id else None
    )
    vets = [v[0] for v in db.query(Staff.name).filter(
        Staff.status.in_(["active", "probation"]), Staff.position.ilike("%医%")
    ).all()]
    vacc_items = db.query(InventoryItem).filter(
        InventoryItem.category.in_(["vaccine", "antiparasitic"]),
        InventoryItem.stock_qty > 0,
    ).order_by(InventoryItem.name).all()
    return templates.TemplateResponse(request, "admin_vaccination_form.html", {
        "mode": "create", "vacc": None,
        "pet": pet, "cust": cust,
        "vets": vets, "vacc_items": vacc_items,
        "vacc_type_zh": _VACC_TYPE_ZH, "dose_zh": _DOSE_ZH,
        "today": date.today().isoformat(),
        "csrf_token": _get_csrf_token(request),
        "msg": None,
    })


@app.post("/admin/vaccinations/create")
async def admin_vaccination_create(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    from datetime import date

    pet_id      = int(form.get("pet_id") or 0) or None
    customer_id = int(form.get("customer_id") or 0) or None
    item_id     = int(form.get("inventory_item_id") or 0) or None
    is_free     = form.get("is_free") == "1"
    admin_name  = request.session.get("admin", "")

    vacc = Vaccination(
        pet_id            = pet_id,
        customer_id       = customer_id,
        vaccine_type      = str(form.get("vaccine_type") or "other"),
        vaccine_name      = str(form.get("vaccine_name") or "").strip()[:120],
        batch_no          = str(form.get("batch_no") or "").strip()[:80],
        dose_number       = int(form.get("dose_number") or 1),
        vaccinated_date   = str(form.get("vaccinated_date") or date.today().isoformat()),
        next_due_date     = str(form.get("next_due_date") or ""),
        inventory_item_id = item_id,
        is_free           = is_free,
        vet_name          = str(form.get("vet_name") or "").strip()[:80],
        notes             = str(form.get("notes") or "").strip(),
        created_by        = admin_name,
    )
    db.add(vacc)
    db.flush()

    # 库存出库
    if item_id:
        _deduct_inventory(db, item_id, 1.0, "vaccination", vacc.id, admin_name,
                          note=f"{vacc.vaccine_name or ''} 接种出库")

    # 需要收费 → 自动生成收费单
    if not is_free and item_id:
        inv_item = db.get(InventoryItem, item_id)
        if inv_item and inv_item.sell_price > 0:
            inv = Invoice(
                invoice_no      = _gen_invoice_no(db),
                customer_id     = customer_id,
                pet_id          = pet_id,
                invoice_date    = vacc.vaccinated_date,
                subtotal        = inv_item.sell_price,
                discount_amount = 0.0,
                total_amount    = inv_item.sell_price,
                payment_status  = "unpaid",
                notes           = f"疫苗接种 #{vacc.id}",
                created_by      = admin_name,
            )
            db.add(inv)
            db.flush()
            db.add(InvoiceItem(
                invoice_id  = inv.id,
                ref_type    = "vaccination",
                ref_id      = vacc.id,
                description = vacc.vaccine_name or vacc.vaccine_type,
                quantity    = 1.0,
                unit_price  = inv_item.sell_price,
                subtotal    = inv_item.sell_price,
            ))
            vacc.invoice_id = inv.id

    db.commit()
    redirect = f"/admin/customers/{db.get(Pet, pet_id).customer_id}" if pet_id and db.get(Pet, pet_id) else "/admin/vaccinations"
    return RedirectResponse(f"{redirect}?msg=疫苗记录已添加", status_code=303)


@app.get("/admin/vaccinations/{vacc_id}", response_class=HTMLResponse)
async def admin_vaccination_detail(vacc_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    vacc = db.get(Vaccination, vacc_id)
    if not vacc:
        raise HTTPException(404)
    pet  = db.get(Pet, vacc.pet_id) if vacc.pet_id else None
    cust = db.get(Customer, vacc.customer_id) if vacc.customer_id else None
    vets = [v[0] for v in db.query(Staff.name).filter(
        Staff.status.in_(["active", "probation"]), Staff.position.ilike("%医%")
    ).all()]
    vacc_items = db.query(InventoryItem).filter(
        InventoryItem.category.in_(["vaccine", "antiparasitic"])
    ).order_by(InventoryItem.name).all()
    return templates.TemplateResponse(request, "admin_vaccination_form.html", {
        "mode": "edit", "vacc": vacc,
        "pet": pet, "cust": cust,
        "vets": vets, "vacc_items": vacc_items,
        "vacc_type_zh": _VACC_TYPE_ZH, "dose_zh": _DOSE_ZH,
        "today": vacc.vaccinated_date,
        "csrf_token": _get_csrf_token(request),
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/vaccinations/{vacc_id}/edit")
async def admin_vaccination_edit(vacc_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    vacc = db.get(Vaccination, vacc_id)
    if not vacc:
        raise HTTPException(404)
    vacc.vaccine_type    = str(form.get("vaccine_type") or "other")
    vacc.vaccine_name    = str(form.get("vaccine_name") or "").strip()[:120]
    vacc.batch_no        = str(form.get("batch_no") or "").strip()[:80]
    vacc.dose_number     = int(form.get("dose_number") or 1)
    vacc.vaccinated_date = str(form.get("vaccinated_date") or "")
    vacc.next_due_date   = str(form.get("next_due_date") or "")
    vacc.vet_name        = str(form.get("vet_name") or "").strip()[:80]
    vacc.notes           = str(form.get("notes") or "").strip()
    db.commit()
    return RedirectResponse(f"/admin/vaccinations/{vacc_id}?msg=已更新", status_code=303)


@app.post("/admin/vaccinations/{vacc_id}/delete")
async def admin_vaccination_delete(vacc_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    vacc = db.get(Vaccination, vacc_id)
    if vacc:
        pet_cust_id = db.get(Pet, vacc.pet_id).customer_id if vacc.pet_id and db.get(Pet, vacc.pet_id) else None
        db.delete(vacc)
        db.commit()
        if pet_cust_id:
            return RedirectResponse(f"/admin/customers/{pet_cust_id}?msg=疫苗记录已删除", status_code=303)
    return RedirectResponse("/admin/vaccinations?msg=已删除", status_code=303)


# ── 后台：狂犬疫苗登记管理 ───────────────────────────────────────────────────

@app.get("/admin/rabies", response_class=HTMLResponse)
async def admin_rabies_list(
    request: Request, db: Session = Depends(get_db),
    q: str = Query(""), status: str = Query(""),
    date_from: str = Query(""), date_to: str = Query(""),
    page: int = Query(1),
):
    require_admin(request)
    query = db.query(RabiesVaccineRecord)
    if q:
        query = query.filter(or_(
            RabiesVaccineRecord.owner_name.ilike(f"%{q}%"),
            RabiesVaccineRecord.owner_phone.ilike(f"%{q}%"),
            RabiesVaccineRecord.cert_no.ilike(f"%{q}%"),
        ))
    if status:
        query = query.filter(RabiesVaccineRecord.status == status)
    if date_from:
        query = query.filter(RabiesVaccineRecord.created_at >= date_from)
    if date_to:
        query = query.filter(RabiesVaccineRecord.created_at <= date_to + " 23:59:59")
    total = query.count()
    page_size = 30
    records = query.order_by(RabiesVaccineRecord.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return templates.TemplateResponse(request, "admin_rabies_list.html", {
        "records": records, "total": total, "page": page,
        "total_pages": total_pages, "page_size": page_size,
        "q": q, "status": status, "date_from": date_from, "date_to": date_to,
        "status_zh": _RABIES_STATUS_ZH,
    })


@app.get("/admin/rabies/{rec_id}", response_class=HTMLResponse)
async def admin_rabies_detail(rec_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)
    vets = db.query(Staff.name).filter(
        Staff.status.in_(["active", "probation"]),
        Staff.position.ilike("%医%")
    ).all()
    vet_names = [v[0] for v in vets]
    return templates.TemplateResponse(request, "admin_rabies_detail.html", {
        "rec": rec,
        "vet_names": vet_names,
        "status_zh": _RABIES_STATUS_ZH,
        "csrf_token": _get_csrf_token(request),
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/rabies/{rec_id}/fill")
async def admin_rabies_fill(rec_id: int, request: Request, db: Session = Depends(get_db)):
    """医护填写疫苗信息 + 签名"""
    require_admin(request)
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)

    rec.vaccine_manufacturer = str(form.get("vaccine_manufacturer", "")).strip()[:120]
    rec.vaccine_batch_no     = str(form.get("vaccine_batch_no", "")).strip()[:80]
    rec.vaccine_date         = str(form.get("vaccine_date", "")).strip()[:20]
    rec.staff_name           = str(form.get("staff_name", "")).strip()[:80]

    staff_sig_data = str(form.get("staff_signature", "")).strip()
    if staff_sig_data and len(staff_sig_data) > 100:
        rec.staff_signature_path = _save_signature(staff_sig_data, f"staff_{rec_id}")
        rec.staff_signed_at = datetime.utcnow()

    rec.status = "completed"
    rec.updated_at = datetime.utcnow()
    db.flush()

    # 自动同步到疫苗档案（如果尚未同步过）
    existing_vacc = db.query(Vaccination).filter(Vaccination.rabies_record_id == rec_id).first()
    if not existing_vacc:
        # 查找狂犬疫苗库存品目（优先匹配名称含"狂犬"的）
        rabies_item = db.query(InventoryItem).filter(
            InventoryItem.category == "vaccine",
            InventoryItem.name.ilike("%狂犬%"),
        ).first()
        vacc = Vaccination(
            pet_id            = rec.pet_id,
            customer_id       = rec.customer_id,
            vaccine_type      = "rabies",
            vaccine_name      = rec.vaccine_manufacturer or "狂犬疫苗",
            batch_no          = rec.vaccine_batch_no or "",
            dose_number       = 1,
            vaccinated_date   = rec.vaccine_date or datetime.utcnow().strftime("%Y-%m-%d"),
            next_due_date     = "",   # 狂犬一般1年有效，可手动补录
            inventory_item_id = rabies_item.id if rabies_item else None,
            is_free           = True,
            rabies_record_id  = rec_id,
            vet_name          = rec.staff_name or "",
            created_by        = request.session.get("admin", ""),
        )
        db.add(vacc)
        db.flush()
        # 库存出库（有关联库存品目时）
        if rabies_item:
            _deduct_inventory(db, rabies_item.id, 1.0, "vaccination", vacc.id,
                              request.session.get("admin", ""), note=f"狂犬疫苗登记#{rec_id} 出库")

    db.commit()
    return RedirectResponse(f"/admin/rabies/{rec_id}?msg=已保存", status_code=303)


@app.post("/admin/rabies/{rec_id}/cert-no")
async def admin_rabies_cert_no(rec_id: int, request: Request, db: Session = Depends(get_db),
                                csrf_token: str = Form(""), cert_no: str = Form("")):
    """录入免疫证号（最后一步）"""
    require_admin(request)
    _require_csrf(request, csrf_token)
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)
    rec.cert_no = cert_no.strip()[:60]
    rec.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/rabies/{rec_id}?msg=免疫证号已录入", status_code=303)


@app.post("/admin/rabies/{rec_id}/delete")
async def admin_rabies_delete(rec_id: int, request: Request, db: Session = Depends(get_db),
                               csrf_token: str = Form("")):
    require_admin(request)
    _require_csrf(request, csrf_token)
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)
    db.delete(rec)
    db.commit()
    return RedirectResponse("/admin/rabies?msg=记录已删除", status_code=303)


@app.get("/admin/rabies/{rec_id}/signature/{who}")
async def admin_rabies_signature(rec_id: int, who: str, request: Request, db: Session = Depends(get_db)):
    """返回签名图片文件"""
    require_admin(request)
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)
    path = rec.owner_signature_path if who == "owner" else rec.staff_signature_path
    if not path or not Path(path).exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.post("/admin/rabies/{rec_id}/edit-owner")
async def admin_rabies_edit_owner(rec_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)
    form = await request.form()
    rec.owner_name    = str(form.get("owner_name", rec.owner_name)).strip() or rec.owner_name
    rec.owner_phone   = str(form.get("owner_phone", rec.owner_phone)).strip() or rec.owner_phone
    rec.owner_address = str(form.get("owner_address", rec.owner_address or "")).strip()
    rec.animal_name   = str(form.get("animal_name", rec.animal_name or "")).strip()
    rec.animal_breed  = str(form.get("animal_breed", rec.animal_breed or "")).strip()
    rec.animal_dob    = str(form.get("animal_dob", rec.animal_dob or "")).strip()
    rec.animal_gender = str(form.get("animal_gender", rec.animal_gender or "")).strip()
    rec.animal_color  = str(form.get("animal_color", rec.animal_color or "")).strip()
    rec.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/rabies/{rec_id}?msg=信息已更新", status_code=303)


@app.post("/admin/rabies/{rec_id}/delete")
async def admin_rabies_delete(rec_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rec = db.get(RabiesVaccineRecord, rec_id)
    if not rec:
        raise HTTPException(404)
    db.delete(rec)
    db.commit()
    return RedirectResponse("/admin/rabies?msg=记录已删除", status_code=303)


@app.get("/admin/rabies/export/excel")
async def admin_rabies_export(
    request: Request, db: Session = Depends(get_db),
    date_from: str = Query(""), date_to: str = Query(""),
    status: str = Query(""),
):
    """导出 Excel，含签名图片（Pillow 在启动时已自动安装）。"""
    require_admin(request)
    import io
    from fastapi.responses import Response as FastResponse
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image as XLImage

    query = db.query(RabiesVaccineRecord)
    if status:
        query = query.filter(RabiesVaccineRecord.status == status)
    if date_from:
        query = query.filter(RabiesVaccineRecord.created_at >= date_from)
    if date_to:
        query = query.filter(RabiesVaccineRecord.created_at <= date_to + " 23:59:59")
    records = query.order_by(RabiesVaccineRecord.id.asc()).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "狂犬疫苗免疫登记"

    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_al = Alignment(horizontal="left", vertical="center", wrap_text=True)
    hdr_font = Font(bold=True, size=10)
    hdr_fill = PatternFill("solid", fgColor="D9E1F2")

    columns = [
        ("免疫证号",       14, lambda r: r.cert_no),
        ("动物主人姓名",   12, lambda r: r.owner_name),
        ("联系地址",       28, lambda r: r.owner_address),
        ("联系电话",       14, lambda r: r.owner_phone),
        ("动物名称",       10, lambda r: r.animal_name),
        ("品种",           10, lambda r: r.animal_breed),
        ("出生年月/年龄",   9, lambda r: r.animal_dob),
        ("性别",            6, lambda r: {"male": "公", "female": "母"}.get(r.animal_gender, r.animal_gender or "")),
        ("毛色",            6, lambda r: r.animal_color),
        ("疫苗厂家",       10, lambda r: r.vaccine_manufacturer),
        ("批号",            8, lambda r: r.vaccine_batch_no),
        ("免疫时间",       12, lambda r: r.vaccine_date),
        ("免疫人员",       10, lambda r: r.staff_name),
        ("医护签名",       15, None),
        ("主人签名",       15, None),
    ]
    SIG_STAFF_COL = 14  # 1-based
    SIG_OWNER_COL = 15
    ROW_H = 35

    for col_idx, (title, width, _) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
        cell = ws.cell(row=1, column=col_idx, value=title)
        cell.font = hdr_font; cell.fill = hdr_fill
        cell.alignment = center; cell.border = border
    ws.row_dimensions[1].height = 35

    # 保持 BytesIO 引用防止 GC 在 wb.save() 前回收
    _img_bufs: list = []

    def _embed_sig(sig_path: str, col_1idx: int, row_idx: int) -> None:
        if not sig_path:
            return
        p = Path(sig_path)
        if not p.exists():
            return
        try:
            buf = io.BytesIO(p.read_bytes())
            _img_bufs.append(buf)
            xl_img = XLImage(buf)
            scale = min(180 / max(xl_img.width, 1), 40 / max(xl_img.height, 1), 1.0)
            xl_img.width  = int(xl_img.width  * scale)
            xl_img.height = int(xl_img.height * scale)
            xl_img.anchor = f"{get_column_letter(col_1idx)}{row_idx}"
            ws.add_image(xl_img)
        except Exception:
            pass

    for r_idx, rec in enumerate(records, 2):
        ws.row_dimensions[r_idx].height = ROW_H
        for c_idx, (_, _, getter) in enumerate(columns, 1):
            if getter is not None:
                try:
                    val = getter(rec)
                except Exception:
                    val = ""
            else:
                val = ""
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.alignment = center; cell.border = border
        _embed_sig(rec.staff_signature_path, SIG_STAFF_COL, r_idx)
        _embed_sig(rec.owner_signature_path,  SIG_OWNER_COL,  r_idx)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    fname = f"狂犬疫苗登记_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    return FastResponse(
        content=out.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


# ── 待领养动物 ────────────────────────────────────────────────────────────────

_ADOPTION_DIR = Path("data/adoption")
_ADOPTION_STATUS_ZH = {"available": "待领养", "adopted": "已领养", "paused": "暂停"}


def _save_adoption_file(upload: UploadFile, prefix: str) -> str:
    _ADOPTION_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(upload.filename).suffix.lower() or ".bin"
    fname = f"{prefix}_{int(datetime.utcnow().timestamp()*1000)}{ext}"
    dest = _ADOPTION_DIR / fname
    content = upload.file.read()
    dest.write_bytes(content)
    return str(dest)


# 公开 API（小程序用）
@app.get("/api/adoption")
async def api_adoption_list(db: Session = Depends(get_db)):
    pets = db.query(AdoptionPet).filter(AdoptionPet.status == "available").order_by(AdoptionPet.sort_order, AdoptionPet.id.desc()).all()
    result = []
    for p in pets:
        result.append({
            "id": p.id,
            "name": p.name,
            "species": p.species,
            "breed": p.breed,
            "age_estimate": p.age_estimate,
            "gender": p.gender,
            "personality": p.personality,
            "health_note": p.health_note,
            "requirements": p.requirements,
            "has_image1": bool(p.image1_path),
            "has_image2": bool(p.image2_path),
            "has_video": bool(p.video_path),
            "status": p.status,
        })
    return result


@app.get("/api/adoption/{pet_id}/image/{n}")
async def api_adoption_image(pet_id: int, n: int, db: Session = Depends(get_db)):
    pet = db.get(AdoptionPet, pet_id)
    if not pet:
        raise HTTPException(404)
    path = pet.image1_path if n == 1 else pet.image2_path
    if not path or not Path(path).exists():
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(path)


@app.get("/api/adoption/{pet_id}/video")
async def api_adoption_video(pet_id: int, db: Session = Depends(get_db)):
    pet = db.get(AdoptionPet, pet_id)
    if not pet or not pet.video_path or not Path(pet.video_path).exists():
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(pet.video_path)


# 后台管理
@app.get("/admin/adoption", response_class=HTMLResponse)
async def admin_adoption_list(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    pets = db.query(AdoptionPet).order_by(AdoptionPet.sort_order, AdoptionPet.id.desc()).all()
    return templates.TemplateResponse(request, "admin_adoption_list.html", {
        "pets": pets, "status_zh": _ADOPTION_STATUS_ZH,
    })


@app.get("/admin/adoption/new", response_class=HTMLResponse)
async def admin_adoption_new_form(request: Request):
    require_admin(request)
    return templates.TemplateResponse(request, "admin_adoption_form.html", {
        "pet": None, "status_zh": _ADOPTION_STATUS_ZH,
    })


@app.post("/admin/adoption/new")
async def admin_adoption_create(request: Request, db: Session = Depends(get_db),
    name: str = Form(""), species: str = Form("cat"), breed: str = Form(""),
    age_estimate: str = Form(""), gender: str = Form("unknown"),
    personality: str = Form(""), health_note: str = Form(""), requirements: str = Form(""),
    sort_order: int = Form(0), csrf_token: str = Form(""),
    image1: UploadFile = File(None), image2: UploadFile = File(None),
    video: UploadFile = File(None),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    pet = AdoptionPet(
        name=name.strip(), species=species, breed=breed.strip(),
        age_estimate=age_estimate.strip(), gender=gender,
        personality=personality.strip(), health_note=health_note.strip(),
        requirements=requirements.strip(), sort_order=sort_order,
    )
    if image1 and image1.filename:
        pet.image1_path = _save_adoption_file(image1, f"img1_{name}")
    if image2 and image2.filename:
        pet.image2_path = _save_adoption_file(image2, f"img2_{name}")
    if video and video.filename:
        pet.video_path = _save_adoption_file(video, f"video_{name}")
    db.add(pet)
    db.commit()
    return RedirectResponse("/admin/adoption?msg=已添加", status_code=303)


@app.get("/admin/adoption/{pet_id}", response_class=HTMLResponse)
async def admin_adoption_detail(pet_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    pet = db.get(AdoptionPet, pet_id)
    if not pet:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin_adoption_form.html", {
        "pet": pet, "status_zh": _ADOPTION_STATUS_ZH,
        "msg": request.query_params.get("msg"),
    })


@app.post("/admin/adoption/{pet_id}/edit")
async def admin_adoption_edit(pet_id: int, request: Request, db: Session = Depends(get_db),
    name: str = Form(""), species: str = Form("cat"), breed: str = Form(""),
    age_estimate: str = Form(""), gender: str = Form("unknown"),
    personality: str = Form(""), health_note: str = Form(""), requirements: str = Form(""),
    sort_order: int = Form(0), status: str = Form("available"), csrf_token: str = Form(""),
    image1: UploadFile = File(None), image2: UploadFile = File(None),
    video: UploadFile = File(None),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    pet = db.get(AdoptionPet, pet_id)
    if not pet:
        raise HTTPException(404)
    pet.name = name.strip(); pet.species = species; pet.breed = breed.strip()
    pet.age_estimate = age_estimate.strip(); pet.gender = gender
    pet.personality = personality.strip(); pet.health_note = health_note.strip()
    pet.requirements = requirements.strip(); pet.sort_order = sort_order
    pet.status = status
    if image1 and image1.filename:
        pet.image1_path = _save_adoption_file(image1, f"img1_{pet_id}")
    if image2 and image2.filename:
        pet.image2_path = _save_adoption_file(image2, f"img2_{pet_id}")
    if video and video.filename:
        pet.video_path = _save_adoption_file(video, f"video_{pet_id}")
    pet.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/adoption/{pet_id}?msg=已保存", status_code=303)


@app.post("/admin/adoption/{pet_id}/adopt")
async def admin_adoption_mark_adopted(pet_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""), agreement: UploadFile = File(None),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    pet = db.get(AdoptionPet, pet_id)
    if not pet:
        raise HTTPException(404)
    pet.status = "adopted"
    if agreement and agreement.filename:
        pet.adoption_agreement_path = _save_adoption_file(agreement, f"agreement_{pet_id}")
    pet.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/adoption/{pet_id}?msg=已标记为已领养", status_code=303)


@app.post("/admin/adoption/{pet_id}/delete")
async def admin_adoption_delete(pet_id: int, request: Request, db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    require_admin(request)
    _require_csrf(request, csrf_token)
    pet = db.get(AdoptionPet, pet_id)
    if not pet:
        raise HTTPException(404)
    db.delete(pet)
    db.commit()
    return RedirectResponse("/admin/adoption?msg=已删除", status_code=303)


@app.get("/admin/adoption/{pet_id}/agreement")
async def admin_adoption_agreement(pet_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    pet = db.get(AdoptionPet, pet_id)
    if not pet or not pet.adoption_agreement_path or not Path(pet.adoption_agreement_path).exists():
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(pet.adoption_agreement_path)
