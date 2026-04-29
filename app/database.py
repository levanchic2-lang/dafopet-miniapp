from pathlib import Path

from sqlalchemy import create_engine, text
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
