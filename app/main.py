from __future__ import annotations

import json
import mimetypes
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.config import settings
from app.database import get_db, init_db
from app.models import Application, ApplicationStatus, AuditLog, MediaFile, MediaKind
from app.services.ai_review import apply_auto_status_from_ai, review_application_media
from app.services.notify import notify_application_result
from app.services.backup_local import create_backup_zip, is_safe_backup_filename, list_backup_zips
from app.services.wechat_miniapp import push_application_result, push_surgery_done, wechat_code2session

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="tnr_session")
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


def _admin_ok(request: Request) -> bool:
    return bool(request.session.get("admin"))


def require_admin(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=401, detail="需要医院后台登录")


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
            actor="admin",
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
        "wechat_message_page": settings.wechat_message_page,
        "wechat_fields_application_result": settings.wechat_fields_application_result,
        "wechat_fields_surgery_done": settings.wechat_fields_surgery_done,
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


_ALLOWED_CLINIC_STORES = frozenset({"大风动物医院（东环店）", "大风动物医院（横岗店）"})


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
    out["appointment_at"] = need("期望手术日期", appointment_at, 40)
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

    return {
        "id": row.id,
        "status": row.status,
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
        "phone_masked": mask_phone(row.phone),
        "cat_nickname": row.cat_nickname or "",
        "cat_gender": row.cat_gender or "",
        "age_estimate": row.age_estimate or "",
        "health_note": row.health_note or "",
        "address": row.address or "",
        "note": notes,
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
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "title": "TNR 审核与手术登记",
            "apps": rows,
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


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...), csrf_token: str = Form("")):
    _require_csrf(request, csrf_token)
    if password == settings.admin_password:
        request.session["admin"] = True
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "title": "医院后台登录", "error": "密码错误", "csrf_token": _get_csrf_token(request)},
        status_code=401,
    )


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/backup/create", name="admin_backup_create")
async def admin_backup_create(request: Request, db: Session = Depends(get_db), csrf_token: str = Form("")):
    require_admin(request)
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
    """一键清理：scope=all 删除全部申请及关联数据与上传文件；scope=drafts 仅删除草稿。"""
    require_admin(request)
    _require_csrf(request, csrf_token)
    scope = (scope or "all").strip().lower()
    if scope not in ("all", "drafts"):
        return RedirectResponse("/admin?purge_err=1", status_code=303)
    confirm = (confirm or "").strip()
    if scope == "drafts":
        if confirm != "确认删除全部草稿":
            return RedirectResponse("/admin?purge_err=1", status_code=303)
        q = db.query(Application).filter(Application.status == ApplicationStatus.draft.value)
    else:
        if confirm != "确认删除全部申请数据":
            return RedirectResponse("/admin?purge_err=1", status_code=303)
        q = db.query(Application)

    rows = q.all()
    n = len(rows)
    for row in rows:
        _rmtree_app_uploads(row.id)
        db.delete(row)
    if scope == "all":
        db.query(AuditLog).delete(synchronize_session=False)
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
    push_application_result(
        db,
        application_id=app_id,
        openid=row.wechat_openid,
        applicant_name=row.applicant_name,
        status_text="未通过",
        phone_masked=row.phone,
        note=(reason or "请联系医院前台")[:20],
        submitted_at=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
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
        },
        "标记手术完成",
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
