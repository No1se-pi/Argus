"""Local-only HTML rendering; one Chromium per export, no third-party requests."""

from dataclasses import dataclass
from html import escape

from app.time import parse_iso_datetime, resolve_timezone

WIDTH, HEIGHT = 1440, 600
CARD_HEIGHT = 540
CSS = """
*{box-sizing:border-box}html,body{margin:0;padding:0;background:transparent}
body{font-family:Arial,'Liberation Sans',sans-serif;color:#242528}
.card{padding:12px 15px;background:white;overflow-wrap:anywhere}
.author{font-size:17px;font-weight:700;line-height:23px;margin-bottom:9px}
.stars{font-size:23px;line-height:26px;color:#ffcc00;white-space:nowrap}
.empty{color:#dcdde0}.date{font-size:13px;color:#8b8e94;margin-left:12px}
.text{font-size:15px;line-height:21px;white-space:pre-wrap;margin-top:9px}
.link{font-size:11px;color:#737780;margin-top:8px;overflow-wrap:anywhere}
.footer{position:absolute;bottom:4px;right:18px;font-size:21px;font-weight:700;color:#777}
.tile{padding:28px;border:1px solid #e7e7ee;border-radius:10px;background:#fff}
.branch{font-size:23px;font-weight:700;line-height:30px}
.number{font-size:58px;color:#4d4380;margin:12px 0}
.caption{font-size:19px;color:#666;line-height:28px}.small{font-size:15px;color:#777}
"""


@dataclass
class RenderedPage:
    image: bytes
    links: list[tuple[float, float, float, float, str]]


def display_date(value: str, timezone_name: str) -> str:
    try:
        date = parse_iso_datetime(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("Date has no timezone")
        return date.astimezone(resolve_timezone(timezone_name)).strftime("%d.%m.%Y")
    except (ValueError, AttributeError):
        return "дата не указана"


def review_card(review: dict, timezone_name: str, text: str | None = None) -> str:
    rating = review.get("rating")
    stars = ""
    if type(rating) is int and 1 <= rating <= 5:
        stars = f"{'★' * rating}<span class='empty'>{'★' * (5 - rating)}</span>"
    value = review.get("text") or ""
    if value == "TEXT_EMPTY":
        value = ""
    if text is not None:
        value = text
    return (
        f"<div class='author'>{escape(review.get('author_name') or 'Аноним')}</div>"
        f"<div class='stars'>{stars}<span class='date'>"
        f"{display_date(review.get('published_at', ''), timezone_name)}</span></div>"
        f"<div class='text'>{escape(value) if value else 'Отзыв без текста'}</div>"
    )


def document(body: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


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
            self.browser = await self.playwright.chromium.launch(headless=True)
            self.page = await self.browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=1.5,
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
        return await self.page.screenshot(type="png", omit_background=True)

    async def overview(
        self, sources: list[dict], reviews: list[dict], *, ratings=False, label=""
    ) -> bytes:
        tiles = []
        for source in sources:
            items = [review for review in reviews if review["source_id"] == source["id"]]
            if ratings:
                rating = source.get("rating_at_end")
                number = f"{rating:.1f} ★" if rating is not None else "н/д"
                change = source.get("rating_change")
                caption = (
                    f"Изменение: {change:+.1f}"
                    if change is not None
                    else "Нет двух замеров для сравнения"
                )
            else:
                number = str(len(items))
                if not source.get("coverage_complete"):
                    number = f"≥{number}" if items else "н/д"
                texts = sum(
                    bool(item.get("text") and item["text"] != "TEXT_EMPTY") for item in items
                )
                caption = f"С текстом: {texts}"
            tiles.append(
                f"<div class='tile'><div class='branch'>{escape(source['branch_name'])}</div>"
                f"<div class='number'>{number}</div><div class='caption'>{caption}</div></div>"
            )
        return await self.image(
            f"<div style='padding:0 25px 12px;font-size:20px;color:#777'>{escape(label)}</div>"
            "<main style='padding:8px 25px;display:grid;"
            "grid-template-columns:repeat(3,1fr);gap:22px'>" + "".join(tiles) + "</main>"
        )

    async def cards(self, reviews: list[dict], footer: str) -> list[RenderedPage]:
        if not reviews:
            image = await self.image(
                "<div style='padding:120px 45px;font-size:30px'>"
                "За период нет сохранённых отзывов.</div>"
                f"<div class='footer'>{escape(footer)}</div>"
            )
            return [RenderedPage(image, [])]
        # Measure all ordinary cards in one browser operation, not one launch per review.
        raw = [review_card(review, self.timezone_name) for review in reviews]
        await self.page.set_content(
            document(
                "".join(
                    f"<article class='card' style='width:460px'>{body}</article>" for body in raw
                )
            )
        )
        heights = await self.page.locator("article").evaluate_all(
            "cards => cards.map(card => Math.ceil(card.getBoundingClientRect().height))"
        )
        pages, placed, used = [], [], [0, 0, 0]

        async def flush():
            nonlocal placed, used
            if not placed:
                return
            body = (
                "".join(
                    f"<article class='card' style='position:absolute;left:{x}px;"
                    f"top:{y}px;width:{width}px'>{content}</article>"
                    for x, y, width, _height, content, _url in placed
                )
                + f"<div class='footer'>{escape(footer)}</div>"
            )
            image = await self.image(body)
            links = [
                (x / WIDTH, y / HEIGHT, width / WIDTH, height / HEIGHT, url)
                for x, y, width, height, _content, url in placed
                if url
            ]
            pages.append(RenderedPage(image, links))
            placed, used = [], [0, 0, 0]

        for review, content, height in zip(reviews, raw, heights, strict=True):
            url = review.get("review_url")
            if height <= CARD_HEIGHT:
                column = min(range(3), key=lambda index: used[index])
                if used[column] + height > CARD_HEIGHT:
                    await flush()
                    column = 0
                placed.append((column * 480, used[column], 460, height, content, url))
                used[column] += height + 22
                continue
            await flush()
            # Very long reviews use the full width. Split only after measurement;
            # concatenating the chunks retains every character of the original text.
            remaining = review.get("text") or ""
            while remaining:
                take = len(remaining)
                while True:
                    content = review_card(review, self.timezone_name, remaining[:take])
                    await self.page.set_content(
                        document(f"<article class='card' style='width:1400px'>{content}</article>")
                    )
                    measured = await self.page.locator("article").evaluate(
                        "card => Math.ceil(card.getBoundingClientRect().height)"
                    )
                    if measured <= CARD_HEIGHT:
                        break
                    if take <= 1:
                        raise ValueError("Карточка отзыва не помещается на слайд.")
                    take = max(1, take // 2)
                placed = [(15, 0, 1400, measured, content, url)]
                await flush()
                remaining = remaining[take:]
        await flush()
        return pages

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
