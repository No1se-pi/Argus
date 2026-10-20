import json
import logging
import re
import time
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
        self._branch_ratings: dict[str, float] = {}
        self._branch_counts: dict[str, int] = {}
        self._ratings_updated_at: dict[str, float] = {}

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

    @staticmethod
    def _parse_page_metadata(html: str) -> tuple[float | None, int | None]:
        rating: float | None = None
        count: int | None = None

        # 1. Rating: "general_rating" in JSON state of 2GIS web page
        m_rating = re.search(r'"general_rating"\s*:\s*([0-9.]+)', html)
        if m_rating:
            try:
                r_val = round(float(m_rating.group(1)), 1)
                if 1.0 <= r_val <= 5.0:
                    rating = r_val
            except (ValueError, TypeError):
                pass

        # Fallback for rating: og:description ("Оценка 4.8...") or HTML tags
        if rating is None:
            m_og = re.search(r"Оценка\s+([0-9.]+)", html)
            if m_og:
                try:
                    r_val = round(float(m_og.group(1)), 1)
                    if 1.0 <= r_val <= 5.0:
                        rating = r_val
                except (ValueError, TypeError):
                    pass

        # 2. Count: "general_review_count_with_stars" (matches visible "69 оценок")
        m_stars = re.search(r'"general_review_count_with_stars"\s*:\s*([0-9]+)', html)
        if m_stars:
            try:
                c_val = int(m_stars.group(1))
                if c_val >= 0:
                    count = c_val
            except (ValueError, TypeError):
                pass

        if count is None:
            m_reviews = re.search(r'"general_review_count"\s*:\s*([0-9]+)', html)
            if m_reviews:
                try:
                    c_val = int(m_reviews.group(1))
                    if c_val >= 0:
                        count = c_val
                except (ValueError, TypeError):
                    pass

        if count is None:
            m_cnt_txt = re.search(r"([0-9]+)\s+(?:оценок|отзыв)", html)
            if m_cnt_txt:
                try:
                    c_val = int(m_cnt_txt.group(1))
                    if c_val >= 0:
                        count = c_val
                except (ValueError, TypeError):
                    pass

        return rating, count

    async def _fetch_branch_metadata(
        self, source: ReviewSource, api_rating: float | None = None
    ) -> tuple[float | None, int | None]:
        session = await self._get_session()
        try:
            headers = {
                "Cookie": "dg5_museum_accept=true",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            async with session.get(source.url, headers=headers) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                    match_key = re.search(
                        r'["\']reviewApiKey["\']\s*:\s*["\']([a-f0-9-]+)["\']', html
                    )
                    if match_key:
                        self._api_key = match_key.group(1)
                    rating, count = self._parse_page_metadata(html)

                    # Determine reference/expected rating
                    curr_rating = self._branch_ratings.get(source.external_id)
                    if curr_rating is None and source.last_rating is not None:
                        try:
                            curr_val = round(float(source.last_rating), 1)
                            if 1.0 <= curr_val <= 5.0:
                                curr_rating = curr_val
                        except (ValueError, TypeError):
                            pass

                    effective_rating = rating
                    effective_count = count

                    # Anti-flapping & CDN edge cache jitter protection:
                    # If page rating differs from current known rating or api_rating,
                    # verify with a second confirmation fetch to rule out a stale CDN edge node.
                    reference_rating = curr_rating if curr_rating is not None else api_rating
                    if (
                        rating is not None
                        and 1.0 <= rating <= 5.0
                        and reference_rating is not None
                        and rating != reference_rating
                    ):
                        try:
                            async with session.get(source.url, headers=headers) as resp2:
                                if resp2.status == 200:
                                    html2 = await resp2.text(errors="replace")
                                    rating2, count2 = self._parse_page_metadata(html2)
                                    if rating2 is not None and rating2 != rating:
                                        # Edge nodes returned conflicting ratings (e.g. 4.9 vs 5.0).
                                        # Prioritize authoritative backend api_rating
                                        # or confirmed reference.
                                        logger.warning(
                                            "2GIS CDN edge cache inconsistency detected for %s: "
                                            "fetch1=%s, fetch2=%s; retaining confirmed rating %s",
                                            source.branch_name,
                                            rating,
                                            rating2,
                                            reference_rating,
                                        )
                                        if api_rating is not None and api_rating in (
                                            rating,
                                            rating2,
                                        ):
                                            effective_rating = api_rating
                                        else:
                                            effective_rating = reference_rating
                                    elif rating2 == rating:
                                        # Both independent fetches agree on the new rating.
                                        effective_rating = rating
                                    if count2 is not None and count is not None:
                                        effective_count = max(count, count2)
                        except Exception as conf_exc:
                            logger.debug(
                                "Failed confirmation fetch for %s: %s", source.branch_name, conf_exc
                            )

                    # If the API rating and current rating both agree (e.g. 5.0),
                    # never let a single inconsistent page fetch downgrade it.
                    if (
                        api_rating is not None
                        and curr_rating is not None
                        and api_rating == curr_rating
                        and effective_rating != api_rating
                    ):
                        logger.info(
                            "2GIS page rating %s disagrees with API & current rating %s for %s, "
                            "retaining %s",
                            effective_rating,
                            api_rating,
                            source.branch_name,
                            api_rating,
                        )
                        effective_rating = api_rating

                    self._ratings_updated_at[source.external_id] = time.time()
                    if effective_rating is not None and 1.0 <= effective_rating <= 5.0:
                        self._branch_ratings[source.external_id] = effective_rating
                    if effective_count is not None:
                        self._branch_counts[source.external_id] = effective_count
                    return effective_rating, effective_count
        except Exception as exc:
            logger.debug("Failed to fetch 2GIS page metadata for %s: %s", source.branch_name, exc)
        return self._branch_ratings.get(source.external_id), self._branch_counts.get(
            source.external_id
        )

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
                rating, count = self._parse_page_metadata(html)
                for ext_id in list(self._branch_ratings.keys()):
                    if ext_id in firm_url:
                        if rating is not None:
                            self._branch_ratings[ext_id] = rating
                        if count is not None:
                            self._branch_counts[ext_id] = count
                        self._ratings_updated_at[ext_id] = time.time()
                return match is not None
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
            offset = max(0, (page - 1) * page_size)
            params = {
                "limit": str(page_size),
                "offset": str(offset),
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
                        cached_rating = self._branch_ratings.get(source.external_id)
                        cached_count = self._branch_counts.get(source.external_id)
                        if rotated:
                            continue
                        return (
                            ReviewSyncStatus.SOURCE_BLOCKED,
                            [],
                            f"2GIS API key rejected (HTTP {http_status}) and rotation failed",
                            http_status,
                            cached_rating,
                            cached_count,
                        )

                    cached_rating = self._branch_ratings.get(source.external_id)
                    cached_count = self._branch_counts.get(source.external_id)

                    if http_status == 429:
                        return (
                            ReviewSyncStatus.RATE_LIMITED,
                            [],
                            "Rate limited by 2GIS (HTTP 429)",
                            429,
                            cached_rating,
                            cached_count,
                        )
                    if http_status == 403:
                        return (
                            ReviewSyncStatus.SOURCE_BLOCKED,
                            [],
                            "Access forbidden by 2GIS (HTTP 403)",
                            403,
                            cached_rating,
                            cached_count,
                        )
                    if http_status != 200:
                        return (
                            ReviewSyncStatus.HTTP_ERROR,
                            [],
                            f"HTTP {http_status}",
                            http_status,
                            cached_rating,
                            cached_count,
                        )

                    data = await resp.json()
                    if not isinstance(data, dict) or not isinstance(data.get("reviews"), list):
                        return (
                            ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                            [],
                            "Invalid response schema: expected reviews list",
                            200,
                            cached_rating,
                            cached_count,
                        )
                    reviews_data = data["reviews"]
                    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
                    branch_rating = None
                    if meta.get("branch_rating") is not None:
                        try:
                            b_r = round(float(meta["branch_rating"]), 1)
                            if 1.0 <= b_r <= 5.0:
                                branch_rating = b_r
                        except (ValueError, TypeError):
                            pass

                    total_count = None
                    if meta.get("total_count") is not None:
                        try:
                            c_v = int(meta["total_count"])
                            if c_v >= 0:
                                total_count = c_v
                        except (ValueError, TypeError):
                            pass

                    # Ensure we have fresh rating and count directly from the 2GIS firm page
                    now_ts = time.time()
                    if (now_ts - self._ratings_updated_at.get(source.external_id, 0.0)) > 600.0:
                        await self._fetch_branch_metadata(source, api_rating=branch_rating)

                    final_rating = self._branch_ratings.get(source.external_id)
                    if final_rating is None:
                        final_rating = branch_rating

                    final_count = self._branch_counts.get(source.external_id)
                    if final_count is None:
                        final_count = total_count

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
                        final_rating,
                        final_count,
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
