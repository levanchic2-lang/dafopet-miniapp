from __future__ import annotations

import enum
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from typing import Optional
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApplicationStatus(str, enum.Enum):
    draft = "draft"
    pending_ai = "pending_ai"
    pending_manual = "pending_manual"
    pre_approved = "pre_approved"
    approved = "approved"
    scheduled = "scheduled"
    no_show = "no_show"
    cancelled = "cancelled"
    rejected = "rejected"
    arrived_verified = "arrived_verified"
    surgery_completed = "surgery_completed"


class AppointmentCategory(str, enum.Enum):
    tnr = "tnr"
    outpatient = "outpatient"
    surgery = "surgery"
    beauty = "beauty"
    # 保留旧值以兼容历史数据
    grooming = "grooming"
    washcare = "washcare"


class AppointmentStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    arrived = "arrived"
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"


class MediaKind(str, enum.Enum):
    application_image = "application_image"
    application_video = "application_video"
    surgery_before = "surgery_before"
    surgery_after = "surgery_after"


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    applicant_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(40))
    # 小程序订阅消息：申请人 openid（用于推送审核/手术结果）
    wechat_openid: Mapped[str] = mapped_column(String(64), default="")
    # 预约信息
    clinic_store: Mapped[str] = mapped_column(String(80), default="")  # 东环店/横岗店
    appointment_at: Mapped[str] = mapped_column(String(40), default="")  # YYYY-MM-DD（仅日期；历史数据可能含时间）
    # 客户定位（可选）
    location_lat: Mapped[str] = mapped_column(String(32), default="")
    location_lng: Mapped[str] = mapped_column(String(32), default="")
    location_address: Mapped[str] = mapped_column(String(500), default="")
    # 身份证号（敏感信息：后台需脱敏展示）
    id_number: Mapped[str] = mapped_column(String(40), default="")
    # 术后打算：申请人勾选的固定中文选项全文（与网页/小程序下拉一致）
    post_surgery_plan: Mapped[str] = mapped_column(String(120), default="")
    address: Mapped[str] = mapped_column(String(500))
    cat_nickname: Mapped[str] = mapped_column(String(120), default="")
    cat_gender: Mapped[str] = mapped_column(String(10))  # male / female / unknown
    cat_breed: Mapped[str] = mapped_column(String(80), default="")        # 品种（猫为主，可空）
    cat_color: Mapped[str] = mapped_column(String(80), default="")        # 毛色 / 颜色描述
    age_estimate: Mapped[str] = mapped_column(String(80), default="")
    weight_estimate: Mapped[str] = mapped_column(String(80), default="")
    health_note: Mapped[str] = mapped_column(Text, default="")
    agree_ear_tip: Mapped[bool] = mapped_column(Boolean, default=True)
    agree_no_pet_fraud: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(40), default=ApplicationStatus.pending_ai.value)
    ai_raw_json: Mapped[str] = mapped_column(Text, default="")
    ai_is_likely_stray = mapped_column(Boolean, nullable=True)
    ai_confidence = mapped_column(Float, nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")

    staff_cat_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    showcase_consent: Mapped[bool] = mapped_column(Boolean, default=False)

    # 代申请信息
    is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    proxy_name: Mapped[str] = mapped_column(String(120), default="")
    proxy_phone: Mapped[str] = mapped_column(String(40), default="")
    proxy_relation: Mapped[str] = mapped_column(String(40), default="")

    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    media = relationship("MediaFile", back_populates="application", cascade="all, delete-orphan")
    notifications = relationship("NotificationLog", back_populates="application", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="application")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wechat_openid: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(40), default=AppointmentCategory.outpatient.value)
    status: Mapped[str] = mapped_column(String(40), default=AppointmentStatus.pending.value)
    service_name: Mapped[str] = mapped_column(String(120), default="")
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    pet_name: Mapped[str] = mapped_column(String(120), default="")
    pet_gender: Mapped[str] = mapped_column(String(20), default="")
    store: Mapped[str] = mapped_column(String(120), default="")
    appointment_date: Mapped[str] = mapped_column(String(20), default="")
    appointment_time: Mapped[str] = mapped_column(String(20), default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    source: Mapped[str] = mapped_column(String(40), default="admin")
    notes: Mapped[str] = mapped_column(Text, default="")
    # 美容专用附加字段（nullable，其他类型为空）
    pet_size        = mapped_column(String(40),  nullable=True, default=None)
    coat_length     = mapped_column(String(20),  nullable=True, default=None)
    addon_services  = mapped_column(String(200), nullable=True, default=None)
    related_application_id = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    # 代预约信息
    is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    proxy_name: Mapped[str] = mapped_column(String(120), default="")
    proxy_phone: Mapped[str] = mapped_column(String(40), default="")
    proxy_relation: Mapped[str] = mapped_column(String(40), default="")  # 家人/朋友/员工代录/其他

    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    application = relationship("Application", back_populates="appointments")


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    stored_path: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    application = relationship("Application", back_populates="media")


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[Optional[int]] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=True, default=None)
    channel: Mapped[str] = mapped_column(String(40))  # email / log / wechat_miniapp
    payload: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    application = relationship("Application", back_populates="notifications", foreign_keys=[application_id])


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(80))  # manual_approve / reject / surgery_done ...
    actor: Mapped[str] = mapped_column(String(80), default="admin")
    application_id = mapped_column(Integer, nullable=True)
    ip: Mapped[str] = mapped_column(String(80), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FeedbackStatus(str, enum.Enum):
    pending = "pending"
    resolved = "resolved"


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    openid: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    admin_note: Mapped[str] = mapped_column(Text, default="")
    image_paths: Mapped[str] = mapped_column(Text, default="")  # JSON list of stored paths
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)


class StaffStatus(str, enum.Enum):
    probation = "probation"   # 试用中
    active = "active"         # 在职
    resigned = "resigned"     # 离职


