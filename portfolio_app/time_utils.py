from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


CHINA_STANDARD_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")


def current_valuation_date(now: datetime | None = None) -> date:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(CHINA_STANDARD_TIME).date()
