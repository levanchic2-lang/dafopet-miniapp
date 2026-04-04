from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    showcase_consent: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    media: Mapped[List["MediaFile"]] = relationship(back_populates="application", cascade="all, delete-orphan")
    notifications: Mapped[List["NotificationLog"]] = relationship(back_populates="application", cascade="all, delete-orphan")


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    stored_path: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    application: Mapped["Application"] = relationship(back_populates="media")


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(40))  # email / log
    payload: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    application: Mapped["Application"] = relationship(back_populates="notifications")


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
