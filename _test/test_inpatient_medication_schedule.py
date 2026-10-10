"""Focused regression tests for flexible vs exact inpatient medication times."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.inpatient_medication_schedule import (
    flexible_schedule_for_frequency,
    medication_due_at,
    medication_is_overdue,
    medication_schedule_label,
)


def row(at: datetime, schedule_times: str = ""):
    item = SimpleNamespace(schedule_times=schedule_times)
    return SimpleNamespace(scheduled_at=at, prescription_item=item, status="pending")


assert flexible_schedule_for_frequency("QD", 9) == [(8, 0)]
assert flexible_schedule_for_frequency("QD", 15) == [(14, 0)]
assert flexible_schedule_for_frequency("BID", 9) == [(8, 0), (20, 0)]
assert flexible_schedule_for_frequency("每日3次", 9) == [(8, 0), (14, 0), (20, 0)]

morning = row(datetime(2026, 10, 10, 8, 0))
assert medication_schedule_label(morning) == "上午"
assert medication_due_at(morning) == datetime(2026, 10, 10, 13, 0)
assert not medication_is_overdue(morning, datetime(2026, 10, 10, 12, 59))
assert medication_is_overdue(morning, datetime(2026, 10, 10, 13, 0))

evening = row(datetime(2026, 10, 10, 20, 0))
assert medication_schedule_label(evening) == "晚上"
assert medication_due_at(evening) == datetime(2026, 10, 11, 0, 0)

exact = row(datetime(2026, 10, 10, 8, 30), "08:30,20:00")
assert medication_schedule_label(exact) == "08:30"
assert medication_due_at(exact) == datetime(2026, 10, 10, 8, 30)
assert not medication_is_overdue(exact, datetime(2026, 10, 10, 8, 29))
assert medication_is_overdue(exact, datetime(2026, 10, 10, 8, 30))

print("inpatient medication schedule tests passed")
