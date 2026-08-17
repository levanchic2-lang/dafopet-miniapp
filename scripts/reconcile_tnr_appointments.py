"""Repair unfinished TNR appointments whose application already reached a later state.

Dry-run by default. Pass --apply to persist changes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.models import Application  # noqa: E402
from app.services.tnr_appointment_sync import (  # noqa: E402
    get_latest_active_tnr_appointment,
    sync_active_tnr_appointment,
    target_status_for_application,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist repairs (default: dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    repairs: list[tuple[int, int, str, str, str, str]] = []
    try:
        applications = db.query(Application).order_by(Application.id).all()
        for application in applications:
            target = target_status_for_application(application)
            if target is None:
                continue
            target_status, note = target
            appointment = get_latest_active_tnr_appointment(db, application.id)
            if appointment is None:
                continue
            repairs.append(
                (
                    application.id,
                    appointment.id,
                    appointment.status,
                    target_status,
                    application.cat_nickname or "",
                    note,
                )
            )
            if args.apply:
                sync_active_tnr_appointment(
                    db,
                    application.id,
                    target_status,
                    note,
                )

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] Found {len(repairs)} TNR appointment(s) to reconcile.")
        for app_id, appt_id, old, new, cat_name, note in repairs:
            print(
                f"  application#{app_id} appointment#{appt_id} "
                f"{old} -> {new} cat={cat_name or '-'} reason={note}"
            )

        if args.apply:
            db.commit()
            print(f"Applied {len(repairs)} repair(s).")
        else:
            db.rollback()
            print("No changes written. Re-run with --apply after reviewing the list.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
