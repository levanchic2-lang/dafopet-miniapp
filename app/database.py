from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


Path("data").mkdir(parents=True, exist_ok=True)
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
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
            if "id_number" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN id_number VARCHAR(40) DEFAULT ''"))
            if "post_surgery_plan" not in names:
                conn.execute(text("ALTER TABLE applications ADD COLUMN post_surgery_plan VARCHAR(120) DEFAULT ''"))

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
            conn.commit()
    except Exception:
        # 迁移失败不阻塞启动（新库 create_all 已含新列）
        return
