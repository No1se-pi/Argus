"""Local-only HTML rendering; one Chromium per export, no third-party requests."""

import asyncio
import io
from dataclasses import dataclass
from html import escape
from pathlib import Path
from shutil import which

from PIL import Image

from app.reports.branding import BRANCH_LOGOS, PLATFORM_LOGOS, logo_data_url

from app.time import parse_iso_datetime, resolve_timezone

WIDTH, HEIGHT = 1440, 600
CARD_HEIGHT = 550
CARD_WIDTH = 456
CARD_GAP = 18
COLUMN_COUNT = 3
WIDE_CARD_WIDTH = COLUMN_COUNT * CARD_WIDTH + (COLUMN_COUNT - 1) * CARD_GAP
CSS = """
*{box-sizing:border-box}html,body{margin:0;padding:0;background:transparent}
body{font-family:Arial,'Liberation Sans',sans-serif;color:#242528}
.card{padding:14px 16px;background:white;overflow-wrap:anywhere;border:1px solid #e7e8ed;border-radius:10px}
.wide .review-content.dual{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}
.wide.reply-dominant .review-content.dual{grid-template-columns:minmax(0,1fr) minmax(0,3fr)}
.wide .reply{margin:0}.wide .text{margin-top:2px}.wide .reply .text{margin:0}
.compact .text{font-size:14px;line-height:19px}.compact .photos img{width:69px;height:59px}
.compact .identity{margin-bottom:4px}.compact .reactions{margin-top:7px}
.compact .reply .text{padding:9px 11px}
.more{margin-top:8px;color:#596078;font-size:13px}
.card-brand{min-height:19px;margin:0 0 6px;display:flex;justify-content:flex-end}
.card-brand img{width:auto;height:12px;max-width:152px;object-fit:contain}
.card-brand span{font-size:13px;color:#596078}
.author{font-size:19px;font-weight:700;line-height:23px}
.identity{display:flex;align-items:center;gap:11px;margin-bottom:8px;min-height:31px}
.level,.date{font-size:15px;color:#80858e;font-weight:400}.level{margin-top:3px}
.avatar{width:42px;height:42px;border-radius:50%;object-fit:cover}
.stars{font-size:23px;line-height:26px;color:#fc0;white-space:nowrap}
.empty{color:#dcdde0}.date{margin-left:12px}
.text{font-size:17px;line-height:23px;white-space:pre-wrap;margin-top:6px}
.reactions{display:flex;gap:20px;margin-top:12px;color:#565960;font-size:16px}
.reactions span{display:flex;align-items:center;gap:5px}
.reactions svg{width:21px;height:21px}
.photos{margin-top:9px;display:flex;gap:8px;flex-wrap:wrap}
.photos img{width:86px;height:76px;object-fit:cover;border-radius:8px}
.reply{margin:14px 0 0 20px;background:#f1f1f2;border-radius:12px;overflow:hidden}
.reply header{background:#e9e9eb;font-size:16px;font-weight:bold;padding:10px 12px}
.reply .text{margin:0;padding:12px}
.footer{position:absolute;bottom:1px;right:18px;font-size:21px;font-weight:700;color:#777}
.rating-top{display:flex;align-items:center;justify-content:space-between;
width:1320px;margin:0 auto 14px;padding:8px 10px 0;min-height:48px}
.rating-note{font-size:25px;color:#3f3175;font-weight:700}
.platform-logo{width:145px;height:34px;object-fit:contain}
.rating-panel{width:1320px;margin:auto;display:grid;grid-template-columns:1fr 1fr;gap:15px}
.rating-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:18px;
min-height:163px;padding:17px 24px;border:1px solid #dce1ef;border-radius:15px;background:#fff}
.rating-panel.dgis .rating-row{border-color:#d7e7dc}
.rating-logo{width:165px;height:18px;object-fit:contain;object-position:left;margin-bottom:6px}
.rating-branch{font-size:25px;line-height:30px;color:#24345a;font-weight:bold}
.rating-address{font-size:20px;line-height:25px;color:#575f6e;margin-top:6px}
.score{text-align:right;min-width:202px}.score-value{font-size:43px;font-weight:700;color:#242947}
.star{color:#e9a416}.volume{display:block;font-size:18px;color:#575f6e;margin:0 0 4px}
.delta{display:inline-block;font-size:20px;font-weight:700;padding:4px 9px;
border-radius:6px;background:#f0f1f5;color:#454a60}
.delta.up{color:#137246;background:#e4f4e9}.delta.down{color:#a22538;background:#fae8ec}
.verified-owner{display:inline-flex;align-items:center;justify-content:center;
width:20px;height:20px;margin-left:7px;vertical-align:3px;border-radius:50%;
background:#2477f3;color:white;font:700 14px/1 Arial}
"""


