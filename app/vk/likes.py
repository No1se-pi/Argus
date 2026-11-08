"""Count each profile once per post, discovering profiles from VK likes lists."""

import asyncio
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from html import escape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.analytics.periods import parse_period
from app.time import resolve_timezone
from app.vk.client import VKAPIError, VKClient
from app.vk.likes_repository import VKLikesRepository
from app.vk.service import VKService

UTC = timezone.utc  # noqa: UP017 - Python 3.10 compatibility


def likes_web_token(settings, config) -> str | None:
    saved = getattr(config, "web_access_token", None)
    if saved:
        return saved
    web_key = getattr(settings, "vk_web_access_token", None)
    return web_key.get_secret_value() if web_key else None


def likes_access_token(settings, config) -> str | None:
    """Explicit website credentials, then app service key, then user token."""
    web_key = likes_web_token(settings, config)
    if web_key:
        return web_key
    service_key = getattr(settings, "vk_service_access_token", None)
    if service_key:
        return service_key.get_secret_value()
    return config.user_access_token


@dataclass(frozen=True)
class LikesSelection:
    label: str
    start: datetime | None = None
    end: datetime | None = None
    last: int | None = None
    group_reference: str | None = None
    post_id: int | None = None


def parse_vk_link(value: str) -> tuple[str, int | None]:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.hostname not in {"vk.com", "vk.ru", "www.vk.com", "www.vk.ru", "m.vk.com", "m.vk.ru"}:
        raise ValueError("Ожидалась ссылка vk.com или vk.ru.")
    target = parse_qs(parsed.query).get("w", [parsed.path.strip("/")])[0]
    match = re.match(r"^(?:wall|reactions)-([1-9]\d*)_([1-9]\d*)(?:[?&/]|$)", unquote(target))
    if match:
        return match[1], int(match[2])
    path = parsed.path.strip("/")
    match = re.fullmatch(r"(?:club|public|event)([1-9]\d*)", path)
    if match:
        return match[1], None
    if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.]*", path):
        return path, None
    raise ValueError("Не удалось прочитать ссылку на сообщество или пост ВК.")


def parse_selection(args: str, timezone_name: str) -> LikesSelection:
    parts = args.split()
    group_reference = None
    if parts and any(
        parts[0].startswith(prefix)
        for prefix in ("https://", "http://", "vk.com/", "vk.ru/", "www.vk.", "m.vk.")
    ):
        group_reference, post_id = parse_vk_link(parts.pop(0))
        if not parts and post_id is not None:
            return LikesSelection(
                label=f"Пост {post_id}", group_reference=group_reference, post_id=post_id
            )
    if not parts:
        parts = ["7d"]
    if len(parts) == 2 and parts[0].lower() == "last":
        count = int(parts[1])
        if not 1 <= count <= 1000:
            raise ValueError("Количество постов должно быть от 1 до 1000.")
        return LikesSelection(
            label=f"Последние {count} постов", last=count, group_reference=group_reference
        )
    if len(parts) == 1:
        period = parse_period(parts[0])
        return LikesSelection(
            label=period.label, start=period.start, end=period.end, group_reference=group_reference
        )
    if len(parts) == 2:
        zone = resolve_timezone(timezone_name)
        start_date = datetime.strptime(parts[0], "%Y-%m-%d").date()
        end_date = datetime.strptime(parts[1], "%Y-%m-%d").date()
        if end_date < start_date:
            raise ValueError("Дата окончания должна быть не раньше даты начала.")
        start = datetime.combine(start_date, time.min, zone).astimezone(UTC)
        end = datetime.combine(end_date + timedelta(days=1), time.min, zone).astimezone(UTC)
        return LikesSelection(
            label=f"{parts[0]} — {parts[1]} включительно ({timezone_name})",
            start=start,
            end=end,
            group_reference=group_reference,
        )
    raise ValueError("Укажите 7d, last 17 или две даты YYYY-MM-DD YYYY-MM-DD.")


