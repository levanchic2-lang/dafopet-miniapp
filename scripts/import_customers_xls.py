"""从老系统导出的「全部客户.xls」批量导入。

用法：
  python scripts/import_customers_xls.py path/to/全部客户.xls           # dry-run（默认）
  python scripts/import_customers_xls.py path/to/全部客户.xls --commit  # 真写库

规则：
- 按手机号 upsert：同手机号已存在则跳过（不覆盖现有客户）
- 空手机号 → 跳过
- name / phone / source / created_at 直接映射
- 会员编号 / 性别 / 累计消费 / 老机构 / 老备注 → 合并到 notes（带「[导入]」前缀）
- 余额 > 0（会员卡 + 账户） → 创建 Wallet + 1 笔 WalletTransaction(type=adjust)
- 导入归属：东环店（仅记 notes，Customer 表本身无 store 字段）
"""
from __future__ import annotations
import sys
import io
import os

# Windows 控制台 GBK 默认编码会把 ¥ 之类 Unicode 炸掉
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
from decimal import Decimal
from datetime import datetime

import pandas as pd

# 让 scripts/ 能 import app/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import Customer, Wallet, WalletTransaction


IMPORT_STORE = "东环店"  # 老系统「龙华分院」归到东环店


def _phone(v) -> str:
    if pd.isna(v):
        return ""
    if isinstance(v, (int, float)):
        return str(int(v))
    return str(v).strip()


def _ts(v):
    if pd.isna(v):
        return datetime.now()
    if isinstance(v, datetime):
        return v
    try:
        return pd.to_datetime(v).to_pydatetime()
    except Exception:
        return datetime.now()


def _f(v) -> float:
    if pd.isna(v):
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def build_notes(row) -> str:
    parts = []
    mid = row.get("会员编号")
    if pd.notna(mid):
        parts.append(f"老系统会员号:{mid}")
    g = row.get("性别")
    if pd.notna(g):
        parts.append(f"性别:{g}")
    spent = _f(row.get("累计消费"))
    if spent > 0:
        parts.append(f"老系统累计消费:¥{spent:,.0f}")
    org = row.get("所属机构")
    if pd.notna(org):
        parts.append(f"原属:{org}→{IMPORT_STORE}")
    lvl = row.get("会员级别")
    if pd.notna(lvl) and str(lvl).strip() and str(lvl) != "warmsoft客户":
        parts.append(f"等级:{lvl}")
    old_note = row.get("备注")
    if pd.notna(old_note) and str(old_note).strip():
        parts.append(f"原备注:{old_note}")
    return "[导入] " + " | ".join(parts) if parts else ""


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: python scripts/import_customers_xls.py <xls_path> [--commit]")
        sys.exit(1)
    fp = args[0]
    commit = "--commit" in args

    print(f"\n读取: {fp}")
    df = pd.read_excel(fp)
    print(f"总行数: {len(df)}")
    print(f"模式: {'真实写库' if commit else 'DRY-RUN（不写）'}\n")

    db = SessionLocal()
    # 预先查现有手机号
    existing_phones = {
        p for (p,) in db.query(Customer.phone).filter(Customer.phone.isnot(None)).all()
        if p
    }
    print(f"现有客户档案: {len(existing_phones)} 个手机号")

    n_new = 0
    n_skip_dup = 0
    n_skip_no_phone = 0
    n_wallet = 0
    wallet_total = 0.0

    new_customers = []
    wallet_jobs = []  # (phone, balance, lifetime_recharge)

    for _, row in df.iterrows():
        phone = _phone(row["联系电话"])
        if not phone:
            n_skip_no_phone += 1
            continue
        if phone in existing_phones:
            n_skip_dup += 1
            continue

        name = str(row["客户姓名"]).strip() if pd.notna(row["客户姓名"]) else ""
        source = str(row["客户来源"]).strip() if pd.notna(row["客户来源"]) else None
        notes = build_notes(row)
        created_at = _ts(row["登记日期"])

        card_bal = _f(row.get("会员卡余额"))
        acc_bal = _f(row.get("账户余额"))
        total_bal = card_bal + acc_bal

        c = Customer(
            name=name,
            phone=phone,
            source=source,
            notes=notes,
            created_at=created_at,
            updated_at=created_at,
        )
        new_customers.append(c)

        if total_bal > 0:
            wallet_jobs.append((phone, total_bal, total_bal))
            wallet_total += total_bal

        n_new += 1
        existing_phones.add(phone)

    print(f"\n=== 预演结果 ===")
    print(f"新建客户: {n_new}")
    print(f"跳过-手机号已存在: {n_skip_dup}")
    print(f"跳过-无手机号: {n_skip_no_phone}")
    print(f"附带创建钱包: {len(wallet_jobs)}（合计余额 ¥{wallet_total:,.2f}）")

    if not commit:
        print("\n[DRY-RUN] 未写库。若确认无误，加 --commit 重跑。")
        # 随机看几条
        print("\n--- 预览前 3 个新客户 ---")
        for c in new_customers[:3]:
            print(f"  {c.name} · {c.phone} · {c.source} · {c.notes[:80]}")
        db.close()
        return

    print("\n开始写库…")
    db.add_all(new_customers)
    db.flush()  # 拿到 id

    # 建 phone → customer.id 索引
    phone_to_id = {c.phone: c.id for c in new_customers}

    for phone, balance, lifetime in wallet_jobs:
        cid = phone_to_id.get(phone)
        if not cid:
            continue
        w = Wallet(
            customer_id=cid,
            balance=balance,
            lifetime_recharge=lifetime,
            lifetime_consume=0,
        )
        db.add(w)
        db.flush()
        tx = WalletTransaction(
            wallet_id=w.id,
            customer_id=cid,
            type="adjust",
            amount=balance,
            balance_after=balance,
            note=f"老系统历史余额导入（{IMPORT_STORE}）",
            operator="系统导入",
            store=IMPORT_STORE,
        )
        db.add(tx)
        n_wallet += 1

    db.commit()
    print(f"✓ 已提交 {n_new} 个客户 + {n_wallet} 个钱包")
    db.close()


if __name__ == "__main__":
    main()
