"""Read one consistent database snapshot without holding up the running bot."""

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from app.time import parse_iso_datetime, resolve_timezone

UTC = timezone.utc


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    label: str
    timezone_name: str = "Europe/Moscow"

    @property
    def comparison_start(self) -> datetime:
        zone = resolve_timezone(self.timezone_name)
        start = self.start.astimezone(zone)
        days = (self.end.astimezone(zone).date() - start.date()).days
        return (start - timedelta(days=days)).astimezone(UTC)

    @property
    def comparison_label(self) -> str:
        zone = resolve_timezone(self.timezone_name)
        first = self.comparison_start.astimezone(zone).date()
        last = self.start.astimezone(zone).date() - timedelta(days=1)
        return f"{first:%d.%m.%Y}–{last:%d.%m.%Y}"

    @classmethod
    def parse(cls, value: str, timezone_name: str = "Europe/Moscow"):
        parts = value.split()
        if len(parts) != 2:
            raise ValueError("Укажите две даты: 12.10.2026 19.10.2026")
        dates = []
        for part in parts:
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    dates.append(datetime.strptime(part, fmt).date())
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("Даты должны иметь формат ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")
        if dates[1] < dates[0] or (dates[1] - dates[0]).days > 366:
            raise ValueError("Период должен составлять от одного дня до года.")
        zone = resolve_timezone(timezone_name)
        if dates[1] > datetime.now(zone).date():
            raise ValueError("Дата окончания не может быть в будущем.")
        return cls(
            datetime.combine(dates[0], time.min, zone).astimezone(UTC),
            datetime.combine(dates[1] + timedelta(days=1), time.min, zone).astimezone(UTC),
            f"{dates[0]:%d.%m.%Y}–{dates[1]:%d.%m.%Y}",
            timezone_name,
        )


def select_vk_report(reports: list[dict], period: Period) -> tuple[dict | None, list[str]]:
    """Derive counts from the post-level evidence of a covering saved collection."""
    now = datetime.now(UTC)
    for parent in reversed(reports):
        start, end = parent.get("period_start"), parent.get("period_end_exclusive")
        if not start or not end:
            continue  # A last-N collection does not prove coverage of a calendar period.
        first, last = parse_iso_datetime(start), parse_iso_datetime(end)
        if first > period.start:
            continue
        if last < period.end and period.end <= now:
            continue
        if last <= period.start:
            continue

        def post_date(post):
            value = post["date"]
            return (
                datetime.fromtimestamp(value, UTC)
                if isinstance(value, (int, float))
                else parse_iso_datetime(value)
            )

        posts = [post for post in parent["posts"] if period.start <= post_date(post) < period.end]
        counts = Counter(user for post in posts for user in set(post["user_ids"]))
        profiles = {row["user_id"]: row for row in parent["rows"]}
        if not counts.keys() <= profiles.keys():
            raise ValueError("Сохранённый сбор ВК не содержит имён всех участников.")
        report = {
            **parent,
            "posts": posts,
            "posts_count": len(posts),
            "rows": [{**profiles[user], "likes": count} for user, count in counts.items()],
            "period_start": period.start.isoformat(),
            "period_end_exclusive": min(period.end, last).isoformat(),
            "label": period.label,
        }
        warnings = []
        if last < min(period.end, now):
            warnings.append(f"ВК: сохранённый сбор покрывает данные только до {last.isoformat()}.")
        return report, warnings
    return None, ["Нет сохранённого сбора ВК, покрывающего период. Значения лайков — н/д."]