class VKLikesService:
    def __init__(self, vk_service: VKService, repository: VKLikesRepository) -> None:
        self.vk_service = vk_service
        self.repository = repository
        self._lock = asyncio.Lock()

    async def collect(self, selection: LikesSelection) -> dict[str, Any]:
        if self._lock.locked():
            raise ValueError("Сбор лайков уже выполняется. Дождитесь завершения.")
        async with self._lock:
            config = await self.vk_service.effective_config()
            if not config.enabled:
                raise ValueError("VK Monitor выключен. Включите его командой /vk_watch_on.")
            token = likes_access_token(self.vk_service.settings, config)
            if not token:
                raise ValueError(
                    "Для сбора нужен VK_SERVICE_ACCESS_TOKEN или VK_USER_ACCESS_TOKEN."
                )
            if not config.group_id and not selection.group_reference:
                raise ValueError(
                    "Укажите ссылку на сообщество или настройте VK_GROUP_ID (/vk_setup)."
                )
            client = VKClient(
                access_token=token,
                api_version=self.vk_service.settings.vk_api_version,
                web_mode=bool(likes_web_token(self.vk_service.settings, config)),
            )
            started = datetime.now(UTC).isoformat()
            group_id = config.group_id
            if selection.group_reference:
                if selection.group_reference.isdigit():
                    group_id = int(selection.group_reference)
                else:
                    group_id = await self._request(
                        client.resolve_group_id, selection.group_reference
                    )
            if selection.post_id is not None:
                posts = [await self._request(client.get_wall_post, group_id, selection.post_id)]
            else:
                posts = await self._posts(client, group_id, selection)
            counts: Counter[int] = Counter()
            collected_posts = []
            for post in posts:
                likers = await self._likers(client, group_id, post["id"])
                # A profile contributes at most one like per post, even if pages overlap.
                counts.update(likers)
                collected_posts.append(
                    {
                        "id": post["id"],
                        "date": post["date"],
                        "url": f"https://vk.com/wall{-group_id}_{post['id']}",
                        "user_ids": sorted(likers),
                    }
                )
            names = await self._names(client, sorted(counts))
            rows = [
                {
                    "user_id": user_id,
                    "full_name": names.get(
                        user_id, f"id{user_id}" if user_id > 0 else f"club{-user_id}"
                    ),
                    "url": (
                        f"https://vk.com/id{user_id}"
                        if user_id > 0
                        else f"https://vk.com/club{-user_id}"
                    ),
                    "likes": count,
                }
                for user_id, count in counts.items()
            ]
            rows.sort(key=lambda row: (-row["likes"], row["full_name"].casefold(), row["user_id"]))
            report = {
                "group_id": group_id,
                "label": selection.label,
                "period_start": selection.start.isoformat() if selection.start else None,
                "period_end_exclusive": selection.end.isoformat() if selection.end else None,
                "requested_last": selection.last,
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "posts_count": len(posts),
                "posts": collected_posts,
                "rows": rows,
            }
            # Publish only after every page and every post has been read successfully.
            await self.repository.save_report(report)
            return report

    async def latest(self) -> dict[str, Any] | None:
        return await self.repository.latest_report()

    async def _request(self, callback, *args, **kwargs):
        # Keep below the user-token request quota; no parallel calls in a collection.
        for attempt in range(3):
            await asyncio.sleep(0.35 if attempt == 0 else float(attempt))
            try:
                return await callback(*args, **kwargs)
            except VKAPIError as exc:
                if str(exc.code) not in {"6", "10"} or attempt == 2:
                    raise

    async def _posts(
        self, client: VKClient, group_id: int, selection: LikesSelection
    ) -> list[dict[str, Any]]:
        selected: dict[int, dict[str, Any]] = {}
        seen: set[int] = set()
        offset = 0
        for _ in range(100):
            page = await self._request(client.get_wall_page, group_id, offset=offset)
            items = page["items"]
            ordinary = []
            new_ids = set()
            for post in items:
                if (
                    not isinstance(post, dict)
                    or type(post.get("id")) is not int
                    or type(post.get("date")) is not int
                    or post.get("owner_id") != -abs(group_id)
                ):
                    raise VKAPIError("VK вернул некорректный пост. Отчет не сохранен.")
                new_ids.add(post["id"])
                date = datetime.fromtimestamp(post["date"], UTC)
                if not post.get("is_pinned"):
                    ordinary.append(date)
                if selection.start is not None and date < selection.start:
                    continue
                if selection.end is not None and date >= selection.end:
                    continue
                selected[post["id"]] = post
            offset += len(items)
            ended = offset >= page["count"]
            if not ended and (not items or not (new_ids - seen)):
                raise VKAPIError("VK вернул неполную стену. Отчет не сохранен.")
            seen.update(new_ids)
            enough = False
            if selection.last is not None:
                # Pinned posts may be much older than the requested last N posts.
                enough = len([p for p in selected.values() if not p.get("is_pinned")]) >= (
                    selection.last
                )
            elif selection.start is not None:
                enough = any(date < selection.start for date in ordinary)
            if ended or enough:
                result = sorted(
                    selected.values(), key=lambda post: (post["date"], post["id"]), reverse=True
                )
                return result[: selection.last] if selection.last is not None else result
        raise VKAPIError("Достигнут предел 10000 постов. Выберите более короткий период.")

    async def _likers(self, client: VKClient, group_id: int, post_id: int) -> set[int]:
        result: set[int] = set()
        offset = 0
        for _ in range(1000):
            page = await self._request(client.get_likes_page, group_id, post_id, offset=offset)
            ids = page["items"]
            if any(type(user_id) is not int or user_id == 0 for user_id in ids):
                raise VKAPIError("VK вернул некорректный список лайков. Отчет не сохранен.")
            new_ids = set(ids) - result
            result.update(ids)
            offset += len(ids)
            if offset >= page["count"]:
                if len(result) < page["count"]:
                    raise VKAPIError("Список лайков изменился при сборе. Повторите запрос.")
                return result
            if not ids or not new_ids:
                raise VKAPIError("VK вернул неполный список лайков. Отчет не сохранен.")
        raise VKAPIError("Превышен предел страниц лайков. Отчет не сохранен.")

    async def _names(self, client: VKClient, ids: list[int]) -> dict[int, str]:
        names = {}
        user_ids = [item for item in ids if item > 0]
        for offset in range(0, len(user_ids), 1000):
            users = await self._request(client.get_users, user_ids[offset : offset + 1000])
            for user in users:
                name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                names[user["id"]] = name or f"id{user['id']}"
        group_ids = [item for item in ids if item < 0]
        for offset in range(0, len(group_ids), 500):
            groups = await self._request(client.get_groups, group_ids[offset : offset + 500])
            for group in groups:
                names[-group["id"]] = group.get("name") or f"club{group['id']}"
        return names


