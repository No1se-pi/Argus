from datetime import datetime, timezone, timedelta
try:
    from dateutil.parser import isoparse
except ImportError:
    isoparse = None

MSK_TZ = timezone(timedelta(hours=3))

def format_msk_datetime(dt_val: str | datetime | None) -> str:
    """Format an ISO datetime string or datetime object into human-readable Moscow time (UTC+3):
    'ДД.ММ.ГГГГ ЧЧ:ММ:СС МСК'. Eliminates milliseconds and microseconds.
    """
    if not dt_val:
        return "—"
    try:
        if isinstance(dt_val, datetime):
            dt = dt_val
        elif isoparse is not None:
            dt = isoparse(str(dt_val).strip())
        else:
            s = str(dt_val).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        dt_msk = dt.astimezone(MSK_TZ)
        return dt_msk.strftime("%d.%m.%Y %H:%M:%S МСК")
    except Exception:
        return str(dt_val)


def parse_utc_datetime(dt_val: str | datetime | None) -> datetime | None:
    """Parse an ISO datetime string or datetime object into a timezone-aware UTC datetime.
    Returns None if parsing fails or input is empty.
    """
    if not dt_val:
        return None
    try:
        if isinstance(dt_val, datetime):
            dt = dt_val
        elif isoparse is not None:
            dt = isoparse(str(dt_val).strip())
        else:
            s = str(dt_val).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
