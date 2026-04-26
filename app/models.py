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


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # role: 'superadmin' | 'staff'
    role: Mapped[str] = mapped_column(String(20), default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