class ContractType(str, enum.Enum):
    formal = "formal"         # 正式合同
    probation = "probation"   # 试用期合同
    parttime = "parttime"     # 兼职合同
    labor = "labor"           # 劳务合同


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    gender: Mapped[str] = mapped_column(String(10), default="")          # male / female
    birthday: Mapped[str] = mapped_column(String(20), default="")        # YYYY-MM-DD
    phone: Mapped[str] = mapped_column(String(40), default="")
    id_number: Mapped[str] = mapped_column(String(40), default="")
    store: Mapped[str] = mapped_column(String(80), default="")           # 东环店 / 横岗店
    position: Mapped[str] = mapped_column(String(80), default="")        # 前台/医生/美容师/助理/其他
    hire_date: Mapped[str] = mapped_column(String(20), default="")       # YYYY-MM-DD
    probation_end_date: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default=StaffStatus.active.value)
    resign_date: Mapped[str] = mapped_column(String(20), default="")
    resign_reason: Mapped[str] = mapped_column(Text, default="")
    emergency_contact_name: Mapped[str] = mapped_column(String(80), default="")
    emergency_contact_phone: Mapped[str] = mapped_column(String(40), default="")
    emergency_contact_relation: Mapped[str] = mapped_column(String(40), default="")
    admin_user_id = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    admin_user = relationship("AdminUser", backref="staff_profile", foreign_keys=[admin_user_id])
    contracts = relationship("Contract", back_populates="staff", cascade="all, delete-orphan")


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"))
    contract_type: Mapped[str] = mapped_column(String(20), default=ContractType.formal.value)
    start_date: Mapped[str] = mapped_column(String(20), default="")     # YYYY-MM-DD
    end_date: Mapped[str] = mapped_column(String(20), default="")       # 空=无固定期限
    file_path: Mapped[str] = mapped_column(String(512), default="")
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    staff = relationship("Staff", back_populates="contracts")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    wechat_openid: Mapped[str] = mapped_column(String(64), default="")
    id_number: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(40), default="")   # tnr / outpatient / beauty / surgery / manual
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pets = relationship("Pet", back_populates="customer", cascade="all, delete-orphan")


class Pet(Base):
    __tablename__ = "pets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), default="")
    species: Mapped[str] = mapped_column(String(40), default="cat")   # cat / dog / other
    breed: Mapped[str] = mapped_column(String(80), default="")
    gender: Mapped[str] = mapped_column(String(10), default="unknown")  # male / female / unknown
    birthday_estimate: Mapped[str] = mapped_column(String(40), default="")
    is_neutered: Mapped[bool] = mapped_column(Boolean, default=False)
    color_pattern: Mapped[str] = mapped_column(String(80), default="")
    is_stray: Mapped[bool] = mapped_column(Boolean, default=False)
    microchip_id: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    # 新版客户档案中心化所需字段
    store: Mapped[str] = mapped_column(String(40), default="")              # 短名：东环店/横岗店
    medical_record_no: Mapped[str] = mapped_column(String(40), default="")  # 病历号 DC2605xxxxx / HC2605xxxxx
    life_status: Mapped[str] = mapped_column(String(20), default="alive")   # alive / deceased
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="pets")


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True, default=None)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id = mapped_column(ForeignKey("pets.id", ondelete="SET NULL"), nullable=True, default=None)
    prescribed_date: Mapped[str] = mapped_column(String(20), default="")
    vet_name: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / issued / dispensed
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = relationship("PrescriptionItem", back_populates="prescription", cascade="all, delete-orphan")
    customer = relationship("Customer", foreign_keys=[customer_id], backref="prescriptions")
    pet = relationship("Pet", foreign_keys=[pet_id], backref="prescriptions")


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id", ondelete="CASCADE"))
    item_id = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True, default=None)
    drug_name: Mapped[str] = mapped_column(String(120), default="")
    drug_type: Mapped[str] = mapped_column(String(40), default="oral")
    dosage: Mapped[str] = mapped_column(String(80), default="")
    frequency: Mapped[str] = mapped_column(String(80), default="")
    duration_days: Mapped[str] = mapped_column(String(40), default="")
    quantity_num: Mapped[float] = mapped_column(Float, default=1.0)   # 数量（数字）
    quantity: Mapped[str] = mapped_column(String(40), default="")     # 显示用（如 14片）
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)
    instructions: Mapped[str] = mapped_column(Text, default="")
    # ── 细化字段（与库存单位关联） ──
    dose_amount: Mapped[float] = mapped_column(Float, default=0.0)        # 单次用量数字
    dose_unit: Mapped[str] = mapped_column(String(20), default="")        # 单次用量单位 (ml/mg/片)
    times_per_day: Mapped[float] = mapped_column(Float, default=0.0)      # 次/天
    item_unit: Mapped[str] = mapped_column(String(20), default="")        # 出库单位（粒/支/盒）
    print_note: Mapped[str] = mapped_column(Text, default="")             # 打印备注（客户可见）

    prescription = relationship("Prescription", back_populates="items")
    inventory_item = relationship("InventoryItem", foreign_keys=[item_id])


class SalesOrder(Base):
    __tablename__ = "sales_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    visit_id = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id = mapped_column(ForeignKey("pets.id", ondelete="SET NULL"), nullable=True, default=None)
    order_date: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / paid / cancelled
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    payment_method: Mapped[str] = mapped_column(String(40), default="")  # 现金/微信/支付宝/挂账
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = relationship("SalesOrderItem", back_populates="order", cascade="all, delete-orphan")
    customer = relationship("Customer", foreign_keys=[customer_id], backref="sales_orders")


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"))
    item_id = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True, default=None)
    item_name: Mapped[str] = mapped_column(String(120), default="")
    item_type: Mapped[str] = mapped_column(String(40), default="product")  # product/service/medication/vaccine
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(String(200), default="")

    order = relationship("SalesOrder", back_populates="items")
    inventory_item = relationship("InventoryItem", foreign_keys=[item_id])


class Visit(Base):
    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id = mapped_column(ForeignKey("pets.id", ondelete="SET NULL"), nullable=True, default=None)
    appointment_id = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True, default=None)

    visit_date: Mapped[str] = mapped_column(String(20), default="")        # YYYY-MM-DD
    visit_type: Mapped[str] = mapped_column(String(40), default="outpatient")  # outpatient/followup/postop/vaccine/surgery_consult/other
    chief_complaint: Mapped[str] = mapped_column(Text, default="")          # 主诉
    physical_exam: Mapped[str] = mapped_column(Text, default="")            # 体格检查（体温/体重/心率等）
    diagnosis: Mapped[str] = mapped_column(Text, default="")                # 诊断结论
    treatment_plan: Mapped[str] = mapped_column(Text, default="")           # 处理方案
    notes: Mapped[str] = mapped_column(Text, default="")                    # 补充备注

    vet_name: Mapped[str] = mapped_column(String(80), default="")
    # 7 步 SOAP 工作流之"回访"步骤
    follow_up_note: Mapped[str] = mapped_column(Text, default="")           # 回访备注
    follow_up_at: Mapped[str] = mapped_column(String(20), default="")       # 回访日期 YYYY-MM-DD
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", backref="visits", foreign_keys=[customer_id])
    pet = relationship("Pet", backref="visits", foreign_keys=[pet_id])


