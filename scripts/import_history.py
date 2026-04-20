"""
历史申请数据导入脚本
用法：在项目根目录执行
  python scripts/import_history.py <excel文件路径>

示例：
  python scripts/import_history.py /tmp/历史申请.xlsx
"""

import sys
import os
from pathlib import Path

# 把项目根目录加入 Python 路径
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from datetime import datetime
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Application, ApplicationStatus


# ── 字段映射辅助 ──────────────────────────────────────────

def _str(val) -> str:
    if pd.isna(val):
        return ""
    return str(val).strip()


def _map_gender(val: str) -> str:
    v = val.strip()
    if v in ("公猫", "公"):
        return "male"
    if v in ("母猫", "母"):
        return "female"
    return "unknown"


def _map_status(shen_he: str, jie_yu: str) -> str:
    """根据审核状态+绝育状态推断系统状态。"""
    jie_yu = jie_yu.strip()
    shen_he = shen_he.strip()

    if jie_yu == "已做":
        return ApplicationStatus.surgery_completed.value
    if jie_yu in ("已取消", "上台发现已绝育", "到店发现已绝育", "手术发现已绝育"):
        return ApplicationStatus.cancelled.value
    if jie_yu == "检查异常未做":
        return ApplicationStatus.approved.value

    # 根据审核状态判断
    if "√" in shen_he:
        return ApplicationStatus.approved.value
    if any(k in shen_he for k in ("×", "x", "不通过", "图片不符", "定位不符", "定位在", "无法判")):
        return ApplicationStatus.rejected.value

    return ApplicationStatus.approved.value  # 默认已通过（历史记录）


def _map_clinic(val: str) -> str:
    v = val.strip()
    if "龙华" in v:
        return "龙华店"
    if "横岗" in v:
        return "横岗店"
    return v


def _map_post_plan(val: str) -> str:
    v = val.strip()
    if "笼养" in v:
        return "带回家笼养"
    if "住院" in v:
        return "医院住院"
    return v


def _build_health_note(row) -> str:
    parts = []
    spirit = _str(row.get("猫咪目前精神状态如何", ""))
    symptoms = _str(row.get("是否有以下症状？（可多选）", ""))
    symptom_other = _str(row.get("是否有以下症状？（可多选）的其他回复", ""))
    pregnant = _str(row.get("对于母猫，是否怀孕或正在哺乳？", ""))
    tameness = _str(row.get("猫咪亲人程度", ""))

    if spirit and spirit not in ("正常，吃喝玩乐正常",):
        parts.append(f"精神状态：{spirit}")
    if symptoms and symptoms not in ("无明显异常",):
        parts.append(f"症状：{symptoms}")
    if symptom_other:
        parts.append(f"其他症状：{symptom_other}")
    if pregnant and pregnant not in ("否，看起来未怀孕",):
        parts.append(f"怀孕/哺乳：{pregnant}")
    if tameness:
        parts.append(f"亲人程度：{tameness}")
    return "；".join(parts)


# ── 主逻辑 ──────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法：python scripts/import_history.py <excel文件路径>")
        sys.exit(1)

    excel_path = sys.argv[1]
    if not os.path.exists(excel_path):
        print(f"文件不存在：{excel_path}")
        sys.exit(1)

    print(f"读取文件：{excel_path}")
    df = pd.read_excel(excel_path)

    # 过滤有效行（有手机号，且不是重复表头行）
    valid = df[
        df["手机号-手机号"].notna() &
        (df["手机号-手机号"].astype(str) != "手机号-手机号") &
        df["姓名"].notna() &
        (df["姓名"].astype(str) != "姓名")
    ].copy()

    print(f"有效记录数：{len(valid)} 条")

    db: Session = SessionLocal()
    inserted = 0
    skipped = 0

    try:
        for _, row in valid.iterrows():
            phone = _str(row.get("手机号-手机号", ""))
            if not phone:
                skipped += 1
                continue

            # 检查是否已存在（以手机号+填写时间去重）
            created_raw = row.get("填写时间", "")
            if pd.notna(created_raw):
                if isinstance(created_raw, datetime):
                    created_at = created_raw
                else:
                    try:
                        created_at = datetime.strptime(str(created_raw).strip(), "%Y-%m-%d %H:%M")
                    except Exception:
                        created_at = datetime.now()
            else:
                created_at = datetime.now()

            exists = (
                db.query(Application)
                .filter(Application.phone == phone)
                .filter(Application.created_at == created_at)
                .first()
            )
            if exists:
                skipped += 1
                continue

            shen_he = _str(row.get("审核状态", ""))
            jie_yu = _str(row.get("绝育状态", ""))
            status = _map_status(shen_he, jie_yu)

            cat_nickname = _str(row.get("猫咪花色/品种/特征", "")) or "未命名"
            cat_gender_raw = _str(row.get("猫咪性别", ""))
            cat_gender = _map_gender(cat_gender_raw)

            address_auto = _str(row.get("自动定位-仅限自动定位", ""))
            found_location = _str(row.get("发现地点", ""))
            rescue_area = _str(row.get("救助区域", ""))
            address_parts = [p for p in [rescue_area, found_location] if p]
            address = "；".join(address_parts) if address_parts else address_auto

            appt_raw = row.get("手术目标预约日期", "")
            if pd.notna(appt_raw):
                if isinstance(appt_raw, datetime):
                    appointment_at = appt_raw.strftime("%Y-%m-%d")
                else:
                    appointment_at = str(appt_raw).strip()[:10]
            else:
                appointment_at = ""

            app_row = Application(
                applicant_name=_str(row.get("姓名", "")),
                phone=phone,
                wechat_openid="",
                clinic_store=_map_clinic(_str(row.get("目标门店", ""))),
                appointment_at=appointment_at,
                location_address=address_auto,
                id_number=_str(row.get("身份证号-身份证", "")),
                address=address,
                cat_nickname=cat_nickname,
                cat_gender=cat_gender,
                age_estimate=_str(row.get("预估年龄", "")),
                weight_estimate=str(row.get("猫咪体重（预估）", "")).strip() if pd.notna(row.get("猫咪体重（预估）", "")) else "",
                health_note=_build_health_note(row),
                post_surgery_plan=_map_post_plan(_str(row.get("您计划如何安排术后恢复？", ""))),
                agree_ear_tip=True,
                agree_no_pet_fraud=True,
                status=status,
                is_proxy=False,
                created_at=created_at,
                updated_at=created_at,
            )
            db.add(app_row)
            inserted += 1

        db.commit()
        print(f"\n✅ 导入完成：新增 {inserted} 条，跳过 {skipped} 条（已存在或无效）")

    except Exception as e:
        db.rollback()
        print(f"\n❌ 导入失败：{e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
