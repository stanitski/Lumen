from __future__ import annotations

from datetime import datetime
from datetime import timezone as datetime_timezone


LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or datetime_timezone.utc


def local_now() -> datetime:
    return datetime.now(LOCAL_TIMEZONE)


def local_now_iso() -> str:
    return local_now().isoformat()