class InventoryItem(Base):
    """品目表：药品/耗材/商品/服务项目"""
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)              # 品名
    # 大类：medication/consumable/product/vaccine/antiparasitic/grooming/lab/imaging/microscopy
    category: Mapped[str] = mapped_column(String(60), default="medication")
    # 小类：controlled/general/washcare/styling/addon/routine_lab/external_lab/dr/ct/mri/ultrasound/optical/electron
    subcategory: Mapped[str] = mapped_column(String(60), default="")
    is_service: Mapped[bool] = mapped_column(Boolean, default=False)            # 服务项目不占库存
    is_controlled: Mapped[bool] = mapped_column(Boolean, default=False)         # 精神类/麻药管控标记
    unit: Mapped[str] = mapped_column(String(20), default="个")                 # 主单位（片/ml/盒/次）
    unit2: Mapped[str] = mapped_column(String(20), default="")                  # 副单位（盒/瓶）
    unit2_ratio: Mapped[float] = mapped_column(Float, default=1.0)              # 1副单位 = N主单位
    sell_price: Mapped[float] = mapped_column(Float, default=0.0)               # 销售单价（按主单位）
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)               # 进价
    stock_qty: Mapped[float] = mapped_column(Float, default=0.0)                # 当前库存（服务项目忽略）
    low_stock_min: Mapped[float] = mapped_column(Float, default=0.0)            # 低库存预警线
    supplier: Mapped[str] = mapped_column(String(200), default="")              # 供应商
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)              # 下架/停用
    # 多门店分离：空 = 通用两店共享，"东环店" / "横岗店" = 仅该店
    store: Mapped[str] = mapped_column(String(40), default="")
    last_counted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 上次盘点时间
    # 门店级价格覆盖（JSON 字符串，方案 H）
    # 格式：{"东环店": {"sell": 99.5, "cost": 50}, "横岗店": {"sell": 105, "cost": 52}}
    # 未配置的门店 → 回退到 sell_price / cost_price 默认价
    # 加店无需改 schema，直接 JSON 添 key
    store_overrides: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions = relationship("InventoryTransaction", back_populates="item", cascade="all, delete-orphan")
    batches = relationship("InventoryBatch", back_populates="item", cascade="all, delete-orphan",
                           order_by="InventoryBatch.expiry_date")


class InventoryTransaction(Base):
    """出入库流水记录"""
    __tablename__ = "inventory_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    # type: in（入库）/ out（出库）/ adjust（盘点调整）/ return（退货）
    tx_type: Mapped[str] = mapped_column(String(20), default="in")
    qty: Mapped[float] = mapped_column(Float, nullable=False)                   # 变动数量（正数）
    qty_before: Mapped[float] = mapped_column(Float, default=0.0)              # 变动前库存
    qty_after: Mapped[float] = mapped_column(Float, default=0.0)               # 变动后库存
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)              # 本次单价（入库用进价，出库用售价）
    # ref_type: manual/prescription/sales_order/lab_order/imaging_order
    ref_type: Mapped[str] = mapped_column(String(40), default="manual")
    ref_id: Mapped[int] = mapped_column(Integer, nullable=True)
    operator: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    item = relationship("InventoryItem", back_populates="transactions")


class InventoryBatch(Base):
    """库存批次：记录每批入库的有效期与剩余数量"""
    __tablename__ = "inventory_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    batch_no: Mapped[str] = mapped_column(String(80), default="")       # 批次号（选填）
    quantity: Mapped[float] = mapped_column(Float, default=0.0)         # 该批次剩余数量
    expiry_date: Mapped[str] = mapped_column(String(20), default="")    # YYYY-MM-DD
    received_date: Mapped[str] = mapped_column(String(20), default="")  # 入库日期
    notes: Mapped[str] = mapped_column(String(500), default="")
    is_depleted: Mapped[bool] = mapped_column(Boolean, default=False)   # 手动标记已用完
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    item = relationship("InventoryItem", back_populates="batches")


class StocktakeSession(Base):
    """盘点会话：一次循环盘点"""
    __tablename__ = "stocktake_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), default="")          # 盘点备注名
    category_filter: Mapped[str] = mapped_column(String(60), default="")  # 空=全部；否则限定大类
    status: Mapped[str] = mapped_column(String(20), default="open")     # open / completed
    operator: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    item_count: Mapped[int] = mapped_column(Integer, default=0)         # 参与品目数
    variance_count: Mapped[int] = mapped_column(Integer, default=0)     # 有差异品目数
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    items = relationship("StocktakeItem", back_populates="session", cascade="all, delete-orphan")


class StocktakeItem(Base):
    """盘点明细：盘点会话中每个品目的实盘记录"""
    __tablename__ = "stocktake_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("stocktake_sessions.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True)
    item_name: Mapped[str] = mapped_column(String(200), default="")     # 冗余品名，防品目删除后丢失
    category: Mapped[str] = mapped_column(String(60), default="")
    unit: Mapped[str] = mapped_column(String(20), default="")
    system_qty: Mapped[float] = mapped_column(Float, default=0.0)       # 建单时的系统库存
    actual_qty: Mapped[float] = mapped_column(Float, nullable=True)     # 实盘数量（NULL=未盘）
    variance: Mapped[float] = mapped_column(Float, default=0.0)         # actual - system
    is_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)   # 是否已产生调整流水
    notes: Mapped[str] = mapped_column(String(500), default="")

    session = relationship("StocktakeSession", back_populates="items")
    item = relationship("InventoryItem")


