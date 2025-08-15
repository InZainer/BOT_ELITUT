from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone

from .loader import Activity

def month_in_season(activity: Activity, now: Optional[datetime] = None) -> bool:
    if not activity.months:
        return True
    now = now or datetime.now(timezone.utc)
    return now.month in activity.months

