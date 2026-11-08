"""Build a complete activity deck from saved evidence; never trigger VK collection."""

import asyncio
import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from app.reports.data import Period, read_snapshot
from app.reports.history import HistoricalReviewsLoader
from app.reports.identities import identity_csv
from app.reports.pptx import Slide, TemplateDeck
from app.reports.render import RenderedPage, ReportRenderer


@dataclass(frozen=True)
class ActivityExport:
    directory: Path
    slides: int
    reviews: int
    posts: int | None
    warnings: tuple[str, ...]


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

    async def generate(self, period: Period) -> ActivityExport:
        if self._lock.locked():
            raise ValueError("Презентация уже формируется. Дождитесь завершения текущей задачи.")
        async with self._lock:
            template = await self.workspace.template()
            if template is None:
                raise ValueError("Сначала загрузите исходный PPTX в меню ВК → Шаблон презентации.")
            if self.reviews_service is not None:
                await self.reviews_service.ensure_default_sources()
            snapshot = await asyncio.to_thread(read_snapshot, self.database.path, period)
            if self.history is not None:
                warnings = await self.history.ensure(period, snapshot["sources"])
                snapshot = await asyncio.to_thread(read_snapshot, self.database.path, period)
                snapshot["warnings"].extend(warnings)
            return await self.generate_snapshot(template, snapshot)

    async def generate_snapshot(self, template: bytes, snapshot: dict) -> ActivityExport:
        deck = await asyncio.to_thread(TemplateDeck, template, snapshot, self.timezone_name)
        yandex = [source for source in snapshot["sources"] if source["platform"] == "yandex"]
        dgis = [source for source in snapshot["sources"] if source["platform"] == "2gis"]
        yandex_proto, dgis_proto = deck.card_prototypes()
        slides = [Slide(deck.order[0], cover=True)]
        async with ReportRenderer(self.timezone_name) as renderer:
            for platform, sources, prototype, names in (
                (
                    "Яндекс Карты",
                    yandex,
                    yandex_proto,
                    ("РЕЙТИНГ УЧЕБНЫХ ОТДЕЛЕНИЙ Я.КАРТЫ", "ИНФОРМАЦИОННАЯ СПРАВКА ЯНДЕКС.КАРТЫ"),
                ),
                (
                    "2ГИС",
                    dgis,
                    dgis_proto,
                    ("РЕЙТИНГ УЧЕБНЫХ ОТДЕЛЕНИЙ 2ГИС", "ИНФОРМАЦИОННАЯ СПРАВКА 2ГИС"),
                ),
            ):
                rating = Slide(
                    deck.find(names[0]),
                    names[0],
                    RenderedPage(
                        await renderer.overview(
                            sources, snapshot["reviews"], ratings=True, label=snapshot["period"]
                        ),
                        [],
                    ),
                )
                summary = Slide(
                    deck.find(names[1]),
                    names[1],
                    RenderedPage(
                        await renderer.overview(
                            sources, snapshot["reviews"], label=snapshot["period"]
                        ),
                        [],
                    ),
                )
                slides.extend(
                    [rating, summary] if platform == "Яндекс Карты" else [summary, rating]
                )
                for source in sources:
                    reviews = [
                        review
                        for review in snapshot["reviews"]
                        if review["source_id"] == source["id"]
                    ]
                    for page in await renderer.cards(
                        reviews, f"{source['branch_name']} · {platform}"
                    ):
                        slides.append(Slide(prototype, "Скриншоты", page))
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
            for source in snapshot["sources"]:
                section = f"{source['platform']} / {source['branch_name']}"
                writer.writerow(
                    [
                        section,
                        "История до начала периода",
                        "Подтверждена" if source["coverage_complete"] else "Не подтверждена",
                    ]
                )
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
            for warning in snapshot["warnings"]:
                writer.writerow(["Ограничение", "Проверить", warning])
            (directory / "checks.csv").write_bytes(checks.getvalue().encode("utf-8-sig"))
            (directory / "evidence.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest = json.dumps(
                {
                    "period": snapshot["period"],
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
        )