class RabiesVaccineRecord(Base):
    """狂犬疫苗免疫登记表"""
    __tablename__ = "rabies_vaccine_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cert_no: Mapped[str] = mapped_column(String(60), default="")          # 免疫证号（最后录入）

    # 关联客户/宠物档案
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)

    # 第一部分：主人填写
    owner_name:    Mapped[str] = mapped_column(String(120), default="")   # 姓名
    owner_address: Mapped[str] = mapped_column(String(500), default="")   # 地址
    owner_phone:   Mapped[str] = mapped_column(String(40),  default="")   # 电话

    # 动物基本情况（主人填）
    animal_name:   Mapped[str] = mapped_column(String(80),  default="")   # 动物名称
    animal_breed:  Mapped[str] = mapped_column(String(80),  default="")   # 品种
    animal_dob:    Mapped[str] = mapped_column(String(40),  default="")   # 出生年月/年龄
    animal_gender: Mapped[str] = mapped_column(String(10),  default="")   # 性别
    animal_color:  Mapped[str] = mapped_column(String(80),  default="")   # 毛色

    # 主人签名
    owner_signature_path: Mapped[str] = mapped_column(String(512), default="")
    owner_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)

    # 第二部分：医护填写
    vaccine_manufacturer: Mapped[str] = mapped_column(String(120), default="")  # 厂家
    vaccine_batch_no:     Mapped[str] = mapped_column(String(80),  default="")  # 批号
    vaccine_date:         Mapped[str] = mapped_column(String(20),  default="")  # 免疫时间

    # 医护签名
    staff_name:           Mapped[str] = mapped_column(String(80),  default="")
    staff_signature_path: Mapped[str] = mapped_column(String(512), default="")
    staff_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)

    clinic_store: Mapped[str] = mapped_column(String(60), default="横岗店")

    # 状态: owner_pending / staff_pending / completed
    status: Mapped[str] = mapped_column(String(20), default="owner_pending")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", foreign_keys=[customer_id])
    pet      = relationship("Pet",      foreign_keys=[pet_id])


class Invoice(Base):
    """收费单"""
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_no: Mapped[str] = mapped_column(String(40), default="")          # YYYYMMDD-序号
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    invoice_date: Mapped[str] = mapped_column(String(20), default="")
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)              # 合计
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0)       # 折扣/减免
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)          # 实收
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid")  # unpaid / paid
    payment_method: Mapped[str] = mapped_column(String(40), default="")      # cash/wechat/alipay/credit
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items    = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    customer = relationship("Customer", foreign_keys=[customer_id])
    pet      = relationship("Pet",      foreign_keys=[pet_id])


class InvoiceItem(Base):
    """收费明细行"""
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    ref_type: Mapped[str] = mapped_column(String(40), default="manual")  # prescription/sales_order/manual
    ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    description: Mapped[str] = mapped_column(String(300), default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)

    invoice = relationship("Invoice", back_populates="items")


class Vaccination(Base):
    """疫苗接种记录"""
    __tablename__ = "vaccinations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)

    # 疫苗信息
    # vaccine_type: rabies/combo_3/combo_6/canine_8/deworming/other
    vaccine_type: Mapped[str]  = mapped_column(String(40),  default="other")
    vaccine_name: Mapped[str]  = mapped_column(String(120), default="")   # 品牌/商品名
    batch_no:     Mapped[str]  = mapped_column(String(80),  default="")   # 批次号
    dose_number:  Mapped[int]  = mapped_column(Integer,     default=1)    # 第几针（99=加强）
    vaccinated_date: Mapped[str] = mapped_column(String(20), default="")  # 接种日期
    next_due_date:   Mapped[str] = mapped_column(String(20), default="")  # 下次到期日

    # 关联库存品目（出库用）
    inventory_item_id = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True, default=None)

    # 是否免费（狂犬疫苗 = True，不开收费单）
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)

    # 关联来源
    rabies_record_id = mapped_column(ForeignKey("rabies_vaccine_records.id", ondelete="SET NULL"), nullable=True, default=None)
    invoice_id       = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, default=None)

    vet_name:   Mapped[str] = mapped_column(String(80), default="")
    notes:      Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    pet           = relationship("Pet",                 foreign_keys=[pet_id])
    customer      = relationship("Customer",            foreign_keys=[customer_id])
    inventory_item = relationship("InventoryItem",      foreign_keys=[inventory_item_id])
    rabies_record = relationship("RabiesVaccineRecord", foreign_keys=[rabies_record_id])


class AdoptionPet(Base):
    __tablename__ = "adoption_pets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    species: Mapped[str] = mapped_column(String(40), default="cat")   # cat / dog / other
    breed: Mapped[str] = mapped_column(String(80), default="")
    age_estimate: Mapped[str] = mapped_column(String(40), default="")  # e.g. "2岁"
    gender: Mapped[str] = mapped_column(String(20), default="unknown") # male/female/unknown
    personality: Mapped[str] = mapped_column(Text, default="")
    health_note: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[str] = mapped_column(Text, default="")        # 领养要求
    image1_path: Mapped[str] = mapped_column(String(512), default="")
    image2_path: Mapped[str] = mapped_column(String(512), default="")
    video_path: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(20), default="available")  # available/adopted/paused
    adoption_agreement_path: Mapped[str] = mapped_column(String(512), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TnrStoreConfig(Base):
    """每家门店的 TNR 预约配额配置"""
    __tablename__ = "tnr_store_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    tnr_monthly_quota: Mapped[int] = mapped_column(Integer, default=30)   # 每月最大已确认 TNR 预约数
    tnr_accepting: Mapped[bool] = mapped_column(Boolean, default=True)    # 管理员手动开关
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[str] = mapped_column(String(80), default="")


class ExamOrder(Base):
    """检查单（关联就诊记录）"""
    __tablename__ = "exam_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), nullable=False)

    items_json: Mapped[str] = mapped_column(Text, default="[]")   # [{name, item_id, notes}]
    notes:      Mapped[str] = mapped_column(Text, default="")
    status:     Mapped[str] = mapped_column(String(20), default="pending")  # pending/completed

    # 手机上传 token（24小时有效）
    upload_token:     Mapped[str]           = mapped_column(String(80), unique=True, default="")
    token_expires_at: Mapped[datetime|None] = mapped_column(DateTime, nullable=True, default=None)

    created_by: Mapped[str]      = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    visit   = relationship("Visit",       backref="exam_orders", foreign_keys=[visit_id])
    reports = relationship("ExamReport",  backref="exam_order",  cascade="all, delete-orphan")


class ExamReport(Base):
    """检查报告文件（PDF 或图片）"""
    __tablename__ = "exam_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exam_order_id = mapped_column(ForeignKey("exam_orders.id", ondelete="CASCADE"), nullable=False)

    file_path:     Mapped[str] = mapped_column(String(500), default="")
    original_name: Mapped[str] = mapped_column(String(200), default="")
    file_type:     Mapped[str] = mapped_column(String(10),  default="image")  # pdf / image
    item_label:    Mapped[str] = mapped_column(String(120), default="")        # 归属检查项（可选标注）
    uploaded_by:   Mapped[str] = mapped_column(String(80),  default="")
    uploaded_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CalendarBlock(Base):
    """全天封锁日程（如：美容师休息）"""
    __tablename__ = "calendar_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), default="")         # 标题，如"美容师休息"
    block_date: Mapped[str] = mapped_column(String(20), default="")     # YYYY-MM-DD
    store: Mapped[str] = mapped_column(String(40), default="")          # 短名；空=全部门店
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DewormingRecord(Base):
    """驱虫记录（独立于疫苗管理）"""
    __tablename__ = "deworming_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id", ondelete="SET NULL"), nullable=True, default=None)
    deworm_date: Mapped[str] = mapped_column(String(20), default="")         # 驱虫日期 YYYY-MM-DD
    deworm_type: Mapped[str] = mapped_column(String(40), default="external") # external/internal/combo
    product_name: Mapped[str] = mapped_column(String(120), default="")       # 药品名称
    weight_kg: Mapped[float] = mapped_column(Float, default=0.0)             # 当时体重
    dose: Mapped[str] = mapped_column(String(80), default="")                # 剂量
    next_due_date: Mapped[str] = mapped_column(String(20), default="")       # 下次到期日
    vet_name: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pet      = relationship("Pet",      backref="deworming_records", foreign_keys=[pet_id])
    customer = relationship("Customer", backref="deworming_records", foreign_keys=[customer_id])


