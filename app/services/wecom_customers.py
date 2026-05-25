"""企业微信外部联系人同步（Phase 3 Step 1）。

把企微里员工跟进的外部联系人（客户）拉到我们系统，建立 WecomCustomerLink。
按备注手机号自动匹配到 Customer，匹配不上的留 sync_status=unmatched 待人工处理。
"""
from __future__ import annotations

import json as _json
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.models import Customer, WecomCustomerLink
from app.services import wecom_client as _wc

logger = logging.getLogger("wecom_customers")


def _normalize_phone(p: str) -> str:
    """去掉空格 / 横线 / 括号，统一成纯数字。"""
    if not p:
        return ""
    return "".join(c for c in p if c.isdigit())


def sync_all(db: Session, dry_run: bool = False) -> dict:
    """从企业微信批量拉取所有外部联系人并落库。

    流程：
      1. external_get_follow_user_list → 拿能用客户联系的员工列表
      2. external_batch_get_by_user (limit=100, 分页) → 批量拉客户详情
      3. 对每个客户：upsert WecomCustomerLink，按 remark_mobile 匹配 Customer

    dry_run=True 不落库，只统计能匹配多少。
    返回：{"pulled": N, "matched": M, "unmatched": K, "created_links": L, "errors": [...]}
    """
    stats = {"pulled": 0, "matched": 0, "unmatched": 0, "created_links": 0,
             "updated_links": 0, "errors": []}

    # Step 1: 拿员工列表
    r1 = _wc.external_get_follow_user_list()
    if r1.get("errcode") not in (0, "0", None):
        stats["errors"].append(f"get_follow_user_list: {r1}")
        return stats
    follow_users = r1.get("follow_user", []) or []
    if not follow_users:
        stats["errors"].append("没有配置客户联系的员工")
        return stats

    # Step 2 + 3: 批量分页拉所有客户详情，逐条落库
    cursor = ""
    page = 0
    while True:
        page += 1
        try:
            r2 = _wc.external_batch_get_by_user(follow_users, cursor=cursor, limit=100)
        except Exception as e:
            stats["errors"].append(f"page {page} exception: {str(e)[:200]}")
            break

        if r2.get("errcode") not in (0, "0", None):
            stats["errors"].append(f"page {page} api: {r2}")
            break

        ec_list = r2.get("external_contact_list", []) or []
        if not ec_list:
            break

        for item in ec_list:
            try:
                _upsert_link(db, item, stats, dry_run=dry_run)
            except Exception as e:
                stats["errors"].append(f"item: {str(e)[:120]}")

        cursor = r2.get("next_cursor", "") or ""
        if not cursor:
            break

        # 防止打爆 API 频控（一般 100/分钟够用，保险起见 0.3s 一页）
        time.sleep(0.3)

    if not dry_run:
        db.commit()
    return stats


def _upsert_link(db: Session, item: dict[str, Any], stats: dict, dry_run: bool = False) -> None:
    """处理一条 batch/get_by_user 返回的 external_contact 项。

    返回结构示意：
      {
        "external_contact": {"external_userid": "wozx...", "name": "客户昵称", "unionid": "...", "type": 1, "avatar": "..."},
        "follow_info": {"userid": "LiangTianBing", "remark": "高小姐", "remark_mobiles": ["19174..."], "tags": [...], ...}
      }
    """
    ec = item.get("external_contact", {}) or {}
    fi = item.get("follow_info", {}) or {}
    external_userid = (ec.get("external_userid") or "").strip()
    if not external_userid:
        return
    stats["pulled"] += 1

    follow_userid = (fi.get("userid") or "").strip()
    remark_name = (fi.get("remark") or "").strip()
    mobiles = fi.get("remark_mobiles") or []
    remark_mobile = _normalize_phone(mobiles[0] if mobiles else "")
    unionid = (ec.get("unionid") or "").strip()
    name = (ec.get("name") or "").strip()
    avatar = (ec.get("avatar") or "").strip()

    # 按备注手机号匹配 Customer（最强匹配字段）
    customer_id = None
    if remark_mobile:
        cust = db.query(Customer).filter(Customer.phone == remark_mobile).first()
        if cust:
            customer_id = cust.id
    # 兜底：按 unionid 找 Customer.wechat_openid（只有少数情况会用上，因为 openid != unionid）
    # 这里先不做，等绑定开发者ID后再加 unionid 匹配

    if dry_run:
        if customer_id:
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1
        return

    link = db.query(WecomCustomerLink).filter(
        WecomCustomerLink.external_userid == external_userid
    ).first()
    if link:
        link.follow_userid = follow_userid
        link.remark_name = remark_name
        link.remark_mobile = remark_mobile
        link.unionid = unionid or link.unionid
        link.name = name or link.name
        link.avatar = avatar or link.avatar
        link.raw_json = _json.dumps(item, ensure_ascii=False)
        # 状态：仅当之前是 unmatched 且现在能匹配上，才更新
        if customer_id and link.sync_status in ("unmatched", "", None):
            link.customer_id = customer_id
            link.sync_status = "matched"
            stats["matched"] += 1
        elif customer_id and not link.customer_id:
            link.customer_id = customer_id
            link.sync_status = "matched"
            stats["matched"] += 1
        elif link.customer_id:
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1
        stats["updated_links"] += 1
    else:
        link = WecomCustomerLink(
            external_userid=external_userid,
            customer_id=customer_id,
            follow_userid=follow_userid,
            remark_name=remark_name,
            remark_mobile=remark_mobile,
            unionid=unionid,
            name=name,
            avatar=avatar,
            sync_status="matched" if customer_id else "unmatched",
            raw_json=_json.dumps(item, ensure_ascii=False),
        )
        db.add(link)
        stats["created_links"] += 1
        if customer_id:
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1
