"""Extract public review details and fetch presentation-sized images."""

import asyncio
import base64
import io
import json
import re
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

_IMAGE_HOST = re.compile(
    r"avatars\.(?:mds\.)?yandex\.net|i\d+\.photo\.2gis\.com|cachizer\d+\.2gis\.com"
)
_IMAGE_LIMIT = 1024 * 1024
_EXPORT_LIMIT = 24 * _IMAGE_LIMIT


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _string(value):
    return value.strip() if isinstance(value, str) else ""


def _image_url(value, size=""):
    value = _string(value).replace("{size}", size)
    if value.startswith("//"):
        value = "https:" + value
    try:
        url = urlsplit(value)
        if url.scheme == "https" and _IMAGE_HOST.fullmatch(url.netloc):
            return value
    except ValueError:
        pass
    return ""


def _count(value):
    return value if type(value) is int and value >= 0 else None


def review_details(review: dict) -> dict:
    try:
        raw = _object(json.loads(review.get("raw_payload_json") or "{}"))
    except (ValueError, TypeError):
        raw = {}
    if review.get("platform") == "yandex":
        author = _object(raw.get("author"))
        reactions = _object(raw.get("reactions"))
        reply = _object(raw.get("businessComment"))
        photos = [
            _image_url(_object(photo).get("urlTemplate"), "S")
            for photo in _list(raw.get("photos"))
        ]
        return {
            "author_level": _string(author.get("professionLevel")),
            "likes": _count(reactions.get("likes")),
            "dislikes": _count(reactions.get("dislikes")),
            "reply": _string(reply.get("text")),
            "reply_date": _string(reply.get("updatedTime")),
            "avatar": _image_url(author.get("avatarUrl"), "islands-68"),
            "photos": list(dict.fromkeys(url for url in photos if url)),
        }
    author = _object(raw.get("user"))
    reply = _object(raw.get("official_answer"))
    photos = []
    for item in _list(raw.get("media")):
        photo = _object(_object(item).get("photo"))
        preview = _object(photo.get("preview_urls"))
        photos.append(_image_url(preview.get("320x")))
    return {
        "likes": _count(raw.get("likes_count")),
        "dislikes": _count(raw.get("dislikes_count")),
        "reply": _string(reply.get("text") or reply.get("answer") or raw.get("official_answer")),
        "reply_date": _string(reply.get("date_created") or reply.get("date")),
        "avatar": _image_url(_object(author.get("photo_preview_urls")).get("64x64")),
        "photos": list(dict.fromkeys(url for url in photos if url)),
    }


def _thumbnail(raw: bytes) -> str:
    try:
        with Image.open(io.BytesIO(raw), formats=("JPEG", "PNG", "WEBP", "GIF")) as source:
            if source.width * source.height > 8_000_000:
                return ""
            source.thumbnail((320, 240))
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            image.save(output, "JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (OSError, ValueError, Image.DecompressionBombError):
        return ""


async def load_review_images(reviews: list[dict], proxy: str | None = None) -> tuple[dict, int]:
    details = [item["details"] for item in reviews]
    urls = dict.fromkeys(
        url for item in details for url in (item["avatar"], *item["photos"]) if _image_url(url)
    )
    if not urls:
        return {}, 0
    slots = asyncio.Semaphore(5)
    received = 0
    timeout = aiohttp.ClientTimeout(total=7)

    async def fetch(session, url):
        nonlocal received
        async with slots:
            if received >= _EXPORT_LIMIT:
                return ""
            try:
                async with session.get(url, proxy=proxy, allow_redirects=False) as response:
                    if (
                        response.status != 200 or not response.content_type.startswith("image/")
                        or (response.content_length or 0) > _IMAGE_LIMIT
                    ):
                        return ""
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(32 * 1024):
                        received += len(chunk)
                        raw.extend(chunk)
                        if received > _EXPORT_LIMIT or len(raw) > _IMAGE_LIMIT:
                            return ""
                return await asyncio.to_thread(_thumbnail, raw)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return ""

    # Put review photos ahead of optional avatars when the export reaches its budget.
    ordered = dict.fromkeys([url for item in details for url in item["photos"] if url in urls] + list(urls))
    candidates = list(ordered)[:300]
    images = {}

    async def save(session, url):
        image = await fetch(session, url)
        if image:
            images[url] = image

    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
        tasks = [asyncio.create_task(save(session, url)) for url in candidates]
        try:
            done, _ = await asyncio.wait(tasks, timeout=45)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return images, len(urls) - len(images)


def is_negative(review: dict) -> bool:
    sentiment = review.get("sentiment")
    rating = review.get("rating")
    return sentiment == "negative" or (
        sentiment is None and type(rating) is int and 1 <= rating <= 2
    )