@dataclass
class RenderedPage:
    image: bytes
    links: list[tuple[float, float, float, float, str]]


@dataclass
class Card:
    content: str
    height: int
    columns: int
    url: str | None
    style: str = ""

    @property
    def width(self):
        return self.columns * CARD_WIDTH + (self.columns - 1) * CARD_GAP


def card_slot(layout, card):
    heights = [0] * COLUMN_COUNT
    for col, y, placed in layout:
        heights[col:col + placed.columns] = [y + placed.height + CARD_GAP] * placed.columns
    candidates = [
        (max(heights[col:col + card.columns]), col)
        for col in range(COLUMN_COUNT - card.columns + 1)
    ]
    y, col = min(candidates)
    return (col, y, card) if y + card.height <= CARD_HEIGHT else None


def pack_cards(cards):
    pages = []
    for card in sorted(cards, key=lambda card: (card.columns, card.height), reverse=True):
        for layout in pages:
            slot = card_slot(layout, card)
            if slot is not None:
                layout.append(slot)
                break
        else:
            pages.append([(0, 0, card)])

    # When the last page needs to exist, balance its lone short review with
    # another card from a fuller page, provided both fit together.
    for page in pages:
        if len(page) != 1 or page[0][2].columns == COLUMN_COUNT:
            continue
        for donor in reversed(pages):
            if len(donor) < 3:
                continue
            slot = card_slot(page, donor[-1][2])
            if slot is not None:
                page.append(slot)
                donor.pop()
                break
    return pages


def display_date(value: str, timezone_name: str) -> str:
    try:
        date = parse_iso_datetime(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("Date has no timezone")
        return date.astimezone(resolve_timezone(timezone_name)).strftime("%d.%m.%Y")
    except (ValueError, AttributeError):
        return "дата не указана"


_THUMB = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.7' "
    "aria-hidden='true'><path stroke-linejoin='round' "
    "d='M7 10H3v11h4m0-11 5-8c3 0 3 3 2 7h5c2 0 2 2 2 3l-2 7c0 1-1 2-2 2H7Z'/></svg>"
)


def review_card(
    review: dict, timezone_name: str, text: str | None = None, *,
    images: dict | None = None, photos=None, reply: str | None = None, excerpt=False,
) -> str:
    details = review.get("details", {})
    images = images or {}
    rating = review.get("rating")
    stars = ""
    if type(rating) is int and 1 <= rating <= 5:
        stars = f"{'★' * rating}<span class='empty'>{'★' * (5 - rating)}</span>"
    value = review.get("text") or ""
    if value == "TEXT_EMPTY":
        value = ""
    if text is not None:
        value = text

    avatar = images.get(details.get("avatar"))
    avatar_tag = f"<img class='avatar' alt='' src='{escape(avatar, quote=True)}'>" if avatar else ""
    level = details.get("author_level")
    badge = f"<div class='level'>{escape(level)}</div>" if level else ""
    photo_tags = "".join(
        f"<img alt='Фото из отзыва' src='{escape(images[url], quote=True)}'>"
        for url in (photos if photos is not None else details.get("photos", ()))
        if url in images
    )
    gallery = f"<div class='photos'>{photo_tags}</div>" if photo_tags else ""
    reaction_tags = []
    for key, label in (("likes", "Нравится"), ("dislikes", "Не нравится")):
        count = details.get(key)
        if count is None:
            continue
        icon = _THUMB if key == "likes" else _THUMB.replace(
            "<svg ", "<svg style='transform:rotate(180deg)' "
        )
        reaction_tags.append(f"<span title='{label}'>{icon}{count}</span>")
    reactions = (
        "<div class='reactions'>" + "".join(reaction_tags) + "</div>" if reaction_tags else ""
    )
    reply_text = details.get("reply") if reply is None else reply
    official = ""
    if reply_text:
        reply_date = details.get("reply_date")
        reply_label = (
            f"<span class='date'>{display_date(reply_date, timezone_name)}</span>"
            if reply_date else ""
        )
        official = (
            f"<aside class='reply'><header>Официальный ответ{reply_label}</header>"
            f"<div class='text'>{escape(reply_text)}</div></aside>"
        )
    branch = review.get("branch_name", "").removeprefix("УО ").strip()
    logo = BRANCH_LOGOS.get(branch)
    brand = ""
    if logo:
        brand = (
            f"<div class='card-brand'><img alt='{escape(branch, quote=True)}' "
            f"src='{logo_data_url(logo)}'></div>"
        )
    elif branch:
        brand = f"<div class='card-brand'><span>{escape(branch)}</span></div>"
    more = "<div class='more'>Полный отзыв и ответ — в сверке и по ссылке.</div>" if excerpt else ""
    layout = " dual" if official else ""
    return (
        brand + f"<div class='identity'>{avatar_tag}<div class='author'>"
        f"{escape(review.get('author_name') or 'Аноним')}{badge}</div></div>"
        f"<div class='stars'>{stars}<span class='date'>"
        f"{display_date(review.get('published_at', ''), timezone_name)}</span></div>"
        + f"<div class='review-content{layout}'><div>"
        + f"<div class='text'>{escape(value) or 'Отзыв без текста'}</div>"
        + gallery + reactions + "</div>" + official + "</div>" + more
    )


