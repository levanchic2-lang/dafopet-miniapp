from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationStatus,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
)


ACTIVE_APPOINTMENT_STATUSES = (
    AppointmentStatus.pending.value,
    AppointmentStatus.confirmed.value,
    AppointmentStatus.arrived.value,
)


def get_latest_active_tnr_appointment(
    db: Session,
    application_id: int,
) -> Appointment | None:
    """Return the latest unfinished TNR appointment linked to an application."""
    return (
        db.query(Appointment)
        .filter(
            Appointment.related_application_id == application_id,
            Appointment.category == AppointmentCategory.tnr.value,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        )
        .order_by(
            Appointment.appointment_date.desc(),
            Appointment.appointment_time.desc(),
            Appointment.id.desc(),
        )
        .first()
    )


def sync_active_tnr_appointment(
    db: Session,
    application_id: int,
    target_status: str,
    note: str,
) -> dict | None:
    """Advance the current appointment without rewriting terminal history."""
    appointment = get_latest_active_tnr_appointment(db, application_id)
    if appointment is None:
        return None

    old_status = appointment.status
    appointment.status = target_status
    appointment.updated_at = datetime.utcnow()

    marker = f"[TNR联动] {note.strip()}"
    existing_notes = (appointment.notes or "").strip()
    if marker not in existing_notes:
        appointment.notes = f"{existing_notes}\n{marker}".strip()

    return {
        "appointment_id": appointment.id,
        "appointment_status_from": old_status,
        "appointment_status_to": target_status,
        "appointment_sync_note": note,
    }


def target_status_for_application(application: Application) -> tuple[str, str] | None:
    """Map a completed application-side fact to its appointment outcome."""
    if application.status == ApplicationStatus.surgery_completed.value:
        return AppointmentStatus.completed.value, "手术已完成，预约自动完成"
    if application.status == ApplicationStatus.rejected.value:
        if application.staff_cat_verified:
            return AppointmentStatus.completed.value, "已到店核验，但未实施手术，预约流程已结束"
        return AppointmentStatus.cancelled.value, "申请已拒绝，预约自动取消"
    if application.status == ApplicationStatus.cancelled.value:
        return AppointmentStatus.cancelled.value, "申请已取消，预约自动取消"
    if application.status == ApplicationStatus.no_show.value:
        return AppointmentStatus.no_show.value, "申请已标记爽约，预约同步爽约"
    if (
        application.status == ApplicationStatus.arrived_verified.value
        or application.staff_cat_verified
    ):
        return AppointmentStatus.arrived.value, "已现场核验猫咪，预约自动标记到店"
    return None
