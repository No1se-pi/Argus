import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class Period:
    label: str
    start: datetime
    end: datetime

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()


_PERIOD_RE = re.compile(r"^(?P<count>\d+)(?P<unit>[hdw])$")


def parse_period(value: str) -> Period:
    normalized = value.strip().lower()
    match = _PERIOD_RE.match(normalized)
    if not match:
        raise ValueError("Use a period like 24h, 7d, 2w, or 30d.")

    count = int(match.group("count"))
    unit = match.group("unit")
    if count <= 0:
        raise ValueError("Period must be greater than zero.")

    multiplier = {
        "h": timedelta(hours=1),
        "d": timedelta(days=1),
        "w": timedelta(weeks=1),
    }[unit]
    end = datetime.now(UTC)
    return Period(label=normalized, start=end - multiplier * count, end=end)
