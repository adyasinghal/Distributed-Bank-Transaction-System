"""Timestamps in IST."""

from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> str:
    """Return the current time as an ISO-8601 string in IST."""
    return datetime.now(IST).isoformat()


def now_ist_dt() -> datetime:
    """Return the current datetime in IST."""
    return datetime.now(IST)
