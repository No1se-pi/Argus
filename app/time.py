import logging
import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)
UTC = timezone.utc
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")


def parse_iso_datetime(value: str) -> datetime:
    """Python 3.10 also accepts API timestamps with 1/2/4/5 fractional digits."""
    normalized = value.strip().replace("Z", "+00:00")
    normalized = re.sub(r"\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), normalized)
    return datetime.fromisoformat(normalized)


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


def period_utc_bounds(name: str, days: int) -> tuple[str, str]:
    zone = resolve_timezone(name)
    now = datetime.now(zone)
    start_date = now.date() - timedelta(days=max(1, days) - 1)
    start = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
    return start.isoformat(), now.astimezone(UTC).isoformat()