class WeightRecord(Base):
    """体重记录（用于体重曲线）"""
    __tablename__ = "weight_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pet_id      = mapped_column(ForeignKey("pets.id", ondelete="CASCADE"), nullable=False)
    visit_id    = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True, default=None)
    record_date: Mapped[str] = mapped_column(String(20), default="")         # YYYY-MM-DD
    weight_kg: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    pet = relationship("Pet", backref="weight_records", foreign_keys=[pet_id])


class PrescriptionTemplate(Base):
    """处方套餐模板（常用处方一键套用）"""
    __tablename__ = "prescription_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), default="")            # 模板名，如"猫上呼吸道感染"
    category: Mapped[str] = mapped_column(String(40), default="")         # 类别标签（可选）
    items_json: Mapped[str] = mapped_column(Text, default="[]")           # 药品明细 JSON
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    use_count: Mapped[int] = mapped_column(Integer, default=0)            # 使用次数


class MedicalDocument(Base):
    """医疗文书（同意书、协议、报告等）"""
    __tablename__ = "medical_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="SET NULL"), nullable=True, default=None)

    doc_type: Mapped[str] = mapped_column(String(40), default="consent")     # consent/agreement/report/other
    title: Mapped[str] = mapped_column(String(200), default="")              # 文书名称
    file_path: Mapped[str] = mapped_column(String(500), default="")
    original_name: Mapped[str] = mapped_column(String(200), default="")
    file_type: Mapped[str] = mapped_column(String(10), default="pdf")        # pdf/image
    file_size: Mapped[int] = mapped_column(Integer, default=0)               # bytes
    notes: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(80), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    pet      = relationship("Pet",      backref="medical_documents", foreign_keys=[pet_id])
    customer = relationship("Customer", backref="medical_documents", foreign_keys=[customer_id])


class Wallet(Base):
    """客户钱包：现金预存款。一个客户一个钱包行。"""
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, unique=True)
    balance:           Mapped[float] = mapped_column(Float, default=0.0)   # 当前余额 = principal + bonus
    balance_principal: Mapped[float] = mapped_column(Float, default=0.0)   # 余额-本金部分（充值实付）
    balance_bonus:     Mapped[float] = mapped_column(Float, default=0.0)   # 余额-赠送部分（送的部分）
    lifetime_recharge: Mapped[float] = mapped_column(Float, default=0.0)   # 累计充值
    lifetime_consume:  Mapped[float] = mapped_column(Float, default=0.0)   # 累计消费
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", foreign_keys=[customer_id])


class WalletTransaction(Base):
    """钱包流水：每一笔充值/消费/退款/调账。"""
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id   = mapped_column(ForeignKey("wallets.id",   ondelete="CASCADE"), nullable=False)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    # type: recharge / consume / refund / adjust
    type:          Mapped[str]   = mapped_column(String(20), default="consume")
    amount:        Mapped[float] = mapped_column(Float, default=0.0)        # 本次变动，正=进，负=出
    balance_after: Mapped[float] = mapped_column(Float, default=0.0)        # 操作后余额
    # 关联（充值时可记 pay_method，消费时关联 invoice_id）
    pay_method: Mapped[str] = mapped_column(String(40), default="")         # 充值时记 cash/wechat/...
    invoice_id  = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, default=None)
    bonus_amount: Mapped[float] = mapped_column(Float, default=0.0)         # 赠送金额（充 500 送 50 时）
    consumed_principal: Mapped[float] = mapped_column(Float, default=0.0)   # 本笔消费扣的本金部分
    consumed_bonus:     Mapped[float] = mapped_column(Float, default=0.0)   # 本笔消费扣的赠送部分
    store:      Mapped[str] = mapped_column(String(40), default="")         # 当时门店短名
    note:       Mapped[str] = mapped_column(Text, default="")
    operator:   Mapped[str] = mapped_column(String(80), default="")         # 经办人 username
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    wallet   = relationship("Wallet",   foreign_keys=[wallet_id])
    customer = relationship("Customer", foreign_keys=[customer_id])