def report_text(report: dict[str, Any], timezone_name: str) -> str:
    zone = resolve_timezone(timezone_name)
    started = datetime.fromisoformat(report["started_at"]).astimezone(zone)
    finished = datetime.fromisoformat(report["finished_at"]).astimezone(zone)
    lines = [
        "<b>Лайки по профилям ВК</b>",
        escape(report["label"]),
        f"Сообщество: vk.com/club{report['group_id']}",
        f"Постов: {report['posts_count']} · Профилей: {len(report['rows'])}",
        f"Сбор: {started:%d.%m.%Y %H:%M} — {finished:%H:%M} ({escape(timezone_name)})",
        "Лайки на момент сбора на постах выбранного периода.",
        "",
    ]
    if not report["posts_count"]:
        lines.append("За выбранный период постов нет.")
    elif not report["rows"]:
        lines.append("На выбранных постах лайков нет.")
    shown = 0
    for row in report["rows"][:20]:
        name = escape(row["full_name"][:100])
        line = f'<a href="{row["url"]}">{name}</a> — {row["likes"]}/{report["posts_count"]}'
        if len("\n".join(lines)) + len(line) > 3500:
            break
        lines.append(line)
        shown += 1
    if len(report["rows"]) > shown:
        lines.append(f"Первые {shown} строк; полный список — в CSV.")
    return "\n".join(lines)


def report_csv(report: dict[str, Any]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(
        [
            "ФИО",
            "VK ID",
            "Профиль",
            "Лайки",
            "Всего постов",
            "Период",
            "Начало сбора UTC",
            "Конец сбора UTC",
            "Сообщество",
            "Посты с лайком",
        ]
    )
    liked_urls: dict[int, list[str]] = {}
    for post in report["posts"]:
        for user_id in post["user_ids"]:
            liked_urls.setdefault(user_id, []).append(post["url"])
    for row in report["rows"]:
        # Prevent spreadsheet formulas from names supplied by external profiles.
        name = row["full_name"]
        if name.lstrip().startswith(("=", "+", "-", "@")) or name.startswith(("\t", "\r", "\n")):
            name = "'" + name
        writer.writerow(
            [
                name,
                row["user_id"],
                row["url"],
                row["likes"],
                report["posts_count"],
                report["label"],
                report["started_at"],
                report["finished_at"],
                f"https://vk.com/club{report['group_id']}",
                " ".join(liked_urls.get(row["user_id"], [])),
            ]
        )
    return stream.getvalue().encode("utf-8-sig")
