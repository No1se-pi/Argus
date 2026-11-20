"""Collect an explicit period on demand and build a complete activity deck."""

import asyncio
import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from app.reports.data import Period, read_snapshot
from app.reports.history import HistoricalReviewsLoader
from app.reports.identities import identity_csv
from app.reports.pptx import BRANCHES, Slide, TemplateDeck, branch_name, report_title
from app.reports.render import RenderedPage, ReportRenderer
from app.reports.review_details import load_review_images, review_details
from app.vk.client import VKAPIError, vk_error_text
from app.vk.likes import LikesSelection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityExport:
    directory: Path
    slides: int
    reviews: int
    posts: int | None
    warnings: tuple[str, ...]
    vk_error: str | None = None


def reviews_csv(snapshot: dict) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Площадка", "Отделение", "Автор", "Звёзды", "Дата", "Текст", "Ссылка", "ID"])
    for item in snapshot["reviews"]:
        values = [
            item.get(key, "")
            for key in (
                "platform",
                "branch_name",
                "author_name",
                "rating",
                "published_at",
                "text",
                "review_url",
                "external_review_id",
            )
        ]
        writer.writerow(
            [
                "'" + value
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                else value
                for value in values
            ]
        )
    return output.getvalue().encode("utf-8-sig")


class ActivityReportService:
    def __init__(self, database, workspace, timezone_name="Europe/Moscow", reviews_service=None):
        self.database = database
        self.workspace = workspace
        self.timezone_name = timezone_name
        self._lock = asyncio.Lock()
        self.history = (
            HistoricalReviewsLoader(reviews_service) if reviews_service is not None else None
        )
        self.reviews_service = reviews_service

    async def check_vk_access(self) -> None:
        await self.workspace.service.check_access()

    async def generate(self, period: Period, *, collect_vk: bool = False) -> ActivityExport:
        if self._lock.locked():
            raise ValueError("Презентация уже формируется. Дождитесь завершения текущей задачи.")
        async with self._lock:
            template = await self.workspace.template()
            if template is None:
                raise ValueError("Сначала загрузите исходный PPTX в меню ВК → Шаблон презентации.")
            vk_error = None
            fresh_vk = None
            if collect_vk:
                try:
                    fresh_vk = await self.workspace.service.collect(
                        LikesSelection(label=period.label, start=period.start, end=period.end)
                    )
                except VKAPIError as exc:
                    vk_error = vk_error_text(exc)
                    logger.warning("Activity VK collection failed method=%s code=%s",
                                   exc.method, exc.code)
                except ValueError as exc:
                    vk_error = str(exc)
                except Exception as exc:
                    logger.warning("Activity VK collection failed error=%s", type(exc).__name__)
                    vk_error = "Не удалось связаться с ВК. Проверьте подключение и повторите сбор."
            if self.reviews_service is not None:
                await self.reviews_service.ensure_default_sources()
            snapshot = await asyncio.to_thread(read_snapshot, self.database.path, period)
            if self.history is not None:
                warnings = await self.history.ensure(period, snapshot["sources"])
                snapshot = await asyncio.to_thread(read_snapshot, self.database.path, period)
                snapshot["warnings"].extend(warnings)
                verified = getattr(self.history, "verified_owners", {})
                for source in snapshot["sources"]:
                    if source["id"] in verified:
                        source["verified_owner"] = verified[source["id"]]
                complete = {source["id"] for source in snapshot["sources"]
                            if source["coverage_complete"]}
                # Retain archived records in SQLite, but do not publish disappeared reviews
                # as if they were present in the just-verified public listing.
                previous_count = len(snapshot["reviews"])
                snapshot["reviews"] = [
                    item for item in snapshot["reviews"]
                    if item["source_id"] not in complete
                    or item["external_review_id"] in self.history.observed.get(
                        item["source_id"], set()
                    )
                ]
                snapshot["excluded_cached_reviews"] = previous_count - len(snapshot["reviews"])
                snapshot["comparison_reviews"] = [
                    item for item in snapshot.get("comparison_reviews", ())
                    if item["source_id"] in complete
                    and item["external_review_id"] in self.history.observed.get(
                        item["source_id"], set()
                    )
                ]
            if collect_vk:
                # Use this run's result, never a stale report after a failed fresh request.
                snapshot["warnings"] = [
                    warning for warning in snapshot["warnings"]
                    if not warning.startswith(("ВК:", "Нет сохранённого сбора ВК"))
                ]
                snapshot["vk"] = fresh_vk
                snapshot["vk_collection_error"] = vk_error
                if vk_error:
                    snapshot["warnings"].append(f"Свежие лайки ВК не собраны: {vk_error}")
            return await self.generate_snapshot(template, snapshot)

    async def generate_snapshot(self, template: bytes, snapshot: dict) -> ActivityExport:
        snapshot = {
            **snapshot,
            "sources": [dict(source) for source in snapshot["sources"]],
            "reviews": [dict(review) for review in snapshot["reviews"]],
            "warnings": list(snapshot["warnings"]),
        }
        period = Period.parse(snapshot["period"].replace("–", " "), self.timezone_name)
        snapshot["comparison_period"] = period.comparison_label
        for review in snapshot["reviews"]:
            if "raw_payload_json" in review or "details" not in review:
                review["details"] = review_details(review)
            review.pop("raw_payload_json", None)
        for source in snapshot["sources"]:
            ratings = [
                item["rating"] for item in snapshot["reviews"]
                if item["source_id"] == source["id"] and item.get("rating") in (1, 2, 3, 4, 5)
            ]
            earlier = [
                item["rating"] for item in snapshot.get("comparison_reviews", ())
                if item["source_id"] == source["id"] and item.get("rating") in (1, 2, 3, 4, 5)
            ]
            source["period_review_count"] = len(ratings)
            source["period_average"] = (
                mean(ratings) if ratings and source.get("coverage_complete") else None
            )
            source["previous_period_average"] = (
                mean(earlier) if earlier and source.get("comparison_covered") else None
            )
            previous = source["previous_period_average"]
            current = source["period_average"]
            source["average_rating_change"] = (
                round(current - previous, 1) if current is not None and previous is not None
                else None
            )
        settings = self.reviews_service.settings if self.reviews_service is not None else None
        proxy = settings.reviews_proxy_url if settings is not None else None
        images, missing = await load_review_images(
            snapshot["reviews"], proxy.get_secret_value() if proxy else None
        )
        if missing:
            snapshot["warnings"].append(f"Не удалось добавить изображения отзывов: {missing}.")
        deck = await asyncio.to_thread(TemplateDeck, template, snapshot, self.timezone_name)
        yandex = [source for source in snapshot["sources"] if source["platform"] == "yandex"]
        dgis = [source for source in snapshot["sources"] if source["platform"] == "2gis"]
        slides = [Slide(deck.order[0], cover=True)]
        by_source: dict[int, list[dict]] = {}
        for review in snapshot["reviews"]:
            by_source.setdefault(review["source_id"], []).append(review)
        async with ReportRenderer(self.timezone_name) as renderer:
            for platform, sources, names in (
                (
                    "Яндекс Карты",
                    yandex,
                    ("РЕЙТИНГ УЧЕБНЫХ ОТДЕЛЕНИЙ Я.КАРТЫ", "ИНФОРМАЦИОННАЯ СПРАВКА ЯНДЕКС.КАРТЫ"),
                ),
                (
                    "2ГИС",
                    dgis,
                    ("РЕЙТИНГ УЧЕБНЫХ ОТДЕЛЕНИЙ 2ГИС", "ИНФОРМАЦИОННАЯ СПРАВКА 2ГИС"),
                ),
            ):
                rating_part, summary_part = (deck.find(name) for name in names)
                is_dgis = platform == "2ГИС"
                platform_key = "2gis" if is_dgis else "yandex"
                rating_images = await renderer.ratings(
                    deck.rating_branches(sources, dgis=is_dgis), platform=platform_key,
                    period=snapshot["period"],
                )
                ratings = [
                    Slide(rating_part, names[0], RenderedPage(image, []))
                    for image in rating_images
                ]
                summary = Slide(summary_part, summary=(sources, snapshot["reviews"]),
                                platform=platform_key)
                slides.extend([summary, *ratings] if is_dgis else [*ratings, summary])
                card_start = rating_part if is_dgis else summary_part
                prototype = deck.card_prototype(card_start)
                by_branch: dict[str, list[int]] = {}
                for source in sources:
                    by_branch.setdefault(branch_name(source), []).append(source["id"])
                branch_order = [*BRANCHES, *sorted(by_branch.keys() - set(BRANCHES))]
                have_reviews = False
                for branch in branch_order:
                    reviews = [
                        review for source_id in by_branch.get(branch, ())
                        for review in by_source.get(source_id, ())
                    ]
                    if not reviews:
                        continue
                    have_reviews = True
                    for page in await renderer.cards(reviews, "", images):
                        slides.append(Slide(
                            prototype, f"Отзывы — {branch}", page, platform=platform_key,
                        ))
                if not have_reviews:
                    complete = bool(sources) and all(
                        source.get("coverage_complete") for source in sources
                    )
                    empty_message = (
                        "За период нет отзывов." if complete
                        else "За период нет сохранённых отзывов. Полнота выдачи не подтверждена."
                    )
                    for page in await renderer.cards([], "", empty_message=empty_message):
                        slides.append(Slide(prototype, "Отзывы", page, platform=platform_key))
            slides.extend(Slide(part) for part in deck.vk_slides())
            case_prototype = deck.find("Критические кейсы")
            for page in await renderer.cases(snapshot["cases"]):
                slides.append(
                    Slide(
                        case_prototype, "Критические кейсы опубликованные в социальных сетях", page
                    )
                )
        pptx = await asyncio.to_thread(deck.build, slides)
        directory = self.database.path.parent / "exports" / "activity" / uuid4().hex

        def save():
            directory.mkdir(parents=True)
            # The manifest is written last: a folder without it is never a completed export.
            (directory / "activity.pptx").write_bytes(pptx)
            (directory / "reviews.csv").write_bytes(reviews_csv(snapshot))
            (directory / "vk-matches.csv").write_bytes(deck.matches_csv)
            (directory / "identity-suggestions.csv").write_bytes(identity_csv(template, snapshot))
            checks = io.StringIO(newline="")
            writer = csv.writer(checks, delimiter=";")
            writer.writerow(["Раздел", "Проверка", "Результат"])
            writer.writerow(["Период", "Оба дня включены", snapshot["period"]])
            writer.writerow(["Период", "Предыдущий период", snapshot["comparison_period"]])
            writer.writerow(["Архив", "Старые записи, не найденные в проверенной выдаче",
                             snapshot.get("excluded_cached_reviews", 0)])
            for source in snapshot["sources"]:
                section = f"{source['platform']} / {source['branch_name']}"
                writer.writerow(
                    [
                        section,
                        "История до начала периода",
                        "Подтверждена" if source["coverage_complete"] else "Не подтверждена",
                    ]
                )
                writer.writerow([section, "Время проверки выдачи UTC",
                                 source.get("coverage_checked_at") or "Не проверена"])
                writer.writerow(
                    [section, "Рейтинг на конец периода", source.get("rating_at_end") or "н/д"]
                )
                writer.writerow(
                    [
                        section,
                        "Изменение рейтинга",
                        source["rating_change"]
                        if source.get("rating_change") is not None
                        else "Нет двух замеров",
                    ]
                )
                if source.get("rating_at_end") is None and source.get("latest_rating_at"):
                    writer.writerow(
                        [section, "Последний замер (UTC)",
                         f"{source['latest_rating']:g} / {source['latest_rating_at']}"]
                    )
                writer.writerow([section, "Средняя оценка выбранного периода",
                                 source.get("period_average")])
                writer.writerow([section, "Средняя оценка предыдущего периода",
                                 source.get("previous_period_average")])
                if source.get("average_rating_change") is not None:
                    writer.writerow(
                        [section, "Разница средних оценок отзывов", source["average_rating_change"]]
                    )
            for warning in snapshot["warnings"]:
                writer.writerow(["Ограничение", "Проверить", warning])
            (directory / "checks.csv").write_bytes(checks.getvalue().encode("utf-8-sig"))
            (directory / "evidence.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest = json.dumps(
                {
                    "period": snapshot["period"],
                    "title": report_title(snapshot["period"]),
                    "slides": len(slides),
                    "reviews": len(snapshot["reviews"]),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "warnings": snapshot["warnings"],
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            with ZipFile(directory / "audit.zip", "w", compression=ZIP_DEFLATED) as archive:
                for name in (
                    "reviews.csv",
                    "vk-matches.csv",
                    "identity-suggestions.csv",
                    "evidence.json",
                    "checks.csv",
                ):
                    archive.write(directory / name, name)
                archive.writestr("manifest.json", manifest)
            (directory / "manifest.json").write_bytes(manifest)

        await asyncio.to_thread(save)
        return ActivityExport(
            directory,
            len(slides),
            len(snapshot["reviews"]),
            snapshot["vk"]["posts_count"] if snapshot["vk"] else None,
            tuple(snapshot["warnings"]),
            snapshot.get("vk_collection_error"),
        )
