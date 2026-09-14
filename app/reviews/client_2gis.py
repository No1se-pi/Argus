import json
import logging
import re
import urllib.parse

import aiohttp

from app.reviews.models import ReviewItem, ReviewPlatform, ReviewSource, ReviewSyncStatus

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_2GIS_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"


class DGisReviewsClient:
    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        api_key: str = DEFAULT_2GIS_KEY,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._external_session = session is not None
        self._session = session
        self._user_agent = user_agent
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
            )
        return self._session

    async def close(self) -> None:
        if not self._external_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _rotate_key_from_page(self, firm_url: str) -> bool:
        session = await self._get_session()
        try:
            headers = {
                "Cookie": "dg5_museum_accept=true",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            async with session.get(firm_url, headers=headers) as resp:
                if resp.status != 200:
                    return False
                html = await resp.text(errors="replace")
                match = re.search(r'["\']reviewApiKey["\']\s*:\s*["\']([a-f0-9-]+)["\']', html)
                if match:
                    self._api_key = match.group(1)
                    logger.info("Successfully rotated 2GIS reviewApiKey from page")
                    return True
                return False
        except Exception as exc:
            logger.warning("Failed to rotate 2GIS key from page: %s", exc)
            return False

    async def fetch_reviews(
        self,
        source: ReviewSource,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[
        ReviewSyncStatus, list[ReviewItem], str | None, int | None, float | None, int | None
    ]:
        session = await self._get_session()
        api_base = f"https://public-api.reviews.2gis.com/3.0/branches/{source.external_id}/reviews"

        for attempt in range(2):
            params = {
                "limit": str(page_size),
                "page": str(page),
                "sort_by": "date_created",
                "key": self._api_key,
                "locale": "ru_RU",
                "fields": "meta.branch_rating,meta.branch_reviews_count,meta.total_count",
            }
            url = f"{api_base}?{urllib.parse.urlencode(params)}"

            try:
                async with session.get(url) as resp:
                    http_status = resp.status

                    if http_status in (401, 403) and attempt == 0:
                        logger.info(
                            "2GIS returned %d with current key, attempting key rotation...",
                            http_status,
                        )
                        rotated = await self._rotate_key_from_page(source.url)
                        if rotated:
                            continue
                        return (
                            ReviewSyncStatus.SOURCE_BLOCKED,
                            [],
                            f"2GIS API key rejected (HTTP {http_status}) and rotation failed",
                            http_status,
                            None,
                            None,
                        )

                    if http_status == 429:
                        return (
                            ReviewSyncStatus.RATE_LIMITED,
                            [],
                            "Rate limited by 2GIS (HTTP 429)",
                            429,
                            None,
                            None,
                        )
                    if http_status == 403:
                        return (
                            ReviewSyncStatus.SOURCE_BLOCKED,
                            [],
                            "Access forbidden by 2GIS (HTTP 403)",
                            403,
                            None,
                            None,
                        )
                    if http_status != 200:
                        return (
                            ReviewSyncStatus.HTTP_ERROR,
                            [],
                            f"HTTP {http_status}",
                            http_status,
                            None,
                            None,
                        )

                    data = await resp.json()
                    if not isinstance(data, dict) or not isinstance(data.get("reviews"), list):
                        return (
                            ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                            [],
                            "Invalid response schema: expected reviews list",
                            200,
                            None,
                            None,
                        )
                    reviews_data = data["reviews"]
                    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
                    branch_rating = (
                        float(meta["branch_rating"])
                        if meta.get("branch_rating") is not None
                        else None
                    )
                    total_count = (
                        int(meta["total_count"]) if meta.get("total_count") is not None else None
                    )

                    items: list[ReviewItem] = []
                    for r in reviews_data:
                        if not isinstance(r, dict):
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                "Invalid review item structure: expected dict",
                                200,
                                None,
                                None,
                            )
                        rev_id = str(r.get("id", "")).strip()
                        if not rev_id:
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                "Missing id in 2GIS review item",
                                200,
                                None,
                                None,
                            )

                        # Strict rating validation: Never invent rating=5
                        raw_rating = r.get("rating")
                        if raw_rating is None:
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                f"Missing rating for 2GIS review {rev_id}",
                                200,
                                None,
                                None,
                            )
                        try:
                            rating = int(raw_rating)
                        except (ValueError, TypeError):
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                (
                                    f"Invalid non-integer rating '{raw_rating}' "
                                    f"for 2GIS review {rev_id}"
                                ),
                                200,
                                None,
                                None,
                            )
                        if not (1 <= rating <= 5):
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                f"Rating {rating} out of range [1, 5] for 2GIS review {rev_id}",
                                200,
                                None,
                                None,
                            )

                        # Strict date_created validation
                        date_created = r.get("date_created")
                        if date_created is None or str(date_created).strip() == "":
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                f"Missing date_created for 2GIS review {rev_id}",
                                200,
                                None,
                                None,
                            )
                        published_at = str(date_created).strip()
                        edited_at = (
                            str(r.get("date_edited")).strip() if r.get("date_edited") else None
                        )

                        author = (
                            str(r.get("user", {}).get("name") or "Аноним").strip()
                            if isinstance(r.get("user"), dict)
                            else "Аноним"
                        )
                        text_raw = r.get("text")
                        text = str(text_raw).strip() if text_raw else "TEXT_EMPTY"
                        if not text:
                            text = "TEXT_EMPTY"
                        rev_url = f"{source.url}?reviewId={rev_id}"

                        items.append(
                            ReviewItem(
                                external_review_id=rev_id,
                                platform=ReviewPlatform.DGIS,
                                branch_name=source.branch_name,
                                author_name=author,
                                rating=rating,
                                text=text,
                                published_at=published_at,
                                edited_at=edited_at,
                                review_url=rev_url,
                            )
                        )

                    return (
                        ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
                        items,
                        None,
                        200,
                        branch_rating,
                        total_count,
                    )

            except aiohttp.ClientError as exc:
                return (
                    ReviewSyncStatus.NETWORK_ERROR,
                    [],
                    f"Network error: {exc}",
                    None,
                    None,
                    None,
                )
            except json.JSONDecodeError as exc:
                return (
                    ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                    [],
                    f"JSON parse error: {exc}",
                    200,
                    None,
                    None,
                )
            except Exception as exc:
                return (
                    ReviewSyncStatus.UNKNOWN_ERROR,
                    [],
                    f"Unexpected error: {exc}",
                    None,
                    None,
                    None,
                )

        return ReviewSyncStatus.SOURCE_BLOCKED, [], "2GIS key invalid", 403, None, None