class PackageProduct(Base):
    """套餐商品（目录）：例如 美容套餐 10 次卡 ¥800。"""
    __tablename__ = "package_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:        Mapped[str] = mapped_column(String(120), default="")
    category:    Mapped[str] = mapped_column(String(40),  default="beauty")  # beauty/bath/medical/other
    total_uses:  Mapped[int] = mapped_column(Integer, default=10)            # 包次卡总次数
    sell_price:  Mapped[float] = mapped_column(Float, default=0.0)
    # 单次抵扣的服务参考价（导出/报表用）
    unit_price:  Mapped[float] = mapped_column(Float, default=0.0)
    validity_days: Mapped[int] = mapped_column(Integer, default=365)         # 0 = 无限期
    is_active:   Mapped[bool]  = mapped_column(Boolean, default=True)
    notes:       Mapped[str]   = mapped_column(Text, default="")
    # 多门店分离：空 = 通用套餐两店共享，"东环店" / "横岗店" = 仅该店
    store:       Mapped[str]   = mapped_column(String(40), default="")
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CustomerPackage(Base):
    """客户已购套餐（实例）：1 张包次卡。"""
    __tablename__ = "customer_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    product_id  = mapped_column(ForeignKey("package_products.id", ondelete="SET NULL"), nullable=True, default=None)

    # 售卖时快照（防止 product 改名/改价后影响历史）
    name:       Mapped[str]   = mapped_column(String(120), default="")
    category:   Mapped[str]   = mapped_column(String(40),  default="")
    total_uses: Mapped[int]   = mapped_column(Integer, default=10)
    used_count: Mapped[int]   = mapped_column(Integer, default=0)
    sell_price: Mapped[float] = mapped_column(Float, default=0.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)

    purchase_date: Mapped[str] = mapped_column(String(20), default="")
    expires_at:    Mapped[str] = mapped_column(String(20), default="")        # 空 = 无限期
    # status: active / exhausted / expired / refunded
    status:    Mapped[str] = mapped_column(String(20), default="active")
    store:     Mapped[str] = mapped_column(String(40), default="")
    operator:  Mapped[str] = mapped_column(String(80), default="")
    invoice_id = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, default=None)
    note:      Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer",       foreign_keys=[customer_id])
    pet      = relationship("Pet",            foreign_keys=[pet_id])
    product  = relationship("PackageProduct", foreign_keys=[product_id])


class PackageRedemption(Base):
    """套餐核销：每次扣 1 次的流水。"""
    __tablename__ = "package_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_package_id = mapped_column(ForeignKey("customer_packages.id", ondelete="CASCADE"), nullable=False)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="SET NULL"), nullable=True, default=None)
    invoice_id  = mapped_column(ForeignKey("invoices.id",  ondelete="SET NULL"), nullable=True, default=None)
    used_count:  Mapped[int]   = mapped_column(Integer, default=1)
    remaining_after: Mapped[int] = mapped_column(Integer, default=0)
    store:       Mapped[str]   = mapped_column(String(40), default="")
    operator:    Mapped[str]   = mapped_column(String(80), default="")
    note:        Mapped[str]   = mapped_column(Text, default="")
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Deposit(Base):
    """业务押金：手术押金、寄养押金等。关联具体业务实体。"""
    __tablename__ = "deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    # 关联到具体业务（二选一）
    appointment_id = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True, default=None)
    visit_id       = mapped_column(ForeignKey("visits.id",       ondelete="SET NULL"), nullable=True, default=None)
    # category: surgery / boarding / beauty / other
    category:   Mapped[str]   = mapped_column(String(40), default="surgery")
    amount:     Mapped[float] = mapped_column(Float, default=0.0)
    pay_method: Mapped[str]   = mapped_column(String(40), default="cash")
    # status: held（已收待结算）/ applied（已抵扣到收费单）/ refunded（已退款）/ partial_refund
    status:     Mapped[str]   = mapped_column(String(20), default="held")
    # 抵扣到的收费单
    applied_invoice_id = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, default=None)
    applied_amount:    Mapped[float] = mapped_column(Float, default=0.0)
    refunded_amount:   Mapped[float] = mapped_column(Float, default=0.0)
    refunded_at:       Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    store:      Mapped[str] = mapped_column(String(40), default="")
    operator:   Mapped[str] = mapped_column(String(80), default="")
    note:       Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer    = relationship("Customer",    foreign_keys=[customer_id])
    pet         = relationship("Pet",         foreign_keys=[pet_id])
    appointment = relationship("Appointment", foreign_keys=[appointment_id])
    visit       = relationship("Visit",       foreign_keys=[visit_id])


class Payment(Base):
    """收款明细：一张收费单可以拆成多笔（混合支付）。
    Invoice.total_amount = sum(Payment.amount where status=success)。
    """
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    # method: cash / wechat / alipay / shouqianba / meituan / third_party /
    #         wallet / package / deposit / coupon
    method:  Mapped[str]   = mapped_column(String(20), default="cash")
    amount:  Mapped[float] = mapped_column(Float, default=0.0)
    # 关联引用：method 决定 ref_id 指向哪个表
    #   wallet  → WalletTransaction.id
    #   package → CustomerPackage.id（同时记 PackageRedemption）
    #   deposit → Deposit.id
    #   coupon  → Coupon.id
    #   其他    → NULL
    ref_id:   Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    ref_no:   Mapped[str] = mapped_column(String(120), default="")   # 外部流水号（微信/支付宝/收钱吧 等）
    status:   Mapped[str] = mapped_column(String(20), default="success")  # success / cancelled
    store:    Mapped[str] = mapped_column(String(40), default="")
    operator: Mapped[str] = mapped_column(String(80), default="")
    note:     Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    invoice  = relationship("Invoice",  foreign_keys=[invoice_id])
    customer = relationship("Customer", foreign_keys=[customer_id])


class ConsentTemplate(Base):
    """协议/同意书模板。后台维护、富文本正文、支持 {{变量}} 占位。"""
    __tablename__ = "consent_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:     Mapped[str] = mapped_column(String(120), default="")   # 如 "麻醉知情同意书"
    category: Mapped[str] = mapped_column(String(40),  default="general")
    # category: anesthesia/surgery/vaccination/euthanasia/boarding/general
    body_html: Mapped[str] = mapped_column(Text, default="")          # Quill 输出的 HTML 正文
    # 占位符使用 {{pet_name}} / {{cust_name}} / {{visit_date}} / {{vet_name}} / {{date}} 等
    # 发起任务时按 fields 自动替换并保存到 ConsentTask.snapshot_html
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes:     Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConsentTask(Base):
    """协议签署任务：给指定客户发的 1 次签署请求。"""
    __tablename__ = "consent_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id = mapped_column(ForeignKey("consent_templates.id", ondelete="SET NULL"), nullable=True, default=None)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="SET NULL"), nullable=True, default=None)

    title:         Mapped[str] = mapped_column(String(120), default="")
    # 发起任务时把模板正文 + 变量替换一次性快照，避免模板后续改了影响历史
    snapshot_html: Mapped[str] = mapped_column(Text, default="")
    # 客户端访问凭证（无登录链接）
    token:         Mapped[str] = mapped_column(String(40), default="", index=True)
    # status: pending / signed / cancelled / expired
    status:        Mapped[str] = mapped_column(String(20), default="pending")
    # 客户端签名数据（base64 PNG，签后立刻存）
    signature_path: Mapped[str] = mapped_column(String(500), default="")
    signed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    signed_ip:     Mapped[str] = mapped_column(String(60), default="")
    expires_at:    Mapped[str] = mapped_column(String(20), default="")   # YYYY-MM-DD 空=不限期

    store:         Mapped[str] = mapped_column(String(40), default="")
    initiated_by:  Mapped[str] = mapped_column(String(80), default="")    # 发起人 username
    initiated_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes:         Mapped[str] = mapped_column(Text, default="")

    customer = relationship("Customer", foreign_keys=[customer_id])
    pet      = relationship("Pet",      foreign_keys=[pet_id])
    visit    = relationship("Visit",    foreign_keys=[visit_id])
    template = relationship("ConsentTemplate", foreign_keys=[template_id])


