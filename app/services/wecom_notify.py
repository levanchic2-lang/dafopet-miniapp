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
