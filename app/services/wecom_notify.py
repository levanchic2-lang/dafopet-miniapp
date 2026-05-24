"""企业微信工作通知推送（Phase 2）。

封装常用消息模板：
  - textcard（标题 + 摘要 + 跳转链接）— 最适合工作提醒
  - text（纯文字）— 兜底
  - markdown — 用于汇总型消息

调度策略：
  - push_workbench_digest(userid, store): 早班 / 午班定时推一次「今日待办汇总」
  - push_single_event(userid, key, payload): 单条事件即时推送
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.config import settings
from app.services import wecom_client as _wc

logger = logging.getLogger("wecom_notify")


def _public_base() -> str:
    return (settings.public_base_url or "").rstrip("/")


def push_textcard(
    touser: str | Iterable[str],
    title: str,
    description: str,
    url: str,
    btntxt: str = "查看",
) -> dict:
    """推送一张工作卡片消息。

    touser 可以是单个 userid（"LiangTianBing"）或多个用 | 连接（"id1|id2"）或可迭代。
    description 支持简单 HTML（<div class="gray">…</div> 灰字），最长约 512 字符。
    """
    if not _wc.enabled():
        logger.info("[wecom_notify] 未配置企业微信，跳过推送")
        return {"errcode": -1, "errmsg": "disabled"}
    if isinstance(touser, (list, tuple, set)):
        touser = "|".join([u for u in touser if u])
    if not touser:
        return {"errcode": -1, "errmsg": "no recipient"}
    # 跳转链接必须完整 URL；若传相对路径，自动拼上 public_base_url
    if url.startswith("/"):
        base = _public_base()
        if base:
            url = base + url
    payload = {
        "touser": touser,
        "msgtype": "textcard",
        "textcard": {
            "title": title[:128],
            "description": description[:512],
            "url": url,
            "btntxt": btntxt[:4] if btntxt else "查看",
        },
    }
    return _wc.send_app_message(payload)


def push_text(touser: str | Iterable[str], content: str) -> dict:
    """纯文字消息（用于不需要跳转的简单提示）。"""
    if not _wc.enabled():
        return {"errcode": -1, "errmsg": "disabled"}
    if isinstance(touser, (list, tuple, set)):
        touser = "|".join([u for u in touser if u])
    if not touser:
        return {"errcode": -1, "errmsg": "no recipient"}
    payload = {
        "touser": touser,
        "msgtype": "text",
        "text": {"content": content[:2048]},
    }
    return _wc.send_app_message(payload)


def push_test(touser: str) -> dict:
    """发送一条测试卡片，用于验证企微推送链路是否通畅。"""
    return push_textcard(
        touser=touser,
        title="🐱 测试推送",
        description=(
            "<div class=\"gray\">这是来自 大风动物医院 TNR 系统 的测试消息</div>"
            "<div>如果你能看到这条卡片，说明企业微信工作通知已配置成功。</div>"
        ),
        url="/admin/customers",
        btntxt="进系统",
    )


# ── 单条业务事件推送（接 13 类提醒时一个个加）──

def push_tnr_pending(touser: str, count: int) -> dict:
    """TNR 待人工审核提醒。"""
    return push_textcard(
        touser=touser,
        title=f"🐈 {count} 个 TNR 申请待审核",
        description=(
            f"<div class=\"gray\">需要医生人工审核照片</div>"
            f"<div>累计 {count} 单等待处理，建议尽快查看以免延误申请人体验。</div>"
        ),
        url="/admin?status=pending_manual",
        btntxt="去审核",
    )


def push_invoice_overdue(touser: str, count: int, total_amount: float) -> dict:
    """超期未付收费单提醒。"""
    return push_textcard(
        touser=touser,
        title=f"💰 {count} 笔收费单待催收",
        description=(
            f"<div class=\"gray\">超过 7 天未付清</div>"
            f"<div>合计未收金额 ¥{total_amount:.2f}，建议联系客户结算。</div>"
        ),
        url="/admin/invoices?status=unpaid",
        btntxt="去催收",
    )


def push_deposit_held(touser: str, count: int) -> dict:
    """押金超 7 天未结算。"""
    return push_textcard(
        touser=touser,
        title=f"🔒 {count} 笔押金待结算",
        description=(
            "<div class=\"gray\">收押金后超过 7 天未抵扣或退还</div>"
            "<div>请确认对应业务是否完成，结算押金。</div>"
        ),
        url="/admin/deposits?status=held",
        btntxt="去结算",
    )


def push_surgery_photo_missing(touser: str, count: int) -> dict:
    """术后照片未上传。"""
    return push_textcard(
        touser=touser,
        title=f"📸 {count} 例术后照片未上传",
        description=(
            "<div class=\"gray\">已完成手术但未上传术后照片</div>"
            "<div>影响 TNR 项目验收，请医生尽快补传。</div>"
        ),
        url="/admin?status=surgery_completed&missing_photo=1",
        btntxt="去上传",
    )


def push_consent_pending(touser: str, count: int) -> dict:
    """待签协议提醒。"""
    return push_textcard(
        touser=touser,
        title=f"📝 {count} 份协议等待签署",
        description=(
            "<div class=\"gray\">客户尚未完成手写签字</div>"
            "<div>请回访客户提醒签署，或重新发送签署链接。</div>"
        ),
        url="/admin/consent-tasks?status=pending",
        btntxt="去查看",
    )


def push_rabies_pending(touser: str, count: int) -> dict:
    """狂犬疫苗待医护填写完成。"""
    return push_textcard(
        touser=touser,
        title=f"💉 {count} 张狂犬登记待完成",
        description=(
            "<div class=\"gray\">主人已签字，等医护录入疫苗信息</div>"
            "<div>请尽快补全批号、免疫人员后录入证号。</div>"
        ),
        url="/admin/rabies?status=staff_pending",
        btntxt="去录入",
    )


def push_followup_today(touser: str, count: int) -> dict:
    """今日回访任务。"""
    return push_textcard(
        touser=touser,
        title=f"📞 今日有 {count} 个回访任务",
        description=(
            "<div class=\"gray\">手术/门诊/美容 等回访</div>"
            "<div>建议上午 10 点前完成电话或短信回访。</div>"
        ),
        url="/admin/follow-ups?status=due",
        btntxt="去回访",
    )


# ── 工作台摘要：把 dashboard.build_workbench 的所有 count>0 项汇总到一张卡 ──

def push_workbench_digest(db, user) -> dict | None:
    """根据员工的门店推送今日待办汇总卡。

    返回：None = 没有待办，跳过；否则返回企微 API 响应。
    """
    if not user.wecom_userid:
        return None
    from app.services.dashboard import build_workbench
    wb = build_workbench(db, user.store or "")
    urgent = [c for c in wb.get("urgent", []) if (c.get("count") or 0) > 0]
    weekly = [c for c in wb.get("weekly", []) if (c.get("count") or 0) > 0]
    stock  = [c for c in wb.get("stock", [])  if (c.get("count") or 0) > 0]
    total = sum((c.get("count") or 0) for c in urgent + weekly + stock)
    if total == 0:
        return None  # 没待办，不打扰

    parts = []
    if urgent:
        parts.append("<div class=\"gray\">🔴 今日必做</div>")
        for c in urgent[:5]:
            parts.append(f"<div>· {c.get('title','—')}：<b>{c.get('count')}</b></div>")
    if weekly:
        parts.append("<div class=\"gray\" style=\"margin-top:4px;\">🟡 本周提醒</div>")
        for c in weekly[:5]:
            parts.append(f"<div>· {c.get('title','—')}：<b>{c.get('count')}</b></div>")
    if stock:
        parts.append("<div class=\"gray\" style=\"margin-top:4px;\">📦 库存 / 经营</div>")
        for c in stock[:4]:
            parts.append(f"<div>· {c.get('title','—')}：<b>{c.get('count')}</b></div>")

    description = "".join(parts)
    store_label = user.store or "全门店"
    title = f"📋 今日待办 · {store_label}（{total} 项）"
    return push_textcard(
        touser=user.wecom_userid,
        title=title,
        description=description,
        url="/admin/customers",
        btntxt="进系统",
    )


# ═════════════════════════════════════════════════════════════
# 即时事件推送（C 模式）
# 在业务路由里直接调，客户提交后员工立刻收到卡片
# ═════════════════════════════════════════════════════════════

def _resolve_recipients(db, store_short: str = "", roles: tuple = ("superadmin", "staff")) -> list[str]:
    """根据门店和角色返回 wecom_userid 列表。

    store_short: '东环店' / '横岗店' / '' (空 = 所有门店)
    roles: 默认所有角色都收（超管 + 普通员工）
    返回去重的 userid 列表。
    """
    from app.models import AdminUser
    q = db.query(AdminUser).filter(
        AdminUser.is_active == True,
        AdminUser.wecom_userid != "",
        AdminUser.role.in_(roles),
    )
    users = q.all()
    out: list[str] = []
    for u in users:
        # 超管收所有门店；员工只收自己门店或不限门店
        if u.role == "superadmin":
            out.append(u.wecom_userid)
        elif not store_short or not u.store or u.store == store_short:
            out.append(u.wecom_userid)
    return list(dict.fromkeys(out))  # 去重保序


def _safe_push(touser_list: list[str], **kwargs) -> dict | None:
    """安全推送：无收件人/未启用时静默返回 None；失败仅打日志不抛错。"""
    if not touser_list or not _wc.enabled():
        return None
    try:
        return push_textcard(touser="|".join(touser_list), **kwargs)
    except Exception as e:
        logger.warning("[wecom event push] %s | kwargs=%s", e, kwargs)
        return None


# ── 单条业务事件 ──────────────────────────────────────────────────────────

def notify_rabies_submitted(db, rec) -> None:
    """新狂犬登记：主人已签字 → 推给门店全员，请尽快接待打针。"""
    store = (rec.clinic_store or "").strip()
    tousers = _resolve_recipients(db, store_short=store)
    pet_desc = f"{rec.animal_name or '—'}（{rec.animal_breed or '?'}）"
    _safe_push(
        tousers,
        title=f"💉 {rec.owner_name} 已提交狂犬登记",
        description=(
            f"<div class=\"gray\">{store or '门店未指定'} · {rec.owner_phone}</div>"
            f"<div>动物：{pet_desc}</div>"
            f"<div>请医护尽快接待，录入疫苗信息。</div>"
        ),
        url=f"/admin/rabies/{rec.id}",
        btntxt="去录入",
    )


def notify_tnr_pending_manual(db, app_obj) -> None:
    """新 TNR 申请进入人工审核 → 推给超管 + 同店员工。"""
    store = (app_obj.clinic_store or "").strip()
    tousers = _resolve_recipients(db, store_short=store)
    contact = getattr(app_obj, "applicant_name", None) or "申请人"
    phone = getattr(app_obj, "phone", None) or "—"
    cat = getattr(app_obj, "cat_nickname", None) or "未命名"
    _safe_push(
        tousers,
        title=f"🐈 新 TNR 申请待审核",
        description=(
            f"<div class=\"gray\">{store or '门店未指定'} · {phone}</div>"
            f"<div>申请人：{contact} · 猫咪：{cat}</div>"
            f"<div>需要人工查看照片决定通过/拒绝。</div>"
        ),
        url=f"/admin?status=pending_manual#app-{app_obj.id}",
        btntxt="去审核",
    )


def notify_consent_signed(db, task) -> None:
    """协议签署完成 → 推给发起人。

    ConsentTask.initiated_by 存的是发起人的 username，
    用它在 AdminUser 表里找对应 wecom_userid。
    若发起人没绑企微，回退到推给超管。
    """
    from app.models import AdminUser, Customer
    tousers: list[str] = []
    if task.initiated_by:
        creator = db.query(AdminUser).filter(
            AdminUser.username == task.initiated_by,
            AdminUser.is_active == True,
            AdminUser.wecom_userid != "",
        ).first()
        if creator:
            tousers.append(creator.wecom_userid)
    if not tousers:
        # 回退：推给所有超管
        tousers = _resolve_recipients(db, roles=("superadmin",))
    if not tousers:
        return
    title = (getattr(task, "title", None) or "客户协议")[:40]
    customer_name = ""
    if task.customer_id:
        c = db.get(Customer, task.customer_id)
        if c:
            customer_name = c.name or ""
    _safe_push(
        tousers,
        title=f"✅ 协议已签署：{title}",
        description=(
            f"<div class=\"gray\">客户：{customer_name or '—'} · 发起人：{task.initiated_by or '—'}</div>"
            f"<div>已自动生成 PDF 归档，可下载存档。</div>"
        ),
        url=f"/admin/consent-tasks/{task.id}",
        btntxt="查看",
    )


def notify_appointment_created(db, appt) -> None:
    """客户在小程序创建预约 → 推给对应门店员工。

    （只在 /api/appointments/create 入口调用；后台手动创建的预约不会触发）
    """
    store = (appt.store or "").strip()
    # 门店全名 → 短名（dashboard 用的是短→全反向映射）
    from app.services.dashboard import _STORE_SHORT_TO_FULL
    _STORE_FULL_TO_SHORT = {v: k for k, v in _STORE_SHORT_TO_FULL.items()}
    store_short = _STORE_FULL_TO_SHORT.get(store, "")
    tousers = _resolve_recipients(db, store_short=store_short)
    when = f"{appt.appointment_date or '?'} {appt.appointment_time or ''}"
    _safe_push(
        tousers,
        title=f"📅 新预约：{appt.customer_name or '客户'}",
        description=(
            f"<div class=\"gray\">{store or '门店未指定'} · {appt.phone or '—'}</div>"
            f"<div>项目：{appt.service_name or appt.category or '—'}</div>"
            f"<div>时间：{when}</div>"
        ),
        url=f"/admin/appointments?date={appt.appointment_date or ''}",
        btntxt="去查看",
    )


def dispatch_workbench_to_all(db) -> dict:
    """遍历所有 active + 已绑 wecom_userid 的员工，按各自门店推送工作台摘要。

    返回统计：{"sent": N, "skipped": M, "failed": [...]}
    """
    from app.models import AdminUser
    users = db.query(AdminUser).filter(
        AdminUser.is_active == True,
        AdminUser.wecom_userid != "",
    ).all()
    sent = 0
    skipped = 0
    failed: list[str] = []
    for u in users:
        try:
            res = push_workbench_digest(db, u)
            if res is None:
                skipped += 1
                continue
            errcode = res.get("errcode")
            if errcode in (0, "0", None):
                sent += 1
            else:
                failed.append(f"{u.username}: errcode={errcode} {res.get('errmsg','')[:80]}")
        except Exception as e:
            failed.append(f"{u.username}: {str(e)[:100]}")
    return {"sent": sent, "skipped": skipped, "failed": failed, "total": len(users)}
