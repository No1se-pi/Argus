"""Validate source values before they can become stars or calendar report dates."""

from app.time import parse_iso_datetime


def review_rating(value) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid review rating")
    rating = int(value)
    if not 1 <= rating <= 5 or float(value) != rating:
        raise ValueError("Review rating must be an integer from 1 to 5")
    return rating


def review_date(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing review date")
    date = parse_iso_datetime(value.strip().replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Review date must include timezone")
    return date.isoformat()
