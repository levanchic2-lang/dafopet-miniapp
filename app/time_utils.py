"""Application-wide timezone helpers.

Database ``DateTime`` values are stored as UTC.  SQLite normally returns them
as naive ``datetime`` objects, so naive values are deliberately interpreted as
UTC here.  User-entered date/time strings (appointments, visit dates, analyzer
timestamps, etc.) are local business values and must not be passed through
these helpers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


UTC = timezone.utc
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def to_beijing(value: datetime | None) -> datetime | None:
    """Convert a stored UTC datetime to an aware Beijing datetime."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BEIJING_TZ)


def format_beijing(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a stored UTC datetime in Beijing time."""
    converted = to_beijing(value)
    return converted.strftime(fmt) if converted else ""


def beijing_now() -> datetime:
    """Return the current timezone-aware Beijing datetime."""
    return datetime.now(BEIJING_TZ)


def beijing_today() -> date:
    return beijing_now().date()