def read_snapshot(path: Path, period: Period) -> dict:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        sources = [
            dict(row)
            for row in connection.execute("SELECT * FROM review_sources ORDER BY platform,id")
        ]
        reviews = [
            dict(row)
            for row in connection.execute(
                """SELECT r.*,s.branch_name,a.sentiment FROM reviews r
               JOIN review_sources s ON s.id=r.source_id
               LEFT JOIN review_analysis a ON a.review_id=r.id
               WHERE julianday(r.published_at)>=julianday(?)
               AND julianday(r.published_at)<julianday(?) ORDER BY r.published_at,r.id LIMIT 501""",
                (period.start.isoformat(), period.end.isoformat()),
            )
        ]
        if len(reviews) > 500:
            raise ValueError("Найдено больше 500 отзывов. Выберите более короткий период.")
        warnings = []
        captured_at = datetime.now(UTC)
        if period.end > captured_at:
            warnings.append(
                "Период включает незавершённый день: данные приведены на момент сбора; "
                "позднее могут появиться новые отзывы."
            )
        for source in sources:
            verified = connection.execute(
                """SELECT checked_at, period_start FROM review_history_coverage WHERE source_id=?
                   AND julianday(period_start)<=julianday(?)
                   AND julianday(period_end)>=julianday(?)""",
                (source["id"], period.start.isoformat(), period.end.isoformat()),
            ).fetchone()
            # Having one old record does not prove that intervening pages were read.
            source["coverage_complete"] = bool(verified)
            source["coverage_checked_at"] = verified[0] if verified else None
            source["comparison_covered"] = bool(
                verified and period.comparison_start < period.start
                and parse_iso_datetime(verified[1]) <= period.comparison_start
            )
            if not source["coverage_complete"]:
                warnings.append(
                    f"{source['platform']} / {source['branch_name']}: "
                    "история может не покрывать начало периода."
                )
            if source["last_error"] and not source["coverage_complete"]:
                warnings.append(
                    f"{source['platform']} / {source['branch_name']}: источник имеет ошибку сбора."
                )
            end = min(period.end, captured_at)
            for boundary, suffix in ((end, "end"), (period.start, "start")):
                row = connection.execute(
                    """SELECT rating,total_reviews_count,captured_at FROM review_rating_snapshots
                       WHERE source_id=? AND julianday(captured_at)<=julianday(?)
                       AND julianday(captured_at)>=julianday(?)
                       ORDER BY julianday(captured_at) DESC LIMIT 1""",
                    (source["id"], boundary.isoformat(), (boundary - timedelta(days=1)).isoformat()),
                ).fetchone()
                source[f"rating_at_{suffix}"] = row[0] if row else None
                source[f"rating_count_at_{suffix}"] = row[1] if row else None
                source[f"rating_captured_at_{suffix}"] = row[2] if row else None
            latest = connection.execute(
                """SELECT rating,total_reviews_count,captured_at FROM review_rating_snapshots
                   WHERE source_id=? ORDER BY julianday(captured_at) DESC LIMIT 1""",
                (source["id"],),
            ).fetchone()
            source["latest_rating"] = latest[0] if latest else None
            source["latest_rating_count"] = latest[1] if latest else None
            source["latest_rating_at"] = latest[2] if latest else None
            source["rating_change"] = None
            if (
                source["rating_at_end"] is not None and source["rating_at_start"] is not None
                and source["rating_captured_at_end"] != source["rating_captured_at_start"]
            ):
                source["rating_change"] = round(
                    source["rating_at_end"] - source["rating_at_start"], 2
                )
        previous_reviews = [
            dict(row)
            for row in connection.execute(
                """SELECT source_id, external_review_id, rating FROM reviews
                   WHERE julianday(published_at)>=julianday(?)
                   AND julianday(published_at)<julianday(?)""",
                (period.comparison_start.isoformat(), period.start.isoformat()),
            )
        ] if period.comparison_start < period.start else []
        reports = [
            json.loads(row[0])
            for row in connection.execute("SELECT payload_json FROM vk_like_reports ORDER BY id")
        ]
        vk, vk_warnings = select_vk_report(reports, period)
        warnings.extend(vk_warnings)
        cases = [
            dict(row)
            for row in connection.execute(
                """SELECT m.text,m.message_url,m.message_date,s.title AS source_title,
               a.severity,a.reason,a.categories_json FROM telegram_messages m
               JOIN sources s ON s.id=m.source_id
               JOIN telegram_message_analysis a ON a.message_id=m.id
               LEFT JOIN telegram_risk_alerts r ON r.message_id=m.id
               WHERE a.risk=1 AND a.severity>=2
               AND r.verdict='confirmed'
               AND julianday(m.message_date)>=julianday(?)
               AND julianday(m.message_date)<julianday(?) ORDER BY m.message_date LIMIT 101""",
                (period.start.isoformat(), period.end.isoformat()),
            )
        ]
        if len(cases) > 100:
            raise ValueError("Более 100 критических случаев. Сократите период отчёта.")
        bindings_row = connection.execute(
            "SELECT value FROM runtime_settings WHERE key='vk_likes_name_bindings'"
        ).fetchone()
        return {
            "sources": sources,
            "reviews": reviews,
            "comparison_reviews": previous_reviews,
            "cases": cases,
            "vk": vk,
            "identity_report": reports[-1] if reports else None,
            "bindings": json.loads(bindings_row[0]) if bindings_row else {},
            "warnings": warnings,
            "period": period.label,
            "period_start": period.start.isoformat(),
            "period_end_exclusive": period.end.isoformat(),
            "captured_at": datetime.now(UTC).isoformat(),
        }
    finally:
        connection.close()
