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


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # role: 'superadmin' | 'staff'
    role: Mapped[str] = mapped_column(String(20), default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
