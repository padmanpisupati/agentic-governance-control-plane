"""Injectable clock so time windows and approval expiry are testable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Manually advanced clock for tests and deterministic demos."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # a Monday

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)