class ConsentDocument(Base):
    """签署完成后归档的 PDF 文档（一对一 ConsentTask）。"""
    __tablename__ = "consent_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id     = mapped_column(ForeignKey("consent_tasks.id", ondelete="CASCADE"), nullable=False, unique=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="SET NULL"), nullable=True, default=None)

    pdf_path:     Mapped[str] = mapped_column(String(500), default="")
    pdf_size:     Mapped[int] = mapped_column(Integer, default=0)
    title:        Mapped[str] = mapped_column(String(120), default="")
    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConsentAuditLog(Base):
    """协议签署审计日志：每一步操作都记录，作为打官司时的证据链。
    打开链接 / 发验证码 / 验证手机号 / 提交签字 / 失败原因 等都记。
    任何字段都不应允许修改（仅追加）。
    """
    __tablename__ = "consent_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id = mapped_column(ForeignKey("consent_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    # event:
    #   link_opened       客户打开链接（GET /consent/{token}）
    #   code_sent         发短信验证码
    #   code_verify_ok    验证码 / 手机号 校验通过
    #   code_verify_fail  校验失败
    #   sign_submit       提交签字（payload size、签字图哈希）
    #   sign_success      签字成功落档
    #   sign_fail         签字失败（含错误原因）
    event:       Mapped[str] = mapped_column(String(40), default="")
    ip:          Mapped[str] = mapped_column(String(60), default="")
    user_agent:  Mapped[str] = mapped_column(String(500), default="")
    # 验证时输入的手机号（脱敏：仅前 3 + 后 4，例 138****1234）
    phone_masked: Mapped[str] = mapped_column(String(20), default="")
    # 文档正文 snapshot_html 的 SHA256（防文档被改后说当时签的是另一版）
    doc_sha256:  Mapped[str] = mapped_column(String(64), default="")
    # 签字 PNG 的 SHA256
    sig_sha256:  Mapped[str] = mapped_column(String(64), default="")
    # session 标识（防重放）— 用 session_id 的 SHA256，不存原值
    session_hash: Mapped[str] = mapped_column(String(64), default="")
    # 备注 / 错误原因
    note:        Mapped[str] = mapped_column(Text, default="")
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Coupon(Base):
    """优惠券：自家发放、自家核销。
    kind:
      cash      — 面额抵扣（face_value 元，满 min_amount 可用）
      discount  — 折扣（discount_pct=0.9 = 9 折；满 min_amount 可用）
      free_item — 兑换券（如 免费洗澡 1 次，face_value 当参考价）
    """
    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # 系统生成或自填，唯一
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    # 留空 = 任意客户可用（通用券）；填了 = 仅指定客户可用
    title:     Mapped[str] = mapped_column(String(120), default="")
    kind:      Mapped[str] = mapped_column(String(20), default="cash")
    face_value:   Mapped[float] = mapped_column(Float, default=0.0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)   # 0.9 = 9 折
    min_amount:   Mapped[float] = mapped_column(Float, default=0.0)   # 最低消费门槛
    expires_at:   Mapped[str] = mapped_column(String(20), default="") # YYYY-MM-DD，空 = 不限期
    # status: issued / used / expired / cancelled
    status:    Mapped[str] = mapped_column(String(20), default="issued")
    used_invoice_id = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, default=None)
    used_amount: Mapped[float] = mapped_column(Float, default=0.0)
    used_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    issued_by:   Mapped[str] = mapped_column(String(80), default="")
    issued_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes:       Mapped[str] = mapped_column(Text, default="")
    store:       Mapped[str] = mapped_column(String(40), default="")

    customer = relationship("Customer", foreign_keys=[customer_id])


