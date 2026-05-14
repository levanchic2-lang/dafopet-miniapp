"""
一次性补救脚本：修复狂犬疫苗记录的 pet_id 错挂问题。

背景：早期 /rabies 和 /api/rabies/submit 在收到 pet_id 时会无条件复用，
即使表单里的 animal_name 跟那只宠物的名字不一致，结果同一个主人多只动物的
狂犬记录全挂在第一只宠物身上，客户档案只显示一只。

本脚本扫描所有 RabiesVaccineRecord：
  - 若 record.animal_name 与 record.pet 的 name 不一致：
      1. 在该客户名下按 (customer_id, animal_name) 找已有 Pet
      2. 找不到则新建一只（species=dog，沿用 record 里的品种/性别/生日/毛色）
      3. 把 record.pet_id 指向正确的 Pet

默认 --dry-run，只打印不写库。确认无误后加 --apply 真正执行。

用法（在项目根目录）：
    python scripts/fix_rabies_pet_links.py           # 仅打印
    python scripts/fix_rabies_pet_links.py --apply   # 真正执行
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.models import RabiesVaccineRecord, Pet  # noqa: E402


def normalize(s):
    return (s or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = ap.parse_args()

    db = SessionLocal()
    records = db.query(RabiesVaccineRecord).order_by(RabiesVaccineRecord.id).all()
    print(f"扫描 {len(records)} 条狂犬记录…")

    plan = []  # [(record_id, action, detail)]
    new_pets_cache = {}  # (customer_id, name) -> Pet（脚本内新建去重）

    for rec in records:
        animal_name = normalize(rec.animal_name)
        if not animal_name or not rec.customer_id:
            continue
        cur_pet = db.get(Pet, rec.pet_id) if rec.pet_id else None
        cur_name = normalize(cur_pet.name) if cur_pet else ""

        # 情况 1：当前 pet_id 名字对得上 → 不动
        if cur_pet and cur_name == animal_name:
            continue

        # 情况 2：名字对不上 / pet_id 为空 → 找或建
        # 优先用缓存（脚本内已经为同一对 customer/name 建过 Pet）
        cache_key = (rec.customer_id, animal_name)
        target_pet = new_pets_cache.get(cache_key)
        if not target_pet:
            target_pet = (
                db.query(Pet)
                .filter(Pet.customer_id == rec.customer_id, Pet.name == animal_name)
                .first()
            )
        if target_pet:
            plan.append((rec.id, "relink",
                f"customer_id={rec.customer_id}, animal_name={animal_name}, "
                f"old_pet_id={rec.pet_id}({cur_name!r}) → existing pet_id={target_pet.id}"))
            if args.apply:
                rec.pet_id = target_pet.id
        else:
            # 新建 Pet
            new_pet = Pet(
                customer_id=rec.customer_id,
                name=animal_name,
                breed=normalize(rec.animal_breed),
                gender=normalize(rec.animal_gender),
                birthday_estimate=normalize(rec.animal_dob),
                color_pattern=normalize(rec.animal_color),
                species="dog",
            )
            plan.append((rec.id, "create+link",
                f"customer_id={rec.customer_id}, animal_name={animal_name}, "
                f"old_pet_id={rec.pet_id}({cur_name!r}) → 新建 Pet"))
            if args.apply:
                db.add(new_pet)
                db.flush()
                rec.pet_id = new_pet.id
                new_pets_cache[cache_key] = new_pet

    print()
    print(f"将处理 {len(plan)} 条记录：")
    for rid, action, detail in plan:
        print(f"  record#{rid:>4}  {action:<12}  {detail}")

    if not plan:
        print("✓ 无需修复，所有记录的 pet_id 名字都对得上。")
    elif args.apply:
        db.commit()
        print(f"\n✓ 已写库 {len(plan)} 条。")
    else:
        print("\n[dry-run] 未写库。确认无误后加 --apply 重新执行。")

    db.close()


if __name__ == "__main__":
    main()