def document(body: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'\">"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def compact_png(data: bytes) -> bytes:
    if len(data) < 70_000:
        return data
    with Image.open(io.BytesIO(data)) as image:
        indexed = image.convert("RGBA").quantize(
            colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE,
        )
        out = io.BytesIO()
        indexed.save(out, format="PNG", optimize=True)
        return min((data, out.getvalue()), key=len)


class ReportRenderer:
    def __init__(self, timezone_name: str):
        self.timezone_name = timezone_name
        self.playwright = self.browser = self.page = None

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ValueError(
                "Для презентаций установите Argus с дополнением reports и Chromium."
            ) from exc
        self.playwright = await async_playwright().start()
        try:
            chromium = self.playwright.chromium
            options = {}
            if not Path(chromium.executable_path).is_file() and (system := which("chromium")):
                options["executable_path"] = system
            self.browser = await chromium.launch(headless=True, **options)
            self.page = await self.browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=1.25,
            )
            await self.page.route("**/*", lambda route: route.abort())
        except Exception:
            await self.playwright.stop()
            raise
        return self

    async def __aexit__(self, *_args):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()

    async def image(self, body: str) -> bytes:
        await self.page.set_content(document(body), wait_until="load")
        image = await self.page.screenshot(type="png", omit_background=True)
        return await asyncio.to_thread(compact_png, image)

    async def ratings(
        self, branches: list[tuple[str, dict]], *, platform: str, period: str,
    ) -> list[bytes]:
        addresses = {
            "Датахаб": "1-я Парковая улица, 12",
            "Кибер": "Щёлковское шоссе, 52",
            "Диджитал": "1-я Мясниковская улица, 16",
            "АртТех": "5-я Парковая улица, 58",
            "Техно": "Верхняя Первомайская улица, 7",
            "Моссовет": "улица Расковой, 4",
        }
        rows = []
        for branch, source in branches:
            badge = BRANCH_LOGOS.get(branch)
            logo = (
                f"<img class='rating-logo' alt='' src='{logo_data_url(badge)}'>" if badge else ""
            )
            check = (
                "<span class='verified-owner' title='Информация подтверждена владельцем'>✓</span>"
                if platform == "yandex" and source.get("verified_owner") is True else ""
            )
            address = addresses.get(branch, "")
            rating = source.get("rating_at_end")
            change = source.get("rating_change") if rating is not None else None
            count = int(source.get("period_review_count") or 0)
            detail = f"Отзывов за период: {count}"
            if rating is None and source.get("latest_rating_at"):
                rating = source.get("latest_rating")
                captured_at = parse_iso_datetime(source["latest_rating_at"]).astimezone(
                    resolve_timezone(self.timezone_name)
                )
                detail = f"Последний замер {captured_at:%d.%m.%Y}"
            elif rating is None:
                detail = "Нет замера рейтинга"
            value = "—" if rating is None else f"<span class='star'>★</span> {rating:.1f}"
            direction = ""
            if change is None:
                delta = "Нет данных для сравнения"
            elif round(change, 1) == 0:
                delta = "Рейтинг не изменился"
            else:
                direction = "up" if change > 0 else "down"
                delta = f"Рейтинг {change:+.1f}"
            rows.append(
                "<div class='rating-row'>"
                f"<div>{logo}<div class='rating-branch'>УО {escape(branch)}{check}</div>"
                f"<div class='rating-address'>{escape(address)}</div></div>"
                f"<div class='score'><div class='score-value'>{value}</div>"
                f"<span class='volume'>{detail}</span>"
                f"<span class='delta {direction}'>{delta}</span></div></div>"
            )
        platform_class = " dgis" if platform == "2gis" else ""
        return [
            await self.image(
                "<div class='rating-top'>"
                f"<div class='rating-note'>Рейтинг карточек · {escape(period)}</div>"
                f"<img class='platform-logo' alt='' src='{logo_data_url(PLATFORM_LOGOS[platform])}'>"
                "</div>"
                f"<main class='rating-panel{platform_class}'>"
                + "".join(rows[offset:offset + 6]) + "</main>"
            )
            for offset in range(0, len(rows), 6)
        ]

    async def cards(
        self, reviews: list[dict], footer: str, images: dict | None = None,
        *, empty_message: str = "За период нет сохранённых отзывов.",
    ) -> list[RenderedPage]:
        images = images or {}
        if not reviews:
            image = await self.image(
                "<div style='padding:120px 45px;font-size:30px'>"
                f"{escape(empty_message)}</div>"
                f"<div class='footer'>{escape(footer)}</div>"
            )
            return [RenderedPage(image, [])]
        # Measure all ordinary cards in one browser operation, not one launch per review.
        raw = [review_card(review, self.timezone_name, images=images) for review in reviews]
        await self.page.set_content(document("".join(
            f"<article class='card' style='width:{CARD_WIDTH}px'>{body}</article>" for body in raw
        )))
        heights = await self.page.locator("article").evaluate_all(
            "cards => cards.map(card => Math.ceil(card.getBoundingClientRect().height))"
        )
        cards = []
        for review, content, height in zip(reviews, raw, heights, strict=True):
            url = review.get("review_url")
            if height <= CARD_HEIGHT:
                cards.append(Card(content, height, 1, url))
            else:
                cards.append(await self.fit_wide_card(review, content, images))

        pages = []
        side = (WIDTH - WIDE_CARD_WIDTH) // 2
        for placed in pack_cards(cards):
            cards_html, links = [], []
            for col, y, card in placed:
                x = side + col * (CARD_WIDTH + CARD_GAP)
                if len(placed) == 1:
                    x = (WIDTH - card.width) // 2
                    y = (CARD_HEIGHT - card.height) // 2
                cards_html.append(
                    f"<article class='card {card.style}' style='position:absolute;left:{x}px;"
                    f"top:{y}px;width:{card.width}px'>{card.content}</article>"
                )
                if card.url:
                    links.append((x / WIDTH, y / HEIGHT, card.width / WIDTH,
                                  card.height / HEIGHT, card.url))
            body = "".join(cards_html) + f"<div class='footer'>{escape(footer)}</div>"
            pages.append(RenderedPage(await self.image(body), links))
        return pages

    async def fit_wide_card(self, review, content, images):
        url = review.get("review_url")

        async def measure(card):
            await self.page.set_content(document(
                f"<article class='card {card.style}' style='width:{card.width}px'>"
                f"{card.content}</article>"
            ))
            return await self.page.locator("article").evaluate(
                "card => Math.ceil(card.getBoundingClientRect().height)"
            )

        text = review.get("text") or ""
        reply = review.get("details", {}).get("reply") or ""
        wide_style = "wide reply-dominant" if len(reply) > len(text) * 1.5 else "wide"
        for columns, style in (
            (1, "compact"), (2, wide_style), (2, wide_style + " compact"),
            (COLUMN_COUNT, wide_style), (COLUMN_COUNT, wide_style + " compact"),
        ):
            card = Card(content, 0, columns, url, style)
            card.height = await measure(card)
            if card.height <= CARD_HEIGHT:
                return card

        # A physical slide has a readability limit. The full text and media
        # remain available in evidence.json for exceptional oversized reviews.
        photos = [
            photo for photo in review.get("details", {}).get("photos", ()) if photo in images
        ][:10]
        size = max(len(text), len(reply))
        while True:
            card.content = review_card(
                review, self.timezone_name,
                text[:size] + ("…" if len(text) > size else ""),
                images=images, photos=photos,
                reply=reply[:size] + ("…" if len(reply) > size else ""), excerpt=True,
            )
            card.height = await measure(card)
            if card.height <= CARD_HEIGHT:
                return card
            if size <= 1 and not photos:
                raise ValueError("Карточка отзыва не помещается на слайд.")
            size //= 2
            photos = photos[:max(1, len(photos) // 2)] if size else []

    async def cases(self, cases: list[dict]) -> list[RenderedPage]:
        if not cases:
            return [
                RenderedPage(
                    await self.image(
                        "<div style='padding:120px 45px;font-size:30px'>"
                        "За период нет подтверждённых критических случаев.</div>"
                    ),
                    [],
                )
            ]
        converted = [
            {
                "author_name": item["source_title"],
                "rating": None,
                "published_at": item["message_date"],
                "text": item["text"],
                "review_url": item.get("message_url"),
            }
            for item in cases
        ]
        return await self.cards(converted, "Социальные сети")