class FollowUp(Base):
    """回访任务：每条 Visit 自动衍生一条（visit_type 在规则里有 >0 天的才出）。

    status 流转：
      pending          → 计划中，未到日期
      due              → 到日期未发送（dispatch 扫到后会发）
      sent             → 已发送，等待客户反馈
      responded        → 客户已点反馈（看 response 判断是好转/需复诊）
      phone_pending    → 自动渠道全部失败 / 客户 48h 未点 → 转人工电话
      closed           → 已完成（好转/已联系/忽略）
    """
    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="CASCADE"), nullable=False, unique=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)

    store:        Mapped[str] = mapped_column(String(40), default="")   # 短名：东环店/横岗店
    assigned_to:  Mapped[str] = mapped_column(String(80), default="")   # 处理人（默认 visit.vet_name）
    planned_date: Mapped[str] = mapped_column(String(20), default="")   # 计划回访 YYYY-MM-DD

    status:       Mapped[str] = mapped_column(String(20), default="pending")
    channel:      Mapped[str] = mapped_column(String(20), default="")   # miniapp/sms/phone
    sent_at:      Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    response:     Mapped[str] = mapped_column(String(20), default="")   # recovered/needs_visit/no_reply
    response_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    response_note: Mapped[str] = mapped_column(Text, default="")

    feedback_token: Mapped[str] = mapped_column(String(32), default="", index=True)

    handled_by:   Mapped[str] = mapped_column(String(80), default="")
    handled_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    handle_note:  Mapped[str] = mapped_column(Text, default="")

    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    visit    = relationship("Visit",    foreign_keys=[visit_id])
    customer = relationship("Customer", foreign_keys=[customer_id])
    pet      = relationship("Pet",      foreign_keys=[pet_id])


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # role: 'superadmin' | 'staff'
    role: Mapped[str] = mapped_column(String(20), default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # store: '东环店' | '横岗店' | '' (空=不限，超级管理员)
    store: Mapped[str] = mapped_column(String(40), default="")
    # 显示名（医生真名）：回访任务按 display_name 匹配 Visit.vet_name，
    # 让"只看我的"功能正确生效。留空则回退到 username。
    display_name: Mapped[str] = mapped_column(String(80), default="")
    # 企业微信 userid（自建应用 OAuth 登录后绑定，员工在企微内免密进系统）
    wecom_userid: Mapped[str] = mapped_column(String(80), default="", index=True)
    # 企微通知偏好：CSV 字符串，存「不想收的事件 key」（默认空 = 全部都收）
    # 可选事件：tnr_pending / rabies_submitted / consent_signed / appointment_created / workbench_digest
    wecom_notify_disabled: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WecomCustomerLink(Base):
    """企业微信外部联系人 ↔ 我们系统 Customer 的映射。

    611 个企微客户同步进来时一对一建一条；按 remark_mobile 自动匹配 Customer。
    sync_status: matched(自动匹配上) / unmatched(待人工) / created(同步时新建客户) / ignored(明确跳过)
    """
    __tablename__ = "wecom_customer_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_userid: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    # 跟进员工的企微 userid（员工对客户的所属关系）
    follow_userid: Mapped[str] = mapped_column(String(80), default="", index=True)
    # 员工在企微里给客户起的备注名（最有用，比客户自己设的昵称准）
    remark_name:   Mapped[str] = mapped_column(String(120), default="")
    # 员工填写的备注手机号 — 主要匹配字段
    remark_mobile: Mapped[str] = mapped_column(String(40), default="", index=True)
    # 微信 unionid（需要绑定开发者ID才有，目前留空）
    unionid:       Mapped[str] = mapped_column(String(80), default="")
    # 客户微信昵称 + 头像（外部联系人自报）
    name:          Mapped[str] = mapped_column(String(120), default="")
    avatar:        Mapped[str] = mapped_column(String(500), default="")
    sync_status:   Mapped[str] = mapped_column(String(20), default="unmatched", index=True)
    # 完整 API 返回 JSON，备查/未来字段扩展用
    raw_json:      Mapped[str] = mapped_column(Text, default="")
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", foreign_keys=[customer_id])


# ════════════════════════════════════════════════════════════════
# 麻醉单 + 麻醉/管控药台账
# 国标要求：麻醉单独立开（与处方分开）、双人复核签字、全生命周期可追溯
# ════════════════════════════════════════════════════════════════
class AnesthesiaOrder(Base):
    """麻醉单（独立于处方单）"""
    __tablename__ = "anesthesia_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id    = mapped_column(ForeignKey("visits.id",    ondelete="SET NULL"), nullable=True, default=None)
    customer_id = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, default=None)
    pet_id      = mapped_column(ForeignKey("pets.id",      ondelete="SET NULL"), nullable=True, default=None)
    anesth_date: Mapped[str] = mapped_column(String(20), default="")
    asa_grade:   Mapped[str] = mapped_column(String(10), default="")
    vet_name:    Mapped[str] = mapped_column(String(80), default="")
    cosigner:    Mapped[str] = mapped_column(String(80), default="")
    start_time:  Mapped[str] = mapped_column(String(10), default="")
    end_time:    Mapped[str] = mapped_column(String(10), default="")
    recovery:    Mapped[str] = mapped_column(String(40), default="")
    status:      Mapped[str] = mapped_column(String(20), default="issued")
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    store:       Mapped[str] = mapped_column(String(40), default="")
    notes:       Mapped[str] = mapped_column(Text, default="")
    created_by:  Mapped[str] = mapped_column(String(80), default="")
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = relationship("AnesthesiaOrderItem", back_populates="order", cascade="all, delete-orphan")
    customer = relationship("Customer", foreign_keys=[customer_id])
    pet      = relationship("Pet",      foreign_keys=[pet_id])


class AnesthesiaOrderItem(Base):
    __tablename__ = "anesthesia_order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("anesthesia_orders.id", ondelete="CASCADE"))
    item_id   = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True, default=None)
    drug_name:     Mapped[str] = mapped_column(String(120), default="")
    route:         Mapped[str] = mapped_column(String(20), default="IV")
    concentration: Mapped[str] = mapped_column(String(40), default="")
    dose_amount:   Mapped[float] = mapped_column(Float, default=0.0)
    dose_unit:     Mapped[str] = mapped_column(String(20), default="mg")
    total_qty:     Mapped[float] = mapped_column(Float, default=0.0)
    total_unit:    Mapped[str] = mapped_column(String(20), default="")
    unit_price:    Mapped[float] = mapped_column(Float, default=0.0)
    subtotal:      Mapped[float] = mapped_column(Float, default=0.0)
    is_service:    Mapped[bool] = mapped_column(Boolean, default=False)
    note:          Mapped[str] = mapped_column(String(200), default="")

    order = relationship("AnesthesiaOrder", back_populates="items")
    inventory_item = relationship("InventoryItem", foreign_keys=[item_id])


class NarcoticsLedger(Base):
    """麻醉/管控药台账：自动 + 手动 双源汇总，国标月度盘点用。"""
    __tablename__ = "narcotics_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_date: Mapped[str] = mapped_column(String(20), default="", index=True)
    item_id     = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True, default=None)
    item_name:  Mapped[str] = mapped_column(String(120), default="", index=True)
    direction:  Mapped[str] = mapped_column(String(10), default="out")
    source:     Mapped[str] = mapped_column(String(30), default="manual")
    # anesth_order / manual_refill / manual_consume / stocktake / loss
    qty:        Mapped[float] = mapped_column(Float, default=0.0)
    unit:       Mapped[str] = mapped_column(String(20), default="")
    balance_after: Mapped[float] = mapped_column(Float, default=0.0)
    operator:   Mapped[str] = mapped_column(String(80), default="")
    cosigner:   Mapped[str] = mapped_column(String(80), default="")
    visit_id        = mapped_column(ForeignKey("visits.id",            ondelete="SET NULL"), nullable=True, default=None)
    anesth_order_id = mapped_column(ForeignKey("anesthesia_orders.id", ondelete="SET NULL"), nullable=True, default=None)
    store:      Mapped[str] = mapped_column(String(40), default="", index=True)
    notes:      Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    inventory_item = relationship("InventoryItem", foreign_keys=[item_id])
    visit          = relationship("Visit",         foreign_keys=[visit_id])
    anesth_order   = relationship("AnesthesiaOrder", foreign_keys=[anesth_order_id])
