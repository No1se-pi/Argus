import asyncio
import http.cookiejar
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.reviews.models import ReviewItem, ReviewPlatform, ReviewSource, ReviewSyncStatus

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def djb2_hash(s: str) -> str:
    h = 5381
    for ch in s:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    return str(h)


def stringify_yandex_params(params: dict[str, Any]) -> str:
    sorted_keys = sorted(params.keys(), key=lambda k: k.lower())
    parts = [f"{k}={urllib.parse.quote(str(params[k]), safe='')}" for k in sorted_keys]
    return "&".join(parts)


class YandexReviewsClient:
    """Lightweight HTTP client for Yandex Maps reviews using standard library urllib & cookiejar.

    Executes via asyncio.to_thread to preserve non-blocking event-loop operation
    while avoiding TLS fingerprinting issues associated with certain async HTTP clients.
    """

    def __init__(
        self,
        session: Any = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout_seconds
        self._cookie_jar: http.cookiejar.CookieJar | None = None
        self._opener: urllib.request.OpenerDirector | None = None
        self._csrf_token: str | None = None
        self._session_id: str | None = None
        self._locale: str = "ru_RU"
        self._base_origin: str = "https://yandex.ru"
        self._branch_ratings: dict[str, float] = {}
        self._branch_counts: dict[str, int] = {}
        self._ratings_updated_at: dict[str, float] = {}
        self._session_created_at: float = 0.0
        self._session_ttl_seconds: float = 14400.0  # 4 hours proactive refresh

    def _reset_session(self) -> None:
        self._csrf_token = None
        self._session_id = None
        self._session_created_at = 0.0
        if self._cookie_jar is not None:
            try:
                self._cookie_jar.clear()
            except Exception:
                pass

    @property
    def _last_rating(self) -> float | None:
        return None

    @property
    def _last_count(self) -> int | None:
        return None

    async def close(self) -> None:
        pass

    def _create_opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            self._cookie_jar = http.cookiejar.CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._cookie_jar)
            )
        return self._opener

    def _parse_page_metadata(self, html: str) -> tuple[float | None, int | None]:
        scripts = re.findall(
            r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not scripts:
            return None, None
        try:
            page_data = json.loads(scripts[0])
            stack = page_data.get("stack", [])
            if isinstance(stack, list) and len(stack) > 0 and isinstance(stack[0], dict):
                results = stack[0].get("results", {}) if isinstance(stack[0], dict) else {}
                items = results.get("items", []) if isinstance(results, dict) else []
                if isinstance(items, list) and len(items) > 0 and isinstance(items[0], dict):
                    rating_data = items[0].get("ratingData", {})
                    if isinstance(rating_data, dict):
                        rating = None
                        count = None
                        val = rating_data.get("ratingValue")
                        if val is not None:
                            try:
                                r_val = round(float(val), 1)
                                if 1.0 <= r_val <= 5.0:
                                    rating = r_val
                            except (ValueError, TypeError):
                                pass
                        cnt = rating_data.get("reviewCount")
                        if cnt is not None:
                            try:
                                c_val = int(cnt)
                                if c_val >= 0:
                                    count = c_val
                            except (ValueError, TypeError):
                                pass
                        return rating, count
        except Exception:
            pass
        return None, None

    def _fetch_branch_rating(self, source: ReviewSource) -> float | None:
        try:
            opener = self._create_opener()
            page_req = urllib.request.Request(
                source.url,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            with opener.open(page_req, timeout=self._timeout) as resp:
                final_url = resp.geturl()
                html = resp.read().decode("utf-8", errors="replace")

            if "showcaptcha" in final_url or "showcaptcha" in html.lower():
                return None

            rating, count = self._parse_page_metadata(html)
            self._ratings_updated_at[source.external_id] = time.time()
            if rating is not None and 1.0 <= rating <= 5.0:
                self._branch_ratings[source.external_id] = rating
            if count is not None:
                prev_cnt = self._branch_counts.get(source.external_id)
                if count > 0 or prev_cnt is None or prev_cnt == 0:
                    self._branch_counts[source.external_id] = count
            return self._branch_ratings.get(source.external_id)
        except Exception as exc:
            logger.debug("Failed to fetch branch rating for %s: %s", source.branch_name, exc)
            return None

    def _initialize_session(
        self,
        source: ReviewSource,
    ) -> tuple[ReviewSyncStatus, str | None, int | None]:
        opener = self._create_opener()
        try:
            page_req = urllib.request.Request(
                source.url,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            with opener.open(page_req, timeout=self._timeout) as resp:
                final_url = resp.geturl()
                html = resp.read().decode("utf-8", errors="replace")

            if "showcaptcha" in final_url or "showcaptcha" in html.lower():
                return ReviewSyncStatus.CAPTCHA, "Captcha challenge encountered", 403

            parsed = urllib.parse.urlparse(final_url)
            self._base_origin = f"{parsed.scheme}://{parsed.netloc}"

            scripts = re.findall(
                r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
                html,
                re.DOTALL,
            )
            if not scripts:
                return (
                    ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                    "No application/json state found",
                    200,
                )

            page_data = json.loads(scripts[0])
            cfg = page_data.get("config", {}) if isinstance(page_data, dict) else {}
            self._csrf_token = cfg.get("csrfToken") if isinstance(cfg, dict) else None
            counters = (
                cfg.get("counters", {})
                if isinstance(cfg, dict) and isinstance(cfg.get("counters"), dict)
                else {}
            )
            analytics = (
                counters.get("analytics", {}) if isinstance(counters.get("analytics"), dict) else {}
            )
            self._session_id = analytics.get("sessionId")
            self._locale = cfg.get("locale", "ru_RU") if isinstance(cfg, dict) else "ru_RU"

            rating, count = self._parse_page_metadata(html)
            self._ratings_updated_at[source.external_id] = time.time()
            if rating is not None and 1.0 <= rating <= 5.0:
                self._branch_ratings[source.external_id] = rating
            if count is not None:
                prev_c = self._branch_counts.get(source.external_id)
                if count > 0 or prev_c is None or prev_c == 0:
                    self._branch_counts[source.external_id] = count

            if not self._csrf_token or not self._session_id:
                return ReviewSyncStatus.PARSER_FORMAT_CHANGED, "Missing csrfToken or sessionId", 200

            self._session_created_at = time.time()
            return ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, None, 200

        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return ReviewSyncStatus.RATE_LIMITED, "Rate limited by Yandex (HTTP 429)", 429
            if exc.code == 403:
                return ReviewSyncStatus.SOURCE_BLOCKED, "Access forbidden by Yandex (HTTP 403)", 403
            return ReviewSyncStatus.HTTP_ERROR, f"HTTP {exc.code}", exc.code
        except urllib.error.URLError as exc:
            return ReviewSyncStatus.NETWORK_ERROR, f"Network error: {exc.reason}", None
        except Exception as exc:
            return ReviewSyncStatus.UNKNOWN_ERROR, f"Unexpected error: {exc}", None

    def _sync_fetch(
        self,
        source: ReviewSource,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[
        ReviewSyncStatus, list[ReviewItem], str | None, int | None, float | None, int | None
    ]:
        # Step 1: Initialize session credentials if needed
        now_ts = time.time()
        is_session_stale = (
            self._session_created_at > 0.0
            and (now_ts - self._session_created_at) > self._session_ttl_seconds
        )
        if (
            not self._csrf_token
            or not self._session_id
            or self._opener is None
            or is_session_stale
        ):
            if is_session_stale:
                self._reset_session()
            init_status, err, http_code = self._initialize_session(source)
            if init_status != ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS:
                return (
                    init_status,
                    [],
                    err,
                    http_code,
                    self._branch_ratings.get(source.external_id),
                    self._branch_counts.get(source.external_id),
                )

        # Step 2: Call fetchReviews endpoint (with retries for transient upstream 500 errors)
        max_attempts = 4
        session_refreshed = False
        for attempt in range(max_attempts):
            api_params = {
                "ajax": "1",
                "businessId": source.external_id,
                "csrfToken": self._csrf_token,
                "locale": self._locale or "ru_RU",
                "page": str(page),
                "pageSize": str(page_size),
                "ranking": "by_time",
                "sessionId": self._session_id,
            }
            api_params["s"] = djb2_hash(stringify_yandex_params(api_params))
            encoded_params = urllib.parse.urlencode(api_params)
            api_url = f"{self._base_origin}/maps/api/business/fetchReviews?{encoded_params}"

            api_req = urllib.request.Request(
                api_url,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Retpath-Y": source.url,
                    "Referer": source.url,
                },
            )

            try:
                assert self._opener is not None
                with self._opener.open(api_req, timeout=self._timeout) as resp:
                    raw_body = resp.read().decode("utf-8")
                    data = json.loads(raw_body)
                    if (
                        not isinstance(data, dict)
                        or not isinstance(data.get("data"), dict)
                        or not isinstance(data["data"].get("reviews"), list)
                    ):
                        is_csrf_refresh = isinstance(data, dict) and (
                            "csrfToken" in data or "csrf" in data
                        )
                        err_info = data.get("error") if isinstance(data, dict) else None
                        try:
                            err_code = (
                                int(err_info.get("code")) if isinstance(err_info, dict) else None
                            )
                        except (TypeError, ValueError):
                            err_code = None
                        if (
                            (is_csrf_refresh or err_code in (401, 403))
                            and not session_refreshed
                            and attempt < max_attempts - 1
                        ):
                            session_refreshed = True
                            self._reset_session()
                            init_status, err, http_code = self._initialize_session(source)
                            if init_status == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS:
                                continue
                            return (
                                init_status, [], f"Session refresh failed: {err}", http_code,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )

                        if isinstance(data, dict) and "error" in data:
                            if err_code in (500, 502, 503, 504) and attempt < max_attempts - 1:
                                logger.info(
                                    "Yandex upstream error %s for %s; retry %d/%d",
                                    err_code, source.branch_name, attempt + 1, max_attempts,
                                )
                                time.sleep(2.0 ** attempt)
                                continue
                            # HTML snapshots are neither fresh nor guaranteed to be sorted
                            # by time. Never let one advance the pagination/health state.
                            failure = ReviewSyncStatus.HTTP_ERROR
                            if err_code == 429:
                                failure = ReviewSyncStatus.RATE_LIMITED
                            elif err_code in (401, 403):
                                failure = ReviewSyncStatus.SOURCE_BLOCKED
                            return (
                                failure, [], f"Yandex API error {err_code}",
                                err_code if err_code is not None else 200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )
                        else:
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                "Invalid response schema: expected data.reviews list",
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )
                    else:
                        reviews_data = data["data"]["reviews"]
                        params_dict = data["data"].get("params", {})
                        if isinstance(params_dict, dict) and "count" in params_dict:
                            try:
                                new_cnt = int(params_dict["count"])
                                prev_c = self._branch_counts.get(source.external_id)
                                if new_cnt > 0 or prev_c is None or prev_c == 0:
                                    self._branch_counts[source.external_id] = new_cnt
                            except (ValueError, TypeError):
                                pass

                    # Ensure we have branch rating for this specific source
                    now_ts = time.time()
                    if (now_ts - self._ratings_updated_at.get(source.external_id, 0)) > 600:
                        self._fetch_branch_rating(source)

                    items: list[ReviewItem] = []
                    for r in reviews_data:
                        if not isinstance(r, dict):
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                "Invalid review item structure: expected dict",
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )
                        rev_id = str(r.get("reviewId", "")).strip()
                        if not rev_id:
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                "Missing reviewId in Yandex review item",
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )

                        # Strict rating validation: Never invent rating=5
                        raw_rating = r.get("rating")
                        if raw_rating is None:
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                f"Missing rating in Yandex review item {rev_id}",
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )
                        try:
                            rating = int(raw_rating)
                        except (ValueError, TypeError):
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                (
                                    f"Invalid non-integer rating '{raw_rating}' "
                                    f"for Yandex review {rev_id}"
                                ),
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )
                        if not (1 <= rating <= 5):
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                f"Rating {rating} out of range [1, 5] for Yandex review {rev_id}",
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )

                        # Strict updatedTime validation
                        raw_updated = r.get("updatedTime")
                        if raw_updated is None or str(raw_updated).strip() == "":
                            return (
                                ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                                [],
                                f"Missing updatedTime for Yandex review {rev_id}",
                                200,
                                self._branch_ratings.get(source.external_id),
                                self._branch_counts.get(source.external_id),
                            )
                        published_at = str(raw_updated).strip()

                        author = (
                            str(r.get("author", {}).get("name") or "Аноним").strip()
                            if isinstance(r.get("author"), dict)
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
                                platform=ReviewPlatform.YANDEX,
                                branch_name=source.branch_name,
                                author_name=author,
                                rating=rating,
                                text=text,
                                published_at=published_at,
                                edited_at=None,
                                review_url=rev_url,
                            )
                        )

                    return (
                        ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
                        items,
                        None,
                        200,
                        self._branch_ratings.get(source.external_id),
                        self._branch_counts.get(source.external_id),
                    )

            except urllib.error.HTTPError as exc:
                if exc.code in (500, 502, 503, 504) and attempt < max_attempts - 1:
                    logger.info(
                        "Yandex fetchReviews returned HTTP %d for %s, retrying attempt %d/%d...",
                        exc.code,
                        source.branch_name,
                        attempt + 1,
                        max_attempts,
                    )
                    time.sleep(2.0 ** attempt)
                    continue
                if (
                    exc.code in (401, 403)
                    and not session_refreshed
                    and attempt < max_attempts - 1
                ):
                    session_refreshed = True
                    logger.info(
                        "Yandex fetchReviews returned %d, refreshing session before retry...",
                        exc.code,
                    )
                    self._reset_session()
                    init_status, err, http_code = self._initialize_session(source)
                    if init_status != ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS:
                        return (
                            init_status,
                            [],
                            f"Session refresh failed: {err}",
                            http_code,
                            self._branch_ratings.get(source.external_id),
                            self._branch_counts.get(source.external_id),
                        )
                    continue
                if exc.code == 429:
                    return (
                        ReviewSyncStatus.RATE_LIMITED,
                        [],
                        "Rate limited (HTTP 429)",
                        429,
                        self._branch_ratings.get(source.external_id),
                        self._branch_counts.get(source.external_id),
                    )
                if exc.code == 403:
                    return (
                        ReviewSyncStatus.SOURCE_BLOCKED,
                        [],
                        "Forbidden (HTTP 403)",
                        403,
                        self._branch_ratings.get(source.external_id),
                        self._branch_counts.get(source.external_id),
                    )
                return (
                    ReviewSyncStatus.HTTP_ERROR,
                    [],
                    f"HTTP {exc.code}",
                    exc.code,
                    self._branch_ratings.get(source.external_id),
                    self._branch_counts.get(source.external_id),
                )
            except urllib.error.URLError as exc:
                return (
                    ReviewSyncStatus.NETWORK_ERROR,
                    [],
                    f"Network error: {exc.reason}",
                    None,
                    self._branch_ratings.get(source.external_id),
                    self._branch_counts.get(source.external_id),
                )
            except json.JSONDecodeError as exc:
                return (
                    ReviewSyncStatus.PARSER_FORMAT_CHANGED,
                    [],
                    f"JSON parse error: {exc}",
                    200,
                    self._branch_ratings.get(source.external_id),
                    self._branch_counts.get(source.external_id),
                )
            except Exception as exc:
                return (
                    ReviewSyncStatus.UNKNOWN_ERROR,
                    [],
                    f"Unexpected error: {exc}",
                    None,
                    self._branch_ratings.get(source.external_id),
                    self._branch_counts.get(source.external_id),
                )

        return (
            ReviewSyncStatus.SOURCE_BLOCKED,
            [],
            "Failed after session refresh",
            403,
            self._branch_ratings.get(source.external_id),
            self._branch_counts.get(source.external_id),
        )

    async def fetch_reviews(
        self,
        source: ReviewSource,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[
        ReviewSyncStatus,
        list[ReviewItem],
        str | None,
        int | None,
        float | None,
        int | None,
    ]:
        return await asyncio.to_thread(self._sync_fetch, source, page, page_size)
