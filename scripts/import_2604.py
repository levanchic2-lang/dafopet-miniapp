"""
2026-04-24/25 新增申请导入脚本（共 2 条）
在项目根目录执行：python scripts/import_2604.py
"""
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import Application, ApplicationStatus

RECORDS = [
    {
        # Row 190 — 4.24日
        "applicant_name":   "郑香玉",
        "phone":            "15323455977",
        "wechat_openid":    "",
        "clinic_store":     "龙华店",
        "appointment_at":   "2026-04-25",
        "location_address": "中国广东省深圳市",
        "id_number":        "632123199706180526",
        "address":          "秋港花园；秋港花园D 5楼下灌木丛",
        "cat_nickname":     "黑猫带一点白",
        "cat_gender":       "male",
        "age_estimate":     "6个月-1岁（最佳）",
        "weight_estimate":  "6",
        "health_note":      "花色特征：黑猫带一点白；亲人程度：亲人，随便摸",
        "post_surgery_plan": "医院住院",
        "status":           ApplicationStatus.surgery_completed.value,
        "agree_ear_tip":    True,
        "agree_no_pet_fraud": True,
        "is_proxy":         False,
        "created_at":       datetime(2026, 4, 24, 12, 31),
        "updated_at":       datetime(2026, 4, 24, 12, 31),
    },
    {
        # Row 194 — 4.25日（手术发现已绝育）
        "applicant_name":   "张春晓",
        "phone":            "19856109910",
        "wechat_openid":    "",
        "clinic_store":     "龙华店",
        "appointment_at":   "2026-04-26",
        "location_address": "中国广东省深圳市",
        "id_number":        "340603199502220224",
        "address":          "1980科技文化产业园；停车场",
        "cat_nickname":     "黑白",
        "cat_gender":       "female",
        "age_estimate":     "6个月-1岁（最佳）",
        "weight_estimate":  "3.5",
        "health_note":      "花色特征：黑白；怀孕/哺乳：是，肚子很大/乳头红肿有奶；亲人程度：可摸但警惕",
        "post_surgery_plan": "医院住院",
        "status":           ApplicationStatus.cancelled.value,  # 手术发现已绝育
        "agree_ear_tip":    True,
        "agree_no_pet_fraud": True,
        "is_proxy":         False,
        "created_at":       datetime(2026, 4, 25, 20, 33),
        "updated_at":       datetime(2026, 4, 25, 20, 33),
    },
]


def main():
    db = SessionLocal()
    inserted = 0
    skipped = 0
    try:
        for rec in RECORDS:
            exists = (
                db.query(Application)
                .filter(Application.phone == rec["phone"])
                .filter(Application.created_at == rec["created_at"])
                .first()
            )
            if exists:
                print(f"跳过（已存在）：{rec['applicant_name']} / {rec['phone']}")
                skipped += 1
                continue

            app_row = Application(**rec)
            db.add(app_row)
            inserted += 1
            print(f"新增：{rec['applicant_name']} / {rec['phone']} / {rec['status']}")

        db.commit()
        print(f"\n✅ 完成：新增 {inserted} 条，跳过 {skipped} 条")
    except Exception as e:
        db.rollback()
        print(f"\n❌ 失败：{e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
