from datetime import datetime, timezone

from app.time import parse_iso_datetime


def is_live(value: str | None, since: datetime) -> bool:
    """Only records published since process startup can generate live alerts."""
    if not value:
        return False
    try:
        published = parse_iso_datetime(value)
    except (TypeError, ValueError):
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)  # noqa: UP017 - Python 3.10
    return published >= since
