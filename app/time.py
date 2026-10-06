import logging
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)
UTC = timezone.utc
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")


def resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            logger.warning("IANA tzdata unavailable; using fixed Europe/Moscow UTC+3 fallback")
            return MOSCOW
        logger.error("Timezone %s unavailable; using UTC", name)
        return UTC


def local_now(name: str) -> datetime:
    return datetime.now(resolve_timezone(name))


def local_date_iso(name: str) -> str:
    return local_now(name).date().isoformat()
