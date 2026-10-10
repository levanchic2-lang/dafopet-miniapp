"""Shared schedule rules for inpatient medication tasks.

An empty ``PrescriptionItem.schedule_times`` means the doctor prescribed a
frequency, not an exact clock time. Those tasks use broad work periods. A
non-empty, valid schedule keeps exact-time behaviour.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta


def parse_schedule_times(value: str) -> list[tuple[int, int]]:
    """Parse comma-separated 24-hour times, skipping invalid entries."""
    result: list[tuple[int, int]] = []
    for chunk in (value or "").replace("，", ",").replace("、", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            if ":" in text:
                hour_text, minute_text = text.split(":", 1)
                hour, minute = int(hour_text), int(minute_text)
            else:
                hour, minute = int(text), 0
        except (TypeError, ValueError):
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            result.append((hour, minute))
    return result


def frequency_count(frequency: str, times_per_day: float = 0) -> int:
    """Return the integer daily frequency used by the flexible scheduler."""
    try:
        numeric = float(times_per_day or 0)
        if numeric.is_integer() and numeric > 0:
            return int(numeric)
    except (TypeError, ValueError):
        pass

    normalized = (frequency or "").strip().lower().replace(" ", "")
    named = {
        "qd": 1, "sid": 1, "q24h": 1,
        "bid": 2, "q12h": 2,
        "tid": 3, "q8h": 3,
        "qid": 4, "q6h": 4,
    }
    if normalized in named:
        return named[normalized]
    match = re.search(r"(?:每日|每天|一天)?(\d+(?:\.\d+)?)次(?:/天)?", normalized)
    raw = match.group(1) if match else (normalized if re.fullmatch(r"\d+(?:\.\d+)?", normalized) else "")
    try:
        numeric = float(raw)
        return int(numeric) if numeric.is_integer() and numeric > 0 else 0
    except (TypeError, ValueError):
        return 0


def flexible_schedule_for_frequency(
    frequency: str, opened_hour: int, times_per_day: float = 0,
) -> list[tuple[int, int]]:
    """Return internal anchors for broad periods; these are not UI clock times."""
    count = frequency_count(frequency, times_per_day)
    if count == 1:
        if 0 <= opened_hour < 12:
            return [(8, 0)]
        if opened_hour < 18:
            return [(14, 0)]
        return [(20, 0)]
    if count == 2:
        return [(8, 0), (20, 0)]
    if count == 3:
        return [(8, 0), (14, 0), (20, 0)]
    if count == 4:
        return [(8, 0), (12, 0), (16, 0), (20, 0)]
    return []


def has_explicit_schedule(item) -> bool:
    return bool(item and parse_schedule_times(getattr(item, "schedule_times", "") or ""))


def flexible_period_label(value: datetime) -> str:
    if value.hour < 11:
        return "上午"
    if value.hour < 14:
        return "中午"
    if value.hour < 18:
        return "下午"
    return "晚上"


def medication_schedule_label(log) -> str:
    item = getattr(log, "prescription_item", None)
    scheduled_at = getattr(log, "scheduled_at", None)
    if not scheduled_at:
        return "待安排"
    if has_explicit_schedule(item):
        return scheduled_at.strftime("%H:%M")
    return flexible_period_label(scheduled_at)


def medication_due_at(log) -> datetime | None:
    """Return when a task becomes overdue, not when its period begins."""
    scheduled_at = getattr(log, "scheduled_at", None)
    if not scheduled_at:
        return None
    item = getattr(log, "prescription_item", None)
    if has_explicit_schedule(item):
        return scheduled_at

    day_start = scheduled_at.replace(hour=0, minute=0, second=0, microsecond=0)
    if scheduled_at.hour < 11:
        return day_start.replace(hour=13)
    if scheduled_at.hour < 14:
        return day_start.replace(hour=15)
    if scheduled_at.hour < 18:
        return day_start.replace(hour=19)
    return day_start + timedelta(days=1)


def medication_is_overdue(log, now_local: datetime, grace_minutes: int = 0) -> bool:
    if getattr(log, "status", "pending") != "pending":
        return False
    due_at = medication_due_at(log)
    return bool(due_at and due_at + timedelta(minutes=max(0, grace_minutes)) <= now_local)
