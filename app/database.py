from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from app.config import settings


class Base(DeclarativeBase):
    pass


Path("data").mkdir(parents=True, exist_ok=True)
_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # SQLite 用 NullPool：每个请求独立连接，彻底避免连接池耗尽导致的 504
    poolclass=NullPool if _is_sqlite else QueuePool,
)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        """多进程（uvicorn --workers）安全：
        - WAL：读不阻塞写、写不阻塞读，并发下不易报 database is locked
        - busy_timeout：拿不到写锁时最多等 5s 而不是立刻 500
        - synchronous=NORMAL：WAL 下兼顾安全与速度
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _try_sqlite_migrations()
    _seed_data()
    _heal_rabies_pet_links()
    _backfill_followups()


def _backfill_followups() -> None:
    """为历史 Visit 补建 FollowUp 行（幂等）。
    新装机/旧库升级后，过去 30 天内的就诊会自动生成回访任务；
    避免给太久远的 visit 也建（无意义、可能淹没真正待办）。
    """
    try:
        from app.models import Visit, FollowUp, Pet
    except Exception:
        return
    sess = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        from datetime import date, timedelta
        # 只回填最近 30 天的就诊
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        visits = (
            sess.query(Visit)
            .filter(Visit.visit_date >= cutoff)
            .order_by(Visit.id.asc())
            .all()
        )
        rules = {
            "surgery": 3, "postop": 2, "outpatient": 7, "beauty": 14,
            "followup": 0, "vaccine": 0, "surgery_consult": 0, "other": 7,
        }
        created = 0
        import secrets as _secrets
        for v in visits:
            exists = sess.query(FollowUp).filter(FollowUp.visit_id == v.id).first()
            if exists:
                continue
            # 计算 planned_date
            planned = (v.follow_up_at or "").strip()
            if not planned:
                days = rules.get((v.visit_type or "outpatient").strip(), 0)
                if not days or not v.visit_date:
                    continue
                try:
                    y, m, d = v.visit_date[:10].split("-")
                    planned = (date(int(y), int(m), int(d)) + timedelta(days=days)).isoformat()
                except Exception:
                    continue
            if not planned:
                continue
            pet = sess.get(Pet, v.pet_id) if v.pet_id else None
            sess.add(FollowUp(
                visit_id=v.id,
                customer_id=v.customer_id,
                pet_id=v.pet_id,
                store=(pet.store or "") if pet else "",
                assigned_to=(v.vet_name or "").strip()[:80],
                planned_date=planned,
                status="pending",
                feedback_token=_secrets.token_urlsafe(12)[:16],
            ))
            created += 1
        if created:
            sess.commit()
            print(f"[backfill_followups] 补建 {created} 条回访任务")
    except Exception as e:
        sess.rollback()
        print(f"[backfill_followups] skip: {e}")
    finally:
        sess.close()


def _heal_rabies_pet_links() -> None:
    """
    一次性数据修复：早期 /rabies 与 /api/rabies/submit 收到 pet_id 时无条件复用，
    同一主人多只动物的狂犬记录会全部错挂到第一只宠物身上。
    本函数扫描所有 RabiesVaccineRecord：若 animal_name 与当前 pet.name 不一致，
    重新挂到正确的 Pet（按 customer_id + name 查已有，否则按 record 字段新建）。
    幂等：修复后不再产生不一致即为 no-op。
    """
    try:
        from app.models import RabiesVaccineRecord, Pet, Vaccination  # 延迟 import
    except Exception:
        return
    sess = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        records = sess.query(RabiesVaccineRecord).all()
        cache: dict[tuple[int, str], Pet] = {}
        changed = 0
        created = 0
        for rec in records:
            animal_name = (rec.animal_name or "").strip()
            if not animal_name or not rec.customer_id:
                continue
            cur_pet = sess.get(Pet, rec.pet_id) if rec.pet_id else None
            cur_name = (cur_pet.name or "").strip() if cur_pet else ""
            if cur_pet and cur_name == animal_name:
                continue  # 已对得上
            key = (rec.customer_id, animal_name)
            target = cache.get(key) or (
                sess.query(Pet)
                .filter(Pet.customer_id == rec.customer_id, Pet.name == animal_name)
                .first()
            )
            if not target:
                target = Pet(
                    customer_id=rec.customer_id,
                    name=animal_name,
                    breed=(rec.animal_breed or "").strip(),
                    gender=(rec.animal_gender or "").strip(),
                    birthday_estimate=(rec.animal_dob or "").strip(),
                    color_pattern=(rec.animal_color or "").strip(),
                    species="dog",
                )
                sess.add(target)
                sess.flush()
                created += 1
            cache[key] = target
            rec.pet_id = target.id
            changed += 1
        if changed:
            sess.flush()
            print(f"[heal_rabies] 修复 {changed} 条狂犬记录（新建 {created} 只宠物）")

        # 同步疫苗档案：把 Vaccination 的 pet_id 拉回到 rabies_record.pet_id
        vacc_fixed = 0
        vaccs = sess.query(Vaccination).filter(Vaccination.rabies_record_id.isnot(None)).all()
        for v in vaccs:
            rec = sess.get(RabiesVaccineRecord, v.rabies_record_id)
            if rec and rec.pet_id and v.pet_id != rec.pet_id:
                v.pet_id = rec.pet_id
                vacc_fixed += 1
        if vacc_fixed:
            print(f"[heal_rabies] 同步 {vacc_fixed} 条 Vaccination.pet_id")

        if changed or vacc_fixed:
            sess.commit()
    except Exception as e:
        sess.rollback()
        # 数据修复失败不阻塞启动
        print(f"[heal_rabies] skip: {e}")
    finally:
        sess.close()


def _try_sqlite_migrations() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    # 轻量迁移：为演示项目在已有库上补列
    try:
        with engine.connect() as conn:
            cols = conn.execute(text("PRAGMA table_info(applications)")).fetchall()
            names = {c[1] for c in cols}  # (cid, name, type, notnull, dflt_value, pk)
            if "wechat_openid" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN wechat_openid VARCHAR(64) DEFAULT ''"))
            if "clinic_store" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN clinic_store VARCHAR(80) DEFAULT ''"))
            if "appointment_at" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN appointment_at VARCHAR(40) DEFAULT ''"))
            if "location_lat" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN location_lat VARCHAR(32) DEFAULT ''"))
            if "location_lng" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN location_lng VARCHAR(32) DEFAULT ''"))
            if "location_address" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN location_address VARCHAR(500) DEFAULT ''"))
            if "id_number" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN id_number VARCHAR(40) DEFAULT ''"))
            if "post_surgery_plan" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN post_surgery_plan VARCHAR(120) DEFAULT ''"))
            if "is_proxy" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN is_proxy BOOLEAN DEFAULT 0"))
            if "proxy_name" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN proxy_name VARCHAR(120) DEFAULT ''"))
            if "proxy_phone" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN proxy_phone VARCHAR(40) DEFAULT ''"))
            if "proxy_relation" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN proxy_relation VARCHAR(40) DEFAULT ''"))
            if "cat_breed" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN cat_breed VARCHAR(80) DEFAULT ''"))
            if "cat_color" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN cat_color VARCHAR(80) DEFAULT ''"))

            # ── 单据锁定支持：Vaccination / DewormingRecord 加 status + 作废元数据 ──
            try:
                vacc_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(vaccinations)")).fetchall()}
                if "status" not in vacc_cols:
                    conn.execute(text("ALTER TABLE vaccinations ADD COLUMN status VARCHAR(20) DEFAULT 'active'"))
                if "voided_by" not in vacc_cols:
                    conn.execute(text("ALTER TABLE vaccinations ADD COLUMN voided_by VARCHAR(80) DEFAULT ''"))
                if "voided_at" not in vacc_cols:
                    conn.execute(text("ALTER TABLE vaccinations ADD COLUMN voided_at DATETIME DEFAULT NULL"))
                if "void_reason" not in vacc_cols:
                    conn.execute(text("ALTER TABLE vaccinations ADD COLUMN void_reason VARCHAR(200) DEFAULT ''"))
            except Exception:
                pass
            try:
                dew_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(deworming_records)")).fetchall()}
                if "invoice_id" not in dew_cols:
                    conn.execute(text("ALTER TABLE deworming_records ADD COLUMN invoice_id INTEGER DEFAULT NULL"))
                if "status" not in dew_cols:
                    conn.execute(text("ALTER TABLE deworming_records ADD COLUMN status VARCHAR(20) DEFAULT 'active'"))
                if "voided_by" not in dew_cols:
                    conn.execute(text("ALTER TABLE deworming_records ADD COLUMN voided_by VARCHAR(80) DEFAULT ''"))
                if "voided_at" not in dew_cols:
                    conn.execute(text("ALTER TABLE deworming_records ADD COLUMN voided_at DATETIME DEFAULT NULL"))
                if "void_reason" not in dew_cols:
                    conn.execute(text("ALTER TABLE deworming_records ADD COLUMN void_reason VARCHAR(200) DEFAULT ''"))
            except Exception:
                pass
            # 处方单/销售单/麻醉单/检查单 加作废元数据（status 字段都已有）
            for tbl in ("prescriptions", "sales_orders", "anesthesia_orders", "exam_orders"):
                try:
                    cols = {c[1] for c in conn.execute(text(f"PRAGMA table_info({tbl})")).fetchall()}
                    if "voided_by" not in cols:
                        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN voided_by VARCHAR(80) DEFAULT ''"))
                    if "voided_at" not in cols:
                        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN voided_at DATETIME DEFAULT NULL"))
                    if "void_reason" not in cols:
                        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN void_reason VARCHAR(200) DEFAULT ''"))
                except Exception:
                    pass

            # 钱包"按比例扣本金/赠送"所需字段（idempotent）
            try:
                w_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(wallets)")).fetchall()}
                if "balance_principal" not in w_cols:
                    conn.execute(text("ALTER TABLE wallets ADD COLUMN balance_principal REAL DEFAULT 0"))
                if "balance_bonus" not in w_cols:
                    conn.execute(text("ALTER TABLE wallets ADD COLUMN balance_bonus REAL DEFAULT 0"))
                wt_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(wallet_transactions)")).fetchall()}
                if "consumed_principal" not in wt_cols:
                    conn.execute(text("ALTER TABLE wallet_transactions ADD COLUMN consumed_principal REAL DEFAULT 0"))
                if "consumed_bonus" not in wt_cols:
                    conn.execute(text("ALTER TABLE wallet_transactions ADD COLUMN consumed_bonus REAL DEFAULT 0"))
                # 一次性回填：还没拆过的钱包（principal=0 且 bonus=0 但 balance > 0）
                # 按该钱包历史 recharge 的本金 : 赠送 比例拆当前 balance
                rows = conn.execute(text(
                    "SELECT id, balance FROM wallets "
                    "WHERE balance > 0 AND (balance_principal IS NULL OR balance_principal = 0) "
                    "AND (balance_bonus IS NULL OR balance_bonus = 0)"
                )).fetchall()
                for wid, bal in rows:
                    tot = conn.execute(text(
                        "SELECT COALESCE(SUM(amount), 0), COALESCE(SUM(bonus_amount), 0) "
                        "FROM wallet_transactions WHERE wallet_id=:w AND type='recharge'"
                    ), {"w": wid}).fetchone()
                    p_recharge, b_recharge = float(tot[0] or 0), float(tot[1] or 0)
                    total_rec = p_recharge + b_recharge
                    if total_rec > 0:
                        # 按充值的本金:赠送 比例拆当前 balance
                        ratio_p = p_recharge / total_rec
                        bp = round(float(bal) * ratio_p, 2)
                        bb = round(float(bal) - bp, 2)
                    else:
                        # 没有 recharge 记录的旧数据（导入的），全部算本金
                        bp = float(bal)
                        bb = 0.0
                    conn.execute(text(
                        "UPDATE wallets SET balance_principal=:p, balance_bonus=:b WHERE id=:w"
                    ), {"p": bp, "b": bb, "w": wid})
            except Exception:
                pass

            # 一次性数据修复：手术完成 → 必然已现场确认（业务约束）
            # 历史上员工跳步骤导致 surgery_completed 但 staff_cat_verified=0 的记录
            try:
                conn.execute(text(
                    "UPDATE applications SET staff_cat_verified = 1 "
                    "WHERE status = 'surgery_completed' AND (staff_cat_verified IS NULL OR staff_cat_verified = 0)"
                ))
            except Exception:
                pass

            appointment_cols = conn.execute(text("PRAGMA table_info(appointments)")).fetchall()
            appointment_names = {c[1] for c in appointment_cols}
            if "wechat_openid" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN wechat_openid VARCHAR(64) DEFAULT ''"))
            if "pet_size" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN pet_size VARCHAR(40) DEFAULT NULL"))
            if "coat_length" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN coat_length VARCHAR(20) DEFAULT NULL"))
            if "addon_services" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN addon_services VARCHAR(200) DEFAULT NULL"))
            if "is_proxy" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN is_proxy BOOLEAN DEFAULT 0"))
            if "proxy_name" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN proxy_name VARCHAR(120) DEFAULT ''"))
            if "proxy_phone" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN proxy_phone VARCHAR(40) DEFAULT ''"))
            if "proxy_relation" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN proxy_relation VARCHAR(40) DEFAULT ''"))
            if "reminder_pushed_at" not in appointment_names:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN reminder_pushed_at DATETIME DEFAULT NULL"))

            # 性能：常用筛选字段加索引（存在则跳过）
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_created_at ON applications(created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_store ON applications(clinic_store)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_phone ON applications(phone)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_name ON applications(applicant_name)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_consent ON applications(showcase_consent)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_applications_verified ON applications(staff_cat_verified)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_media_application_kind ON media_files(application_id, kind)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_notify_application_time ON notification_logs(application_id, created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_application_time ON audit_logs(application_id, created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_appointments_category_status ON appointments(category, status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_appointments_store_date ON appointments(store, appointment_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_appointments_application ON appointments(related_application_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_appointments_created_at ON appointments(created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_appointments_wechat_openid ON appointments(wechat_openid)"))

            # staff 员工档案表
            staff_cols = conn.execute(text("PRAGMA table_info(staff)")).fetchall()
            if not staff_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS staff ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(80) NOT NULL, "
                    "gender VARCHAR(10) DEFAULT '', "
                    "birthday VARCHAR(20) DEFAULT '', "
                    "phone VARCHAR(40) DEFAULT '', "
                    "id_number VARCHAR(40) DEFAULT '', "
                    "store VARCHAR(80) DEFAULT '', "
                    "position VARCHAR(80) DEFAULT '', "
                    "hire_date VARCHAR(20) DEFAULT '', "
                    "probation_end_date VARCHAR(20) DEFAULT '', "
                    "status VARCHAR(20) DEFAULT 'active', "
                    "resign_date VARCHAR(20) DEFAULT '', "
                    "resign_reason TEXT DEFAULT '', "
                    "emergency_contact_name VARCHAR(80) DEFAULT '', "
                    "emergency_contact_phone VARCHAR(40) DEFAULT '', "
                    "emergency_contact_relation VARCHAR(40) DEFAULT '', "
                    "admin_user_id INTEGER REFERENCES admin_users(id) ON DELETE SET NULL, "
                    "notes TEXT DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))

            # contracts 合同管理表
            contract_cols = conn.execute(text("PRAGMA table_info(contracts)")).fetchall()
            if not contract_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS contracts ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE, "
                    "contract_type VARCHAR(20) DEFAULT 'formal', "
                    "start_date VARCHAR(20) DEFAULT '', "
                    "end_date VARCHAR(20) DEFAULT '', "
                    "file_path VARCHAR(512) DEFAULT '', "
                    "original_filename VARCHAR(255) DEFAULT '', "
                    "notes TEXT DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))

            # admin_users 表（多账号权限管理）
            admin_user_cols = conn.execute(text("PRAGMA table_info(admin_users)")).fetchall()
            if not admin_user_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS admin_users ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "username VARCHAR(80) UNIQUE NOT NULL, "
                    "password_hash VARCHAR(256) NOT NULL, "
                    "role VARCHAR(20) DEFAULT 'staff', "
                    "is_active BOOLEAN DEFAULT 1, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))

            feedback_cols = conn.execute(text("PRAGMA table_info(feedback)")).fetchall()
            if not feedback_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS feedback ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "openid VARCHAR(64) DEFAULT '', "
                    "content TEXT DEFAULT '', "
                    "status VARCHAR(20) DEFAULT 'pending', "
                    "admin_note TEXT DEFAULT '', "
                    "image_paths TEXT DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "resolved_at DATETIME DEFAULT NULL"
                    ")"
                ))

            # customers 客户档案表
            customer_cols = conn.execute(text("PRAGMA table_info(customers)")).fetchall()
            if not customer_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS customers ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(120) DEFAULT '', "
                    "phone VARCHAR(40) DEFAULT '', "
                    "wechat_openid VARCHAR(64) DEFAULT '', "
                    "id_number VARCHAR(40) DEFAULT '', "
                    "address VARCHAR(500) DEFAULT '', "
                    "source VARCHAR(40) DEFAULT '', "
                    "notes TEXT DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name)"))

            # customers: 补 phones_extra (备用手机号 CSV)
            try:
                _cust_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(customers)")).fetchall()}
                if "phones_extra" not in _cust_cols:
                    conn.execute(text("ALTER TABLE customers ADD COLUMN phones_extra VARCHAR(500) DEFAULT ''"))
                # 员工内购档案标记 + 关联员工
                if "is_internal" not in _cust_cols:
                    conn.execute(text("ALTER TABLE customers ADD COLUMN is_internal BOOLEAN DEFAULT 0"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_customers_is_internal ON customers(is_internal)"))
                if "internal_staff_id" not in _cust_cols:
                    conn.execute(text("ALTER TABLE customers ADD COLUMN internal_staff_id INTEGER DEFAULT NULL"))
            except Exception as _e:
                print(f"[migrations] customers.phones_extra/is_internal skipped: {_e}")

            # pets 宠物档案表
            pet_cols = conn.execute(text("PRAGMA table_info(pets)")).fetchall()
            if not pet_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS pets ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, "
                    "name VARCHAR(120) DEFAULT '', "
                    "species VARCHAR(40) DEFAULT 'cat', "
                    "breed VARCHAR(80) DEFAULT '', "
                    "gender VARCHAR(10) DEFAULT 'unknown', "
                    "birthday_estimate VARCHAR(40) DEFAULT '', "
                    "is_neutered BOOLEAN DEFAULT 0, "
                    "color_pattern VARCHAR(80) DEFAULT '', "
                    "is_stray BOOLEAN DEFAULT 0, "
                    "microchip_id VARCHAR(40) DEFAULT '', "
                    "notes TEXT DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pets_customer ON pets(customer_id)"))

            # 为 applications 和 appointments 补 customer_id / pet_id 列
            app_cols2 = conn.execute(text("PRAGMA table_info(applications)")).fetchall()
            app_names2 = {c[1] for c in app_cols2}
            if "customer_id" not in app_names2:
                conn.execute(text("ALTER TABLE applications ADD COLUMN customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL"))
            if "pet_id" not in app_names2:
                conn.execute(text("ALTER TABLE applications ADD COLUMN pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL"))

            appt_cols2 = conn.execute(text("PRAGMA table_info(appointments)")).fetchall()
            appt_names2 = {c[1] for c in appt_cols2}
            if "customer_id" not in appt_names2:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL"))
            if "pet_id" not in appt_names2:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL"))

            # prescriptions 处方单表
            prx_cols = conn.execute(text("PRAGMA table_info(prescriptions)")).fetchall()
            if not prx_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS prescriptions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                    "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                    "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                    "prescribed_date VARCHAR(20) DEFAULT '', "
                    "vet_name VARCHAR(80) DEFAULT '', "
                    "status VARCHAR(20) DEFAULT 'draft', "
                    "notes TEXT DEFAULT '', "
                    "created_by VARCHAR(80) DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_prescriptions_visit ON prescriptions(visit_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_prescriptions_customer ON prescriptions(customer_id)"))

            # prescriptions: add total_amount if missing
            prx_cols2 = conn.execute(text("PRAGMA table_info(prescriptions)")).fetchall()
            prx_names2 = {c[1] for c in prx_cols2}
            if "total_amount" not in prx_names2:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN total_amount REAL DEFAULT 0.0"))
            # voided fields if missing (older DBs)
            if "voided_by" not in prx_names2:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN voided_by VARCHAR(80) DEFAULT ''"))
            if "voided_at" not in prx_names2:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN voided_at DATETIME DEFAULT NULL"))
            if "void_reason" not in prx_names2:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN void_reason VARCHAR(200) DEFAULT ''"))
            # M2 助理已配齐
            if "dispensed_at" not in prx_names2:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN dispensed_at DATETIME DEFAULT NULL"))
            if "dispensed_by" not in prx_names2:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN dispensed_by VARCHAR(80) DEFAULT ''"))

            prx_item_cols = conn.execute(text("PRAGMA table_info(prescription_items)")).fetchall()
            if not prx_item_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS prescription_items ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "prescription_id INTEGER NOT NULL REFERENCES prescriptions(id) ON DELETE CASCADE, "
                    "item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL, "
                    "drug_name VARCHAR(120) DEFAULT '', "
                    "drug_type VARCHAR(40) DEFAULT 'oral', "
                    "dosage VARCHAR(80) DEFAULT '', "
                    "frequency VARCHAR(80) DEFAULT '', "
                    "duration_days VARCHAR(40) DEFAULT '', "
                    "quantity_num REAL DEFAULT 1.0, "
                    "quantity VARCHAR(40) DEFAULT '', "
                    "unit_price REAL DEFAULT 0.0, "
                    "subtotal REAL DEFAULT 0.0, "
                    "instructions TEXT DEFAULT ''"
                    ")"
                ))
            else:
                prx_item_names = {c[1] for c in prx_item_cols}
                if "item_id" not in prx_item_names:
                    conn.execute(text("ALTER TABLE prescription_items ADD COLUMN item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL"))
                if "quantity_num" not in prx_item_names:
                    conn.execute(text("ALTER TABLE prescription_items ADD COLUMN quantity_num REAL DEFAULT 1.0"))
                if "unit_price" not in prx_item_names:
                    conn.execute(text("ALTER TABLE prescription_items ADD COLUMN unit_price REAL DEFAULT 0.0"))
                if "subtotal" not in prx_item_names:
                    conn.execute(text("ALTER TABLE prescription_items ADD COLUMN subtotal REAL DEFAULT 0.0"))

            # sales_orders 销售单表
            so_cols = conn.execute(text("PRAGMA table_info(sales_orders)")).fetchall()
            if not so_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS sales_orders ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                    "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                    "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                    "order_date VARCHAR(20) DEFAULT '', "
                    "status VARCHAR(20) DEFAULT 'pending', "
                    "total_amount REAL DEFAULT 0, "
                    "payment_method VARCHAR(40) DEFAULT '', "
                    "notes TEXT DEFAULT '', "
                    "created_by VARCHAR(80) DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sales_orders_customer ON sales_orders(customer_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sales_orders_visit ON sales_orders(visit_id)"))

            soi_cols = conn.execute(text("PRAGMA table_info(sales_order_items)")).fetchall()
            if not soi_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS sales_order_items ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE, "
                    "item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL, "
                    "item_name VARCHAR(120) DEFAULT '', "
                    "item_type VARCHAR(40) DEFAULT 'product', "
                    "unit_price REAL DEFAULT 0, "
                    "quantity REAL DEFAULT 1, "
                    "subtotal REAL DEFAULT 0, "
                    "notes VARCHAR(200) DEFAULT ''"
                    ")"
                ))
            else:
                soi_names = {c[1] for c in soi_cols}
                if "item_id" not in soi_names:
                    conn.execute(text("ALTER TABLE sales_order_items ADD COLUMN item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL"))

            # visits 就诊病历表
            visit_cols = conn.execute(text("PRAGMA table_info(visits)")).fetchall()
            if not visit_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS visits ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                    "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                    "appointment_id INTEGER DEFAULT NULL REFERENCES appointments(id) ON DELETE SET NULL, "
                    "visit_date VARCHAR(20) DEFAULT '', "
                    "visit_type VARCHAR(40) DEFAULT 'outpatient', "
                    "chief_complaint TEXT DEFAULT '', "
                    "physical_exam TEXT DEFAULT '', "
                    "diagnosis TEXT DEFAULT '', "
                    "treatment_plan TEXT DEFAULT '', "
                    "notes TEXT DEFAULT '', "
                    "vet_name VARCHAR(80) DEFAULT '', "
                    "created_by VARCHAR(80) DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_visits_customer ON visits(customer_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_visits_pet ON visits(pet_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_visits_date ON visits(visit_date)"))

            # inventory_items 品目表
            inv_cols = conn.execute(text("PRAGMA table_info(inventory_items)")).fetchall()
            if not inv_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS inventory_items ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(200) NOT NULL, "
                    "category VARCHAR(60) DEFAULT 'medication', "
                    "subcategory VARCHAR(60) DEFAULT '', "
                    "is_service BOOLEAN DEFAULT 0, "
                    "is_controlled BOOLEAN DEFAULT 0, "
                    "unit VARCHAR(20) DEFAULT '个', "
                    "unit2 VARCHAR(20) DEFAULT '', "
                    "unit2_ratio REAL DEFAULT 1.0, "
                    "sell_price REAL DEFAULT 0.0, "
                    "cost_price REAL DEFAULT 0.0, "
                    "stock_qty REAL DEFAULT 0.0, "
                    "low_stock_min REAL DEFAULT 0.0, "
                    "supplier VARCHAR(200) DEFAULT '', "
                    "notes TEXT DEFAULT '', "
                    "is_active BOOLEAN DEFAULT 1, "
                    "created_by VARCHAR(80) DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_items_category ON inventory_items(category)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_items_name ON inventory_items(name)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_items_active ON inventory_items(is_active)"))

            # 迁移：把老的 category=medication+subcategory=vaccine/antiparasitic
            # 提升为 category=vaccine / antiparasitic 顶级（idempotent）
            try:
                conn.execute(text(
                    "UPDATE inventory_items SET category='vaccine', subcategory='other' "
                    "WHERE category='medication' AND subcategory='vaccine'"
                ))
                conn.execute(text(
                    "UPDATE inventory_items SET category='antiparasitic', subcategory='both' "
                    "WHERE category='medication' AND subcategory='antiparasitic'"
                ))
            except Exception:
                pass

            # inventory_transactions 出入库流水
            inv_tx_cols = conn.execute(text("PRAGMA table_info(inventory_transactions)")).fetchall()
            if not inv_tx_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS inventory_transactions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE, "
                    "tx_type VARCHAR(20) DEFAULT 'in', "
                    "qty REAL NOT NULL, "
                    "qty_before REAL DEFAULT 0.0, "
                    "qty_after REAL DEFAULT 0.0, "
                    "unit_price REAL DEFAULT 0.0, "
                    "ref_type VARCHAR(40) DEFAULT 'manual', "
                    "ref_id INTEGER DEFAULT NULL, "
                    "operator VARCHAR(80) DEFAULT '', "
                    "note VARCHAR(500) DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_tx_item ON inventory_transactions(item_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_tx_type ON inventory_transactions(tx_type)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_tx_created ON inventory_transactions(created_at)"))

            # inventory_items: 补 last_counted_at 列
            inv_item_cols = conn.execute(text("PRAGMA table_info(inventory_items)")).fetchall()
            inv_item_names = {c[1] for c in inv_item_cols}
            if inv_item_cols and "last_counted_at" not in inv_item_names:
                conn.execute(text("ALTER TABLE inventory_items ADD COLUMN last_counted_at DATETIME DEFAULT NULL"))
            # inventory_items: 多门店分离 — 空字符串 = 通用两店共享
            if inv_item_cols and "store" not in inv_item_names:
                conn.execute(text("ALTER TABLE inventory_items ADD COLUMN store VARCHAR(40) DEFAULT ''"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_items_store ON inventory_items(store)"))
            # inventory_items: 门店级价格覆盖（方案 H — JSON 字符串）
            if inv_item_cols and "store_overrides" not in inv_item_names:
                conn.execute(text("ALTER TABLE inventory_items ADD COLUMN store_overrides TEXT DEFAULT ''"))
            # inventory_items: 拍照入库识别别名（进货单上的标准名/厂家名）
            if inv_item_cols and "aliases" not in inv_item_names:
                conn.execute(text("ALTER TABLE inventory_items ADD COLUMN aliases TEXT DEFAULT ''"))
            # inventory_items: 是否需要出报告（检查项专用，纯收费项可设 False 避免工作台误报"未出报告"）
            if inv_item_cols and "requires_report" not in inv_item_names:
                conn.execute(text("ALTER TABLE inventory_items ADD COLUMN requires_report BOOLEAN DEFAULT 1"))
            # inventory_items: 整支/整瓶计费（玻璃瓶针剂等，开 0.1ml 与开 1ml 同价、同扣 1 整支）
            if inv_item_cols and "single_use_pack" not in inv_item_names:
                conn.execute(text("ALTER TABLE inventory_items ADD COLUMN single_use_pack BOOLEAN DEFAULT 0"))

            # stocktake_sessions 盘点会话表
            st_sess_cols = conn.execute(text("PRAGMA table_info(stocktake_sessions)")).fetchall()
            if not st_sess_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS stocktake_sessions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(120) DEFAULT '', "
                    "category_filter VARCHAR(60) DEFAULT '', "
                    "status VARCHAR(20) DEFAULT 'open', "
                    "operator VARCHAR(80) DEFAULT '', "
                    "note TEXT DEFAULT '', "
                    "item_count INTEGER DEFAULT 0, "
                    "variance_count INTEGER DEFAULT 0, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "completed_at DATETIME DEFAULT NULL"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_st_sess_status ON stocktake_sessions(status)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_st_sess_created ON stocktake_sessions(created_at)"))

            # stocktake_items 盘点明细表
            st_item_cols = conn.execute(text("PRAGMA table_info(stocktake_items)")).fetchall()
            if not st_item_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS stocktake_items ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "session_id INTEGER NOT NULL REFERENCES stocktake_sessions(id) ON DELETE CASCADE, "
                    "item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL, "
                    "item_name VARCHAR(200) DEFAULT '', "
                    "category VARCHAR(60) DEFAULT '', "
                    "unit VARCHAR(20) DEFAULT '', "
                    "system_qty REAL DEFAULT 0.0, "
                    "actual_qty REAL DEFAULT NULL, "
                    "variance REAL DEFAULT 0.0, "
                    "is_adjusted BOOLEAN DEFAULT 0, "
                    "notes VARCHAR(500) DEFAULT ''"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_st_items_session ON stocktake_items(session_id)"))

            # inventory_batches 库存批次表
            inv_batch_cols = conn.execute(text("PRAGMA table_info(inventory_batches)")).fetchall()
            if not inv_batch_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS inventory_batches ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE, "
                    "batch_no VARCHAR(80) DEFAULT '', "
                    "quantity REAL DEFAULT 0.0, "
                    "expiry_date VARCHAR(20) DEFAULT '', "
                    "received_date VARCHAR(20) DEFAULT '', "
                    "notes VARCHAR(500) DEFAULT '', "
                    "is_depleted BOOLEAN DEFAULT 0, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_batch_item ON inventory_batches(item_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_inv_batch_expiry ON inventory_batches(expiry_date)"))

            # rabies_vaccine_records 狂犬疫苗登记表
            rvr_cols = conn.execute(text("PRAGMA table_info(rabies_vaccine_records)")).fetchall()
            if not rvr_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS rabies_vaccine_records ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "cert_no VARCHAR(60) DEFAULT '', "
                    "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                    "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                    "owner_name VARCHAR(120) DEFAULT '', "
                    "owner_address VARCHAR(500) DEFAULT '', "
                    "owner_phone VARCHAR(40) DEFAULT '', "
                    "animal_name VARCHAR(80) DEFAULT '', "
                    "animal_breed VARCHAR(80) DEFAULT '', "
                    "animal_dob VARCHAR(40) DEFAULT '', "
                    "animal_gender VARCHAR(10) DEFAULT '', "
                    "animal_color VARCHAR(80) DEFAULT '', "
                    "owner_signature_path VARCHAR(512) DEFAULT '', "
                    "owner_signed_at DATETIME DEFAULT NULL, "
                    "vaccine_manufacturer VARCHAR(120) DEFAULT '', "
                    "vaccine_batch_no VARCHAR(80) DEFAULT '', "
                    "vaccine_date VARCHAR(20) DEFAULT '', "
                    "staff_name VARCHAR(80) DEFAULT '', "
                    "staff_signature_path VARCHAR(512) DEFAULT '', "
                    "staff_signed_at DATETIME DEFAULT NULL, "
                    "status VARCHAR(20) DEFAULT 'owner_pending', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rvr_phone ON rabies_vaccine_records(owner_phone)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rvr_status ON rabies_vaccine_records(status)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rvr_created ON rabies_vaccine_records(created_at)"))
            else:
                rvr_names = {c[1] for c in rvr_cols}
                if "clinic_store" not in rvr_names:
                    conn.execute(text("ALTER TABLE rabies_vaccine_records ADD COLUMN clinic_store VARCHAR(60) DEFAULT '横岗店'"))

            # adoption_pets 待领养动物表
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS adoption_pets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(80) DEFAULT '', "
                "species VARCHAR(40) DEFAULT 'cat', "
                "breed VARCHAR(80) DEFAULT '', "
                "age_estimate VARCHAR(40) DEFAULT '', "
                "gender VARCHAR(20) DEFAULT 'unknown', "
                "personality TEXT DEFAULT '', "
                "health_note TEXT DEFAULT '', "
                "requirements TEXT DEFAULT '', "
                "image1_path VARCHAR(512) DEFAULT '', "
                "image2_path VARCHAR(512) DEFAULT '', "
                "video_path VARCHAR(512) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'available', "
                "adoption_agreement_path VARCHAR(512) DEFAULT '', "
                "sort_order INTEGER DEFAULT 0, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))

            # invoices 收费单
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS invoices ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "invoice_no VARCHAR(40) DEFAULT '', "
                "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, "
                "visit_id INTEGER REFERENCES visits(id) ON DELETE SET NULL, "
                "pet_id INTEGER REFERENCES pets(id) ON DELETE SET NULL, "
                "invoice_date VARCHAR(20) DEFAULT '', "
                "subtotal REAL DEFAULT 0.0, "
                "discount_amount REAL DEFAULT 0.0, "
                "total_amount REAL DEFAULT 0.0, "
                "payment_status VARCHAR(20) DEFAULT 'unpaid', "
                "payment_method VARCHAR(40) DEFAULT '', "
                "paid_at DATETIME, "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS invoice_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE, "
                "ref_type VARCHAR(40) DEFAULT 'manual', "
                "ref_id INTEGER, "
                "description VARCHAR(300) DEFAULT '', "
                "quantity REAL DEFAULT 1.0, "
                "unit_price REAL DEFAULT 0.0, "
                "subtotal REAL DEFAULT 0.0"
                ")"
            ))

            # invoices: 补 store 列 + 索引；历史数据通过 visit.clinic_store / pet.store 回填
            # 独立 try：即使外层早期迁移有冷不丁的失败也要把这步跑掉
            try:
                inv_cols = conn.execute(text("PRAGMA table_info(invoices)")).fetchall()
                inv_col_names = {c[1] for c in inv_cols} if inv_cols else set()
                if inv_cols and "store" not in inv_col_names:
                    conn.execute(text("ALTER TABLE invoices ADD COLUMN store VARCHAR(40) DEFAULT ''"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_invoices_store ON invoices(store)"))
                # 不管列是新加的还是早就存在的，凡是空 store 都按 pet.store 回填
                # （visits 表没有 clinic_store 列，store 信息只有 Pet 有）
                conn.execute(text(
                    "UPDATE invoices SET store = ("
                    "  SELECT store FROM pets WHERE pets.id = invoices.pet_id"
                    ") "
                    "WHERE (store IS NULL OR store = '') AND pet_id IS NOT NULL"
                ))
                conn.execute(text(
                    "UPDATE invoices SET store = ("
                    "  SELECT p.store FROM visits v JOIN pets p ON p.id = v.pet_id "
                    "  WHERE v.id = invoices.visit_id"
                    ") "
                    "WHERE (store IS NULL OR store = '') AND visit_id IS NOT NULL"
                ))
                conn.commit()
            except Exception as _e:
                print(f"[migrations] invoices.store skipped: {_e}")

            # vaccinations 疫苗接种记录
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS vaccinations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "pet_id INTEGER REFERENCES pets(id) ON DELETE SET NULL, "
                "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, "
                "vaccine_type VARCHAR(40) DEFAULT 'other', "
                "vaccine_name VARCHAR(120) DEFAULT '', "
                "batch_no VARCHAR(80) DEFAULT '', "
                "dose_number INTEGER DEFAULT 1, "
                "vaccinated_date VARCHAR(20) DEFAULT '', "
                "next_due_date VARCHAR(20) DEFAULT '', "
                "inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL, "
                "is_free INTEGER DEFAULT 0, "
                "rabies_record_id INTEGER REFERENCES rabies_vaccine_records(id) ON DELETE SET NULL, "
                "invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL, "
                "vet_name VARCHAR(80) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vacc_pet ON vaccinations(pet_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vacc_due ON vaccinations(next_due_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vacc_rabies ON vaccinations(rabies_record_id)"))

            # exam_orders 检查单
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS exam_orders ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE, "
                "items_json TEXT DEFAULT '[]', "
                "notes TEXT DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'pending', "
                "upload_token VARCHAR(80) UNIQUE DEFAULT '', "
                "token_expires_at DATETIME DEFAULT NULL, "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exam_orders_visit ON exam_orders(visit_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exam_orders_token ON exam_orders(upload_token)"))

            # exam_reports 检查报告
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS exam_reports ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "exam_order_id INTEGER NOT NULL REFERENCES exam_orders(id) ON DELETE CASCADE, "
                "file_path VARCHAR(500) DEFAULT '', "
                "original_name VARCHAR(200) DEFAULT '', "
                "file_type VARCHAR(10) DEFAULT 'image', "
                "item_label VARCHAR(120) DEFAULT '', "
                "uploaded_by VARCHAR(80) DEFAULT '', "
                "uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exam_reports_order ON exam_reports(exam_order_id)"))

            # microscopy_reports：显微镜检查报告（皮肤/耳道/粪检 等手工出报告）
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS microscopy_reports ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "exam_order_id INTEGER NOT NULL REFERENCES exam_orders(id) ON DELETE CASCADE, "
                "exam_report_id INTEGER REFERENCES exam_reports(id) ON DELETE SET NULL, "
                "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER REFERENCES visits(id) ON DELETE SET NULL, "
                "item_label VARCHAR(120) DEFAULT '', "
                "vet_name VARCHAR(80) DEFAULT '', "
                "magnification VARCHAR(20) DEFAULT '', "
                "sample_site VARCHAR(120) DEFAULT '', "
                "findings_json TEXT DEFAULT '[]', "
                "narrative TEXT DEFAULT '', "
                "conclusion TEXT DEFAULT '', "
                "advice TEXT DEFAULT '', "
                "photos_json TEXT DEFAULT '[]', "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_micro_order ON microscopy_reports(exam_order_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_micro_store ON microscopy_reports(store)"))
            # 补 template_type 列（早期建表的旧库）
            mr_cols = conn.execute(text("PRAGMA table_info(microscopy_reports)")).fetchall()
            mr_col_names = {c[1] for c in mr_cols} if mr_cols else set()
            if mr_cols and "template_type" not in mr_col_names:
                conn.execute(text("ALTER TABLE microscopy_reports ADD COLUMN template_type VARCHAR(20) DEFAULT 'general'"))

            # ultrasound_reports：B超 / 超声检查报告（心超/腹部/泌尿 等，测量字段动态）
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS ultrasound_reports ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "exam_order_id INTEGER NOT NULL REFERENCES exam_orders(id) ON DELETE CASCADE, "
                "exam_report_id INTEGER REFERENCES exam_reports(id) ON DELETE SET NULL, "
                "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER REFERENCES visits(id) ON DELETE SET NULL, "
                "item_label VARCHAR(120) DEFAULT '', "
                "exam_type VARCHAR(20) DEFAULT 'cardiac', "
                "device VARCHAR(120) DEFAULT '', "
                "vet_name VARCHAR(80) DEFAULT '', "
                "measurements_json TEXT DEFAULT '[]', "
                "raw_pdf_path VARCHAR(500) DEFAULT '', "
                "vet_findings TEXT DEFAULT '', "
                "findings TEXT DEFAULT '', "
                "conclusion TEXT DEFAULT '', "
                "advice TEXT DEFAULT '', "
                "photos_json TEXT DEFAULT '[]', "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_us_order ON ultrasound_reports(exam_order_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_us_store ON ultrasound_reports(store)"))

            # xray_reports：X光/放射报告（胸/腹/肌骨/关节，医生读片 + AI 帮写）
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS xray_reports ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "exam_order_id INTEGER NOT NULL REFERENCES exam_orders(id) ON DELETE CASCADE, "
                "exam_report_id INTEGER REFERENCES exam_reports(id) ON DELETE SET NULL, "
                "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER REFERENCES visits(id) ON DELETE SET NULL, "
                "item_label VARCHAR(120) DEFAULT '', "
                "region VARCHAR(20) DEFAULT 'thorax', "
                "projection VARCHAR(120) DEFAULT '', "
                "image_quality VARCHAR(40) DEFAULT '', "
                "vet_name VARCHAR(80) DEFAULT '', "
                "findings_json TEXT DEFAULT '[]', "
                "measurements_json TEXT DEFAULT '[]', "
                "vet_findings TEXT DEFAULT '', "
                "findings TEXT DEFAULT '', "
                "conclusion TEXT DEFAULT '', "
                "advice TEXT DEFAULT '', "
                "photos_json TEXT DEFAULT '[]', "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_xray_order ON xray_reports(exam_order_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_xray_store ON xray_reports(store)"))

            # 处方 / 检查单：打包价（整单议价总额）列
            pr_cols = conn.execute(text("PRAGMA table_info(prescriptions)")).fetchall()
            if pr_cols and "package_price" not in {c[1] for c in pr_cols}:
                conn.execute(text("ALTER TABLE prescriptions ADD COLUMN package_price FLOAT DEFAULT 0.0"))
            eo_cols = conn.execute(text("PRAGMA table_info(exam_orders)")).fetchall()
            if eo_cols and "package_price" not in {c[1] for c in eo_cols}:
                conn.execute(text("ALTER TABLE exam_orders ADD COLUMN package_price FLOAT DEFAULT 0.0"))

            # vaccinations: 补 reminder_sent_at 列
            vacc_cols = conn.execute(text("PRAGMA table_info(vaccinations)")).fetchall()
            if vacc_cols:
                vacc_col_names = {c[1] for c in vacc_cols}
                if "reminder_sent_at" not in vacc_col_names:
                    conn.execute(text("ALTER TABLE vaccinations ADD COLUMN reminder_sent_at DATETIME DEFAULT NULL"))

            # admin_users: 补 store 列
            au_cols = conn.execute(text("PRAGMA table_info(admin_users)")).fetchall()
            if au_cols:
                au_names = {c[1] for c in au_cols}
                if "store" not in au_names:
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN store VARCHAR(40) DEFAULT ''"))
                if "display_name" not in au_names:
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN display_name VARCHAR(80) DEFAULT ''"))
                # 企业微信单点登录：员工绑定的企业微信 userid
                if "wecom_userid" not in au_names:
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN wecom_userid VARCHAR(80) DEFAULT ''"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admin_users_wecom_userid ON admin_users(wecom_userid)"))
                # 企微通知偏好（CSV 存 disabled 的事件 key）
                if "wecom_notify_disabled" not in au_names:
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN wecom_notify_disabled VARCHAR(500) DEFAULT ''"))
                # M1 手机端身份：auto / doctor / nurse / groomer
                if "mobile_role" not in au_names:
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN mobile_role VARCHAR(20) DEFAULT 'auto'"))

            # wecom_customer_links: 企微外部联系人 ↔ Customer 映射表（Phase 3）
            wcl_cols = conn.execute(text("PRAGMA table_info(wecom_customer_links)")).fetchall()
            if not wcl_cols:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS wecom_customer_links ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "external_userid VARCHAR(120) UNIQUE NOT NULL, "
                    "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, "
                    "follow_userid VARCHAR(80) DEFAULT '', "
                    "remark_name VARCHAR(120) DEFAULT '', "
                    "remark_mobile VARCHAR(40) DEFAULT '', "
                    "unionid VARCHAR(80) DEFAULT '', "
                    "name VARCHAR(120) DEFAULT '', "
                    "avatar VARCHAR(500) DEFAULT '', "
                    "sync_status VARCHAR(20) DEFAULT 'unmatched', "
                    "raw_json TEXT DEFAULT '', "
                    "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_wcl_external_userid ON wecom_customer_links(external_userid)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_wcl_follow_userid ON wecom_customer_links(follow_userid)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_wcl_remark_mobile ON wecom_customer_links(remark_mobile)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_wcl_sync_status ON wecom_customer_links(sync_status)"))

            # tnr_store_configs TNR 门店配额配置表
            # pets 新增字段：store / medical_record_no / life_status
            pet_cols2 = conn.execute(text("PRAGMA table_info(pets)")).fetchall()
            if pet_cols2:
                pet_names_v2 = {c[1] for c in pet_cols2}
                if "store" not in pet_names_v2:
                    conn.execute(text("ALTER TABLE pets ADD COLUMN store VARCHAR(40) DEFAULT ''"))
                if "medical_record_no" not in pet_names_v2:
                    conn.execute(text("ALTER TABLE pets ADD COLUMN medical_record_no VARCHAR(40) DEFAULT ''"))
                if "life_status" not in pet_names_v2:
                    conn.execute(text("ALTER TABLE pets ADD COLUMN life_status VARCHAR(20) DEFAULT 'alive'"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pets_mrn ON pets(medical_record_no)"))

            # visits 新增字段：follow_up_note / follow_up_at
            vst_cols2 = conn.execute(text("PRAGMA table_info(visits)")).fetchall()
            if vst_cols2:
                vst_names_v2 = {c[1] for c in vst_cols2}
                if "follow_up_note" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN follow_up_note TEXT DEFAULT ''"))
                if "follow_up_at" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN follow_up_at VARCHAR(20) DEFAULT ''"))
                # 病历结束（合规：closed 后不可改、不可重开）
                if "status" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN status VARCHAR(20) DEFAULT 'open'"))
                if "closed_at" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN closed_at DATETIME DEFAULT NULL"))
                if "closed_by" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN closed_by VARCHAR(80) DEFAULT ''"))
                # 关闭回访（主人带回家自治不需要医院回访）
                if "followup_disabled" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN followup_disabled BOOLEAN DEFAULT 0"))
                # 操作门店（这次就诊在哪做的，短名）→ 账单/收据/营收按它走，跨店就诊不再跟宠物归属店
                if "store" not in vst_names_v2:
                    conn.execute(text("ALTER TABLE visits ADD COLUMN store VARCHAR(40) DEFAULT ''"))

            # deworming_records 驱虫记录
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS deworming_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "deworm_date VARCHAR(20) DEFAULT '', "
                "deworm_type VARCHAR(40) DEFAULT 'external', "
                "product_name VARCHAR(120) DEFAULT '', "
                "weight_kg REAL DEFAULT 0.0, "
                "dose VARCHAR(80) DEFAULT '', "
                "next_due_date VARCHAR(20) DEFAULT '', "
                "vet_name VARCHAR(80) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dewor_pet ON deworming_records(pet_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dewor_date ON deworming_records(deworm_date)"))

            # cages 笼位（住院模块基础）
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS cages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "store VARCHAR(40) DEFAULT '', "
                "code VARCHAR(40) DEFAULT '', "
                "kind VARCHAR(20) DEFAULT 'general', "
                "daily_rate REAL DEFAULT 0.0, "
                "sort_order INTEGER DEFAULT 0, "
                "notes TEXT DEFAULT '', "
                "is_active INTEGER DEFAULT 1, "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cages_store ON cages(store)"))

            # hospitalizations 住院档案
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS hospitalizations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "cage_id INTEGER DEFAULT NULL REFERENCES cages(id) ON DELETE SET NULL, "
                "invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "store VARCHAR(40) DEFAULT '', "
                "reason TEXT DEFAULT '', "
                "admitted_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "expected_discharge_date VARCHAR(20) DEFAULT '', "
                "discharged_at DATETIME DEFAULT NULL, "
                "discharge_summary TEXT DEFAULT '', "
                "daily_rate_override REAL DEFAULT 0.0, "
                "status VARCHAR(20) DEFAULT 'admitted', "
                "staff_token VARCHAR(40) DEFAULT '', "
                "owner_token VARCHAR(40) DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "closed_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hosp_pet ON hospitalizations(pet_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hosp_status ON hospitalizations(status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hosp_store ON hospitalizations(store)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_hosp_staff_token ON hospitalizations(staff_token) WHERE staff_token != ''"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_hosp_owner_token ON hospitalizations(owner_token) WHERE owner_token != ''"))

            # medication_admin_logs 住院发药打勾
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS medication_admin_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hospitalization_id INTEGER NOT NULL REFERENCES hospitalizations(id) ON DELETE CASCADE, "
                "prescription_id INTEGER NOT NULL REFERENCES prescriptions(id) ON DELETE CASCADE, "
                "prescription_item_id INTEGER NOT NULL REFERENCES prescription_items(id) ON DELETE CASCADE, "
                "scheduled_at DATETIME NOT NULL, "
                "day_index INTEGER DEFAULT 1, "
                "dose_index INTEGER DEFAULT 1, "
                "status VARCHAR(20) DEFAULT 'pending', "
                "administered_at DATETIME DEFAULT NULL, "
                "administered_by VARCHAR(80) DEFAULT '', "
                "dose_actual VARCHAR(80) DEFAULT '', "
                "notes VARCHAR(300) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_med_log_hosp ON medication_admin_logs(hospitalization_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_med_log_sched ON medication_admin_logs(scheduled_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_med_log_status ON medication_admin_logs(status)"))
            # D8：漏药推送标记
            ml_cols = conn.execute(text("PRAGMA table_info(medication_admin_logs)")).fetchall()
            if ml_cols and "reminder_sent_at" not in {c[1] for c in ml_cols}:
                conn.execute(text("ALTER TABLE medication_admin_logs ADD COLUMN reminder_sent_at DATETIME DEFAULT NULL"))
            # 一次性清理：孤儿用药日志（关联的处方 / 处方明细已不存在）
            # SQLite FK CASCADE 默认关，早期版本删处方时未显式删 log，留下垃圾
            try:
                conn.execute(text(
                    "DELETE FROM medication_admin_logs WHERE "
                    "prescription_id NOT IN (SELECT id FROM prescriptions) "
                    "OR prescription_item_id NOT IN (SELECT id FROM prescription_items)"
                ))
            except Exception:
                pass

            # vital_signs_logs 生命体征
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS vital_signs_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hospitalization_id INTEGER NOT NULL REFERENCES hospitalizations(id) ON DELETE CASCADE, "
                "recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "recorded_by VARCHAR(80) DEFAULT '', "
                "temperature_c REAL DEFAULT 0.0, "
                "hr INTEGER DEFAULT 0, "
                "rr INTEGER DEFAULT 0, "
                "mm_color VARCHAR(20) DEFAULT '', "
                "crt_sec REAL DEFAULT 0.0, "
                "weight_kg REAL DEFAULT 0.0, "
                "notes VARCHAR(300) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vital_hosp ON vital_signs_logs(hospitalization_id, recorded_at)"))

            # io_logs 输液/输出记录
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS io_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hospitalization_id INTEGER NOT NULL REFERENCES hospitalizations(id) ON DELETE CASCADE, "
                "recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "recorded_by VARCHAR(80) DEFAULT '', "
                "direction VARCHAR(10) DEFAULT 'in', "
                "category VARCHAR(20) DEFAULT 'other', "
                "amount_ml REAL DEFAULT 0.0, "
                "notes VARCHAR(300) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_io_hosp ON io_logs(hospitalization_id, recorded_at)"))

            # feeding_logs 进食记录
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS feeding_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hospitalization_id INTEGER NOT NULL REFERENCES hospitalizations(id) ON DELETE CASCADE, "
                "recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "recorded_by VARCHAR(80) DEFAULT '', "
                "food_type VARCHAR(120) DEFAULT '', "
                "offered_g REAL DEFAULT 0.0, "
                "eaten_g REAL DEFAULT 0.0, "
                "appetite_score INTEGER DEFAULT 3, "
                "notes VARCHAR(300) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_feed_hosp ON feeding_logs(hospitalization_id, recorded_at)"))

            # handover_notes 交班一句话
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS handover_notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hospitalization_id INTEGER NOT NULL REFERENCES hospitalizations(id) ON DELETE CASCADE, "
                "shift VARCHAR(20) DEFAULT 'morning', "
                "content TEXT DEFAULT '', "
                "recorded_by VARCHAR(80) DEFAULT '', "
                "recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_handover_hosp ON handover_notes(hospitalization_id, recorded_at)"))

            # weight_records 体重记录
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS weight_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "pet_id INTEGER NOT NULL REFERENCES pets(id) ON DELETE CASCADE, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "record_date VARCHAR(20) DEFAULT '', "
                "weight_kg REAL DEFAULT 0.0, "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_weight_pet ON weight_records(pet_id, record_date)"))

            # medical_documents 医疗文书（同意书/协议/报告）
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS medical_documents ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "doc_type VARCHAR(40) DEFAULT 'consent', "
                "title VARCHAR(200) DEFAULT '', "
                "file_path VARCHAR(500) DEFAULT '', "
                "original_name VARCHAR(200) DEFAULT '', "
                "file_type VARCHAR(10) DEFAULT 'pdf', "
                "file_size INTEGER DEFAULT 0, "
                "notes TEXT DEFAULT '', "
                "uploaded_by VARCHAR(80) DEFAULT '', "
                "uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_meddoc_pet ON medical_documents(pet_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_meddoc_visit ON medical_documents(visit_id)"))

            # prescription_items 细化字段（与库存单位关联）
            pri_cols = conn.execute(text("PRAGMA table_info(prescription_items)")).fetchall()
            if pri_cols:
                pri_names = {c[1] for c in pri_cols}
                for col, typ in [
                    ("dose_amount", "REAL DEFAULT 0.0"),
                    ("dose_unit", "VARCHAR(20) DEFAULT ''"),
                    ("times_per_day", "REAL DEFAULT 0.0"),
                    ("item_unit", "VARCHAR(20) DEFAULT ''"),
                    ("print_note", "TEXT DEFAULT ''"),
                    ("schedule_times", "VARCHAR(200) DEFAULT ''"),
                ]:
                    if col not in pri_names:
                        conn.execute(text(f"ALTER TABLE prescription_items ADD COLUMN {col} {typ}"))

            # prescription_templates 处方套餐模板
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS prescription_templates ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(120) DEFAULT '', "
                "category VARCHAR(40) DEFAULT '', "
                "items_json TEXT DEFAULT '[]', "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "use_count INTEGER DEFAULT 0"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_presc_tmpl_name ON prescription_templates(name)"))

            # exam_templates 检查单套餐模板（猫三联 / 犬血液生化 等常用组合）
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS exam_templates ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(120) DEFAULT '', "
                "category VARCHAR(40) DEFAULT '', "
                "items_json TEXT DEFAULT '[]', "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "use_count INTEGER DEFAULT 0"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exam_tmpl_name ON exam_templates(name)"))

            # calendar_blocks 全天封锁日程
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS calendar_blocks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title VARCHAR(200) DEFAULT '', "
                "block_date VARCHAR(20) DEFAULT '', "
                "store VARCHAR(40) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cal_blocks_date ON calendar_blocks(block_date)"))
            # calendar_blocks: 业务线封锁（beauty=美容线 / medical=医疗线 / all=全部）
            cb_cols = conn.execute(text("PRAGMA table_info(calendar_blocks)")).fetchall()
            if cb_cols and "track" not in {c[1] for c in cb_cols}:
                conn.execute(text("ALTER TABLE calendar_blocks ADD COLUMN track VARCHAR(20) DEFAULT 'all'"))

            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS tnr_store_configs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "store_name VARCHAR(120) UNIQUE NOT NULL, "
                "tnr_monthly_quota INTEGER DEFAULT 30, "
                "tnr_accepting BOOLEAN DEFAULT 1, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_by VARCHAR(80) DEFAULT ''"
                ")"
            ))

            # ── 钱包系统 ───────────────────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS wallets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_id INTEGER NOT NULL UNIQUE REFERENCES customers(id) ON DELETE CASCADE, "
                "balance REAL DEFAULT 0.0, "
                "lifetime_recharge REAL DEFAULT 0.0, "
                "lifetime_consume REAL DEFAULT 0.0, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS wallet_transactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "type VARCHAR(20) DEFAULT 'consume', "
                "amount REAL DEFAULT 0.0, "
                "balance_after REAL DEFAULT 0.0, "
                "pay_method VARCHAR(40) DEFAULT '', "
                "invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "bonus_amount REAL DEFAULT 0.0, "
                "store VARCHAR(40) DEFAULT '', "
                "note TEXT DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wtx_wallet ON wallet_transactions(wallet_id, created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wtx_customer ON wallet_transactions(customer_id, created_at)"))

            # ── 套餐 ────────────────────────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS package_products ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(120) DEFAULT '', "
                "category VARCHAR(40) DEFAULT 'beauty', "
                "total_uses INTEGER DEFAULT 10, "
                "sell_price REAL DEFAULT 0.0, "
                "unit_price REAL DEFAULT 0.0, "
                "validity_days INTEGER DEFAULT 365, "
                "is_active BOOLEAN DEFAULT 1, "
                "notes TEXT DEFAULT '', "
                "store VARCHAR(40) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            # 已存在的 package_products 表补 store 字段（多门店分离）
            _pp_cols = conn.execute(text("PRAGMA table_info(package_products)")).fetchall()
            _pp_names = {c[1] for c in _pp_cols}
            if _pp_cols and "store" not in _pp_names:
                conn.execute(text("ALTER TABLE package_products ADD COLUMN store VARCHAR(40) DEFAULT ''"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pkg_prod_store ON package_products(store)"))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS customer_packages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "product_id INTEGER DEFAULT NULL REFERENCES package_products(id) ON DELETE SET NULL, "
                "name VARCHAR(120) DEFAULT '', "
                "category VARCHAR(40) DEFAULT '', "
                "total_uses INTEGER DEFAULT 10, "
                "used_count INTEGER DEFAULT 0, "
                "sell_price REAL DEFAULT 0.0, "
                "unit_price REAL DEFAULT 0.0, "
                "purchase_date VARCHAR(20) DEFAULT '', "
                "expires_at VARCHAR(20) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'active', "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "note TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cpkg_customer ON customer_packages(customer_id, status)"))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS package_redemptions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_package_id INTEGER NOT NULL REFERENCES customer_packages(id) ON DELETE CASCADE, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "used_count INTEGER DEFAULT 1, "
                "remaining_after INTEGER DEFAULT 0, "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "note TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_predeem_cpkg ON package_redemptions(customer_package_id)"))

            # ── 协议签署系统 ──────────────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS consent_templates ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(120) DEFAULT '', "
                "category VARCHAR(40) DEFAULT 'general', "
                "body_html TEXT DEFAULT '', "
                "is_active BOOLEAN DEFAULT 1, "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS consent_tasks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "template_id INTEGER DEFAULT NULL REFERENCES consent_templates(id) ON DELETE SET NULL, "
                "customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "title VARCHAR(120) DEFAULT '', "
                "snapshot_html TEXT DEFAULT '', "
                "token VARCHAR(40) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'pending', "
                "signature_path VARCHAR(500) DEFAULT '', "
                "signed_at DATETIME DEFAULT NULL, "
                "signed_ip VARCHAR(60) DEFAULT '', "
                "expires_at VARCHAR(20) DEFAULT '', "
                "store VARCHAR(40) DEFAULT '', "
                "initiated_by VARCHAR(80) DEFAULT '', "
                "initiated_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "notes TEXT DEFAULT ''"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_consent_task_token ON consent_tasks(token)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_consent_task_customer ON consent_tasks(customer_id, status)"))

            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS consent_documents ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "task_id INTEGER NOT NULL UNIQUE REFERENCES consent_tasks(id) ON DELETE CASCADE, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "pdf_path VARCHAR(500) DEFAULT '', "
                "pdf_size INTEGER DEFAULT 0, "
                "title VARCHAR(120) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))

            # ── 协议签署审计日志（仅追加，作为打官司证据链） ───
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS consent_audit_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "task_id INTEGER NOT NULL REFERENCES consent_tasks(id) ON DELETE CASCADE, "
                "event VARCHAR(40) DEFAULT '', "
                "ip VARCHAR(60) DEFAULT '', "
                "user_agent VARCHAR(500) DEFAULT '', "
                "phone_masked VARCHAR(20) DEFAULT '', "
                "doc_sha256 VARCHAR(64) DEFAULT '', "
                "sig_sha256 VARCHAR(64) DEFAULT '', "
                "session_hash VARCHAR(64) DEFAULT '', "
                "note TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_consent_audit_task ON consent_audit_logs(task_id, created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_consent_audit_event ON consent_audit_logs(event)"))

            # ── 收款明细（混合支付） ───────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS payments ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "method VARCHAR(20) DEFAULT 'cash', "
                "amount REAL DEFAULT 0.0, "
                "ref_id INTEGER DEFAULT NULL, "
                "ref_no VARCHAR(120) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'success', "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "note TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pay_invoice ON payments(invoice_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pay_method_time ON payments(method, created_at)"))
            # 合并结算批次号
            _pay_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(payments)")).fetchall()}
            if "batch_no" not in _pay_cols:
                conn.execute(text("ALTER TABLE payments ADD COLUMN batch_no VARCHAR(40) DEFAULT NULL"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pay_batch ON payments(batch_no)"))
                # 历史回填：同客户 / 同支付方式 / 同一秒内、且覆盖 ≥2 张不同发票的成功收款
                # = 一次合并结算 → 赋同一 batch_no（"H" + 该组最小 payment id）
                _groups = conn.execute(text(
                    "SELECT customer_id, method, strftime('%Y-%m-%d %H:%M:%S', created_at) AS sec, "
                    "       MIN(id) AS min_id, COUNT(DISTINCT invoice_id) AS n_inv "
                    "FROM payments "
                    "WHERE status='success' AND customer_id IS NOT NULL "
                    "GROUP BY customer_id, method, sec "
                    "HAVING n_inv >= 2"
                )).fetchall()
                for g in _groups:
                    cid, method, sec, min_id, _n = g
                    conn.execute(text(
                        "UPDATE payments SET batch_no = :bn "
                        "WHERE status='success' AND customer_id = :cid AND method = :m "
                        "AND strftime('%Y-%m-%d %H:%M:%S', created_at) = :sec AND batch_no IS NULL"
                    ), {"bn": f"H{min_id}", "cid": cid, "m": method, "sec": sec})

            # 回填收款门店：历史上「超管收款」会写空 store（_get_admin_store 超管返回空），
            # 导致「按门店」收款统计漏单。补成对应发票的门店。自限：无空 store 时跳过。
            _has_blank_store = conn.execute(text(
                "SELECT 1 FROM payments WHERE store IS NULL OR store='' LIMIT 1"
            )).first()
            if _has_blank_store:
                conn.execute(text(
                    "UPDATE payments SET store = ("
                    "  SELECT i.store FROM invoices i WHERE i.id = payments.invoice_id"
                    ") "
                    "WHERE (store IS NULL OR store='') "
                    "AND EXISTS (SELECT 1 FROM invoices i WHERE i.id = payments.invoice_id "
                    "            AND i.store IS NOT NULL AND i.store != '')"
                ))

            # ── 优惠券 ────────────────────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS coupons ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "code VARCHAR(40) NOT NULL UNIQUE, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "title VARCHAR(120) DEFAULT '', "
                "kind VARCHAR(20) DEFAULT 'cash', "
                "face_value REAL DEFAULT 0.0, "
                "discount_pct REAL DEFAULT 0.0, "
                "min_amount REAL DEFAULT 0.0, "
                "expires_at VARCHAR(20) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'issued', "
                "used_invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "used_amount REAL DEFAULT 0.0, "
                "used_at DATETIME DEFAULT NULL, "
                "issued_by VARCHAR(80) DEFAULT '', "
                "issued_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "notes TEXT DEFAULT '', "
                "store VARCHAR(40) DEFAULT ''"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coupon_customer_status ON coupons(customer_id, status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_coupon_code ON coupons(code)"))

            # ── 押金 ────────────────────────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS deposits ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "appointment_id INTEGER DEFAULT NULL REFERENCES appointments(id) ON DELETE SET NULL, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "category VARCHAR(40) DEFAULT 'surgery', "
                "amount REAL DEFAULT 0.0, "
                "pay_method VARCHAR(40) DEFAULT 'cash', "
                "status VARCHAR(20) DEFAULT 'held', "
                "applied_invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "applied_amount REAL DEFAULT 0.0, "
                "refunded_amount REAL DEFAULT 0.0, "
                "refunded_at DATETIME DEFAULT NULL, "
                "store VARCHAR(40) DEFAULT '', "
                "operator VARCHAR(80) DEFAULT '', "
                "note TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dep_status ON deposits(status, customer_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dep_appt ON deposits(appointment_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dep_visit ON deposits(visit_id)"))

            # follow_ups 回访任务
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS follow_ups ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "visit_id INTEGER NOT NULL UNIQUE REFERENCES visits(id) ON DELETE CASCADE, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "store VARCHAR(40) DEFAULT '', "
                "assigned_to VARCHAR(80) DEFAULT '', "
                "planned_date VARCHAR(20) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'pending', "
                "channel VARCHAR(20) DEFAULT '', "
                "sent_at DATETIME DEFAULT NULL, "
                "response VARCHAR(20) DEFAULT '', "
                "response_at DATETIME DEFAULT NULL, "
                "response_note TEXT DEFAULT '', "
                "feedback_token VARCHAR(32) DEFAULT '', "
                "handled_by VARCHAR(80) DEFAULT '', "
                "handled_at DATETIME DEFAULT NULL, "
                "handle_note TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_status_date ON follow_ups(status, planned_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_assignee ON follow_ups(assigned_to)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_token ON follow_ups(feedback_token)"))

            # ── 多模板/多轮回访升级：补字段 + 解除 visit_id UNIQUE ──
            fu_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(follow_ups)")).fetchall()}
            if "round_no" not in fu_cols:
                # 老库 visit_id 是 UNIQUE — 必须 rebuild 表才能去掉
                # SQLite 标准 rebuild：建新表 → copy → drop → rename
                conn.execute(text(
                    "CREATE TABLE follow_ups_new ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE, "
                    "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                    "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                    "template_id INTEGER DEFAULT NULL REFERENCES follow_up_templates(id) ON DELETE SET NULL, "
                    "template_name VARCHAR(120) DEFAULT '', "
                    "round_no INTEGER DEFAULT 1, "
                    "round_name VARCHAR(80) DEFAULT '', "
                    "response_data TEXT DEFAULT '', "
                    "store VARCHAR(40) DEFAULT '', "
                    "assigned_to VARCHAR(80) DEFAULT '', "
                    "planned_date VARCHAR(20) DEFAULT '', "
                    "status VARCHAR(20) DEFAULT 'pending', "
                    "channel VARCHAR(20) DEFAULT '', "
                    "sent_at DATETIME DEFAULT NULL, "
                    "response VARCHAR(20) DEFAULT '', "
                    "response_at DATETIME DEFAULT NULL, "
                    "response_note TEXT DEFAULT '', "
                    "feedback_token VARCHAR(32) DEFAULT '', "
                    "handled_by VARCHAR(80) DEFAULT '', "
                    "handled_at DATETIME DEFAULT NULL, "
                    "handle_note TEXT DEFAULT '', "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                # 复制现有数据（老库的没有 round_no，默认 1）
                conn.execute(text(
                    "INSERT INTO follow_ups_new ("
                    "id, visit_id, customer_id, pet_id, store, assigned_to, planned_date,"
                    " status, channel, sent_at, response, response_at, response_note,"
                    " feedback_token, handled_by, handled_at, handle_note, created_at, updated_at)"
                    " SELECT id, visit_id, customer_id, pet_id, store, assigned_to, planned_date,"
                    " status, channel, sent_at, response, response_at, response_note,"
                    " feedback_token, handled_by, handled_at, handle_note, created_at, updated_at"
                    " FROM follow_ups"
                ))
                conn.execute(text("DROP TABLE follow_ups"))
                conn.execute(text("ALTER TABLE follow_ups_new RENAME TO follow_ups"))
                # 重建索引
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_status_date ON follow_ups(status, planned_date)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_assignee ON follow_ups(assigned_to)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_token ON follow_ups(feedback_token)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_visit_round ON follow_ups(visit_id, round_no)"))
            # ── 健康运营 / 复诊计划：FollowUp 扩展字段（旧库补列，幂等） ──
            fu_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(follow_ups)")).fetchall()}
            if "source_type" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN source_type VARCHAR(40) DEFAULT 'visit_default'"))
            if "source_id" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN source_id INTEGER DEFAULT NULL"))
            if "reason" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN reason TEXT DEFAULT ''"))
            if "question_text" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN question_text TEXT DEFAULT ''"))
            if "expected_reply_type" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN expected_reply_type VARCHAR(20) DEFAULT 'text'"))
            if "risk_trigger" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN risk_trigger TEXT DEFAULT ''"))
            if "priority" not in fu_cols:
                conn.execute(text("ALTER TABLE follow_ups ADD COLUMN priority VARCHAR(20) DEFAULT 'normal'"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_source ON follow_ups(source_type, source_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_followup_priority ON follow_ups(priority, planned_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_care_summary_visit ON client_care_summaries(visit_id, status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_care_plan_visit ON care_plans(visit_id, status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_care_plan_store_status ON care_plans(store, status)"))

            # ── 回访模板（按疾病系统分类） ─────────────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS follow_up_templates ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(120) NOT NULL UNIQUE, "
                "system VARCHAR(40) DEFAULT '', "
                "keywords TEXT DEFAULT '', "
                "priority INTEGER DEFAULT 50, "
                "rounds_json TEXT DEFAULT '[]', "
                "is_active BOOLEAN DEFAULT 1, "
                "is_builtin BOOLEAN DEFAULT 0, "
                "notes TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_futpl_active_pri ON follow_up_templates(is_active, priority)"))

            # ── 疾病字典（用于诊断 autocomplete） ─────────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS diseases ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(160) NOT NULL UNIQUE, "
                "system VARCHAR(40) DEFAULT '', "
                "aliases TEXT DEFAULT '', "
                "severity VARCHAR(20) DEFAULT '', "
                "species VARCHAR(40) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "is_builtin BOOLEAN DEFAULT 0, "
                "use_count INTEGER DEFAULT 0, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_disease_system ON diseases(system)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_disease_use ON diseases(use_count DESC)"))

            # ── 麻醉单 + 麻醉/管控药台账（国标合规） ───────────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS anesthesia_orders ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "anesth_date VARCHAR(20) DEFAULT '', "
                "asa_grade VARCHAR(10) DEFAULT '', "
                "vet_name VARCHAR(80) DEFAULT '', "
                "cosigner VARCHAR(80) DEFAULT '', "
                "start_time VARCHAR(10) DEFAULT '', "
                "end_time VARCHAR(10) DEFAULT '', "
                "recovery VARCHAR(40) DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'issued', "
                "total_amount REAL DEFAULT 0.0, "
                "store VARCHAR(40) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anesth_visit ON anesthesia_orders(visit_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anesth_date ON anesthesia_orders(anesth_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anesth_store ON anesthesia_orders(store)"))

            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS anesthesia_order_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "order_id INTEGER NOT NULL REFERENCES anesthesia_orders(id) ON DELETE CASCADE, "
                "item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL, "
                "drug_name VARCHAR(120) DEFAULT '', "
                "route VARCHAR(20) DEFAULT 'IV', "
                "concentration VARCHAR(40) DEFAULT '', "
                "dose_amount REAL DEFAULT 0.0, "
                "dose_unit VARCHAR(20) DEFAULT 'mg', "
                "total_qty REAL DEFAULT 0.0, "
                "total_unit VARCHAR(20) DEFAULT '', "
                "unit_price REAL DEFAULT 0.0, "
                "subtotal REAL DEFAULT 0.0, "
                "is_service BOOLEAN DEFAULT 0, "
                "note VARCHAR(200) DEFAULT ''"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anesth_item_order ON anesthesia_order_items(order_id)"))

            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS narcotics_ledger ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_date VARCHAR(20) DEFAULT '', "
                "item_id INTEGER DEFAULT NULL REFERENCES inventory_items(id) ON DELETE SET NULL, "
                "item_name VARCHAR(120) DEFAULT '', "
                "direction VARCHAR(10) DEFAULT 'out', "
                "source VARCHAR(30) DEFAULT 'manual', "
                "qty REAL DEFAULT 0.0, "
                "unit VARCHAR(20) DEFAULT '', "
                "balance_after REAL DEFAULT 0.0, "
                "operator VARCHAR(80) DEFAULT '', "
                "cosigner VARCHAR(80) DEFAULT '', "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "anesth_order_id INTEGER DEFAULT NULL REFERENCES anesthesia_orders(id) ON DELETE SET NULL, "
                "store VARCHAR(40) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_narc_item_date ON narcotics_ledger(item_id, event_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_narc_store_date ON narcotics_ledger(store, event_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_narc_source ON narcotics_ledger(source)"))

            # ── 麻醉监护表（手术中逐时段生命体征 · 手机录入 + PDF 导出）─────
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS anesthesia_monitor_sheets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "visit_id INTEGER DEFAULT NULL REFERENCES visits(id) ON DELETE SET NULL, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "monitor_date VARCHAR(20) DEFAULT '', "
                "procedure VARCHAR(200) DEFAULT '', "
                "anesthetist VARCHAR(80) DEFAULT '', "
                "surgeon VARCHAR(80) DEFAULT '', "
                "asa_grade VARCHAR(10) DEFAULT '', "
                "agent VARCHAR(80) DEFAULT '', "
                "weight_kg REAL DEFAULT 0.0, "
                "start_time VARCHAR(10) DEFAULT '', "
                "end_time VARCHAR(10) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'open', "
                "store VARCHAR(40) DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "closed_at DATETIME DEFAULT NULL, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anmon_visit ON anesthesia_monitor_sheets(visit_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anmon_store_date ON anesthesia_monitor_sheets(store, monitor_date)"))

            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS anesthesia_monitor_entries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "sheet_id INTEGER NOT NULL REFERENCES anesthesia_monitor_sheets(id) ON DELETE CASCADE, "
                "recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "recorded_by VARCHAR(80) DEFAULT '', "
                "hr INTEGER DEFAULT 0, "
                "rr INTEGER DEFAULT 0, "
                "spo2 INTEGER DEFAULT 0, "
                "etco2 INTEGER DEFAULT 0, "
                "temperature_c REAL DEFAULT 0.0, "
                "bp_sys INTEGER DEFAULT 0, "
                "bp_dia INTEGER DEFAULT 0, "
                "bp_map INTEGER DEFAULT 0, "
                "agent_pct REAL DEFAULT 0.0, "
                "o2_flow REAL DEFAULT 0.0, "
                "depth VARCHAR(20) DEFAULT '', "
                "event VARCHAR(200) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_anmon_entry_sheet ON anesthesia_monitor_entries(sheet_id, recorded_at)"))

            # 美容单
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS grooming_orders ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "customer_id INTEGER DEFAULT NULL REFERENCES customers(id) ON DELETE SET NULL, "
                "pet_id INTEGER DEFAULT NULL REFERENCES pets(id) ON DELETE SET NULL, "
                "appointment_id INTEGER DEFAULT NULL REFERENCES appointments(id) ON DELETE SET NULL, "
                "invoice_id INTEGER DEFAULT NULL REFERENCES invoices(id) ON DELETE SET NULL, "
                "groom_date VARCHAR(20) DEFAULT '', "
                "start_time VARCHAR(10) DEFAULT '', "
                "end_time VARCHAR(10) DEFAULT '', "
                "groomer_name VARCHAR(80) DEFAULT '', "
                "services_json TEXT DEFAULT '[]', "
                "total_amount REAL DEFAULT 0.0, "
                "before_photos TEXT DEFAULT '', "
                "after_photos TEXT DEFAULT '', "
                "pet_size VARCHAR(20) DEFAULT '', "
                "coat_length VARCHAR(20) DEFAULT '', "
                "skin_condition VARCHAR(200) DEFAULT '', "
                "behavior_note VARCHAR(200) DEFAULT '', "
                "store VARCHAR(40) DEFAULT '', "
                "notes TEXT DEFAULT '', "
                "status VARCHAR(20) DEFAULT 'active', "
                "voided_by VARCHAR(80) DEFAULT '', "
                "voided_at DATETIME DEFAULT NULL, "
                "void_reason VARCHAR(200) DEFAULT '', "
                "created_by VARCHAR(80) DEFAULT '', "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_grooming_pet ON grooming_orders(pet_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_grooming_customer ON grooming_orders(customer_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_grooming_date ON grooming_orders(groom_date)"))

            # grooming_orders: 补 assistant_name 列
            try:
                _gr_cols = {c[1] for c in conn.execute(text("PRAGMA table_info(grooming_orders)")).fetchall()}
                if "assistant_name" not in _gr_cols:
                    conn.execute(text("ALTER TABLE grooming_orders ADD COLUMN assistant_name VARCHAR(80) DEFAULT ''"))
            except Exception as _e:
                print(f"[migrations] grooming_orders.assistant_name skipped: {_e}")

            conn.commit()
    except Exception:
        # 迁移失败不阻塞启动（新库 create_all 已含新列）
        return


def _seed_data() -> None:
    """一次性补录历史数据，幂等（重复执行安全）。"""
    if not settings.database_url.startswith("sqlite"):
        return
    rows = [
        {
            "applicant_name": "郑香玉", "phone": "15323455977",
            "clinic_store": "龙华店", "appointment_at": "2026-04-25",
            "location_address": "中国广东省深圳市",
            "id_number": "632123199706180526",
            "address": "秋港花园；秋港花园D 5楼下灌木丛",
            "cat_nickname": "黑猫带一点白", "cat_gender": "male",
            "age_estimate": "6个月-1岁（最佳）", "weight_estimate": "6",
            "health_note": "花色特征：黑猫带一点白；亲人程度：亲人，随便摸",
            "post_surgery_plan": "医院住院",
            "status": "surgery_completed",
            "created_at": "2026-04-24 12:31:00",
        },
        {
            "applicant_name": "张春晓", "phone": "19856109910",
            "clinic_store": "龙华店", "appointment_at": "2026-04-26",
            "location_address": "中国广东省深圳市",
            "id_number": "340603199502220224",
            "address": "1980科技文化产业园；停车场",
            "cat_nickname": "黑白", "cat_gender": "female",
            "age_estimate": "6个月-1岁（最佳）", "weight_estimate": "3.5",
            "health_note": "花色特征：黑白；怀孕/哺乳：是，肚子很大/乳头红肿有奶；亲人程度：可摸但警惕",
            "post_surgery_plan": "医院住院",
            "status": "cancelled",
            "created_at": "2026-04-25 20:33:00",
        },
    ]
    try:
        with engine.begin() as conn:
            for r in rows:
                exists = conn.execute(
                    text("SELECT 1 FROM applications WHERE phone=:p AND created_at=:c"),
                    {"p": r["phone"], "c": r["created_at"]},
                ).fetchone()
                if exists:
                    continue
                conn.execute(text("""
                    INSERT INTO applications
                      (applicant_name, phone, wechat_openid, clinic_store, appointment_at,
                       location_address, id_number, address, cat_nickname, cat_gender,
                       age_estimate, weight_estimate, health_note, post_surgery_plan,
                       status, agree_ear_tip, agree_no_pet_fraud, is_proxy,
                       created_at, updated_at)
                    VALUES
                      (:applicant_name, :phone, '', :clinic_store, :appointment_at,
                       :location_address, :id_number, :address, :cat_nickname, :cat_gender,
                       :age_estimate, :weight_estimate, :health_note, :post_surgery_plan,
                       :status, 1, 1, 0,
                       :created_at, :created_at)
                """), r)
    except Exception:
        pass
    # 兽医级疾病库 + 回访模板（幂等填充）
    _seed_vet_diseases_and_templates()


def _seed_vet_diseases_and_templates() -> None:
    """从 app/data/vet_seed.py 灌入疾病字典 + 回访模板。

    幂等：按 name 查重；新增写入，不覆盖用户编辑过的内置项。
    """
    try:
        import json as _json
        from app.data.vet_seed import DISEASES, TEMPLATES
    except Exception as e:
        print(f"[seed_vet] import failed: {e}")
        return
    try:
        with engine.begin() as conn:
            # diseases — 新增插入；已存在的 builtin 项同步 aliases（用户可能依赖新增的口语别名）
            for (name, system, aliases, severity, species) in DISEASES:
                exists = conn.execute(
                    text("SELECT is_builtin FROM diseases WHERE name=:n"),
                    {"n": name},
                ).fetchone()
                if exists:
                    if exists[0]:
                        conn.execute(text(
                            "UPDATE diseases SET aliases=:al, system=:sy, severity=:sv, species=:sp, updated_at=CURRENT_TIMESTAMP "
                            "WHERE name=:n AND is_builtin=1"
                        ), {"n": name, "sy": system, "al": aliases, "sv": severity, "sp": species})
                    continue
                conn.execute(text(
                    "INSERT INTO diseases (name, system, aliases, severity, species, notes, is_builtin, use_count, created_at, updated_at) "
                    "VALUES (:n, :sy, :al, :sv, :sp, '', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"n": name, "sy": system, "al": aliases, "sv": severity, "sp": species})

            # follow_up_templates
            for tpl in TEMPLATES:
                exists = conn.execute(
                    text("SELECT 1 FROM follow_up_templates WHERE name=:n"),
                    {"n": tpl["name"]},
                ).fetchone()
                if exists:
                    continue
                rounds_json = _json.dumps(tpl["rounds"], ensure_ascii=False)
                conn.execute(text(
                    "INSERT INTO follow_up_templates "
                    "(name, system, keywords, priority, rounds_json, is_active, is_builtin, notes, created_at, updated_at) "
                    "VALUES (:n, :sy, :kw, :pr, :rj, 1, 1, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {
                    "n": tpl["name"], "sy": tpl["system"],
                    "kw": tpl["keywords"], "pr": int(tpl["priority"]),
                    "rj": rounds_json,
                })
    except Exception as e:
        print(f"[seed_vet] insert failed: {e}")
