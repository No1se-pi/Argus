from dataclasses import dataclass, field
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # noqa: UP042
        pass


class ReviewPlatform(StrEnum):
    YANDEX = "yandex"
    DGIS = "2gis"


class ReviewSyncStatus(StrEnum):
    SUCCESS_NO_NEW_REVIEWS = "SUCCESS_NO_NEW_REVIEWS"
    SUCCESS_NEW_REVIEWS = "SUCCESS_NEW_REVIEWS"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    CAPTCHA = "CAPTCHA"
    PARSER_FORMAT_CHANGED = "PARSER_FORMAT_CHANGED"
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ReviewsSyncAlreadyRunningError(Exception):
    """Raised when manual or background sync is attempted while another sync is in progress."""

    pass


@dataclass
class ReviewItem:
    external_review_id: str
    platform: ReviewPlatform
    branch_name: str
    author_name: str
    rating: int
    text: str
    published_at: str  # ISO 8601 string. Note: For Yandex, mapped from updatedTime.
    edited_at: str | None = None  # Note: For 2GIS, mapped from date_edited.
    review_url: str | None = None
    raw_payload_json: str | None = None
    id: int | None = None
    source_id: int | None = None
    telegram_parts_total: int = 1
    telegram_parts_sent: int = 0
    is_sent_to_telegram: bool = False
    telegram_sent_at: str | None = None
    last_delivery_error: str | None = None

    @property
    def is_empty_text(self) -> bool:
        return not self.text or self.text.strip() in ("", "TEXT_EMPTY")


@dataclass
class ReviewSource:
    id: int
    platform: ReviewPlatform
    branch_name: str
    external_id: str
    url: str
    is_active: bool = True
    is_initialized: bool = False
    last_status: str = "PENDING"
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    health_alert_active: int = 0
    health_alert_sent_at: str | None = None
    backoff_until: str | None = None
    last_rating: float | None = None
    total_reviews_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ReviewSyncResult:
    source: ReviewSource
    status: ReviewSyncStatus
    fetched_reviews: list[ReviewItem] = field(default_factory=list)
    new_reviews: list[ReviewItem] = field(default_factory=list)
    error_message: str | None = None
    http_status: int | None = None
    latency_ms: int = 0

    @property
    def is_success(self) -> bool:
        return self.status in (
            ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            ReviewSyncStatus.SUCCESS_NEW_REVIEWS,
        )

    @property
    def is_error(self) -> bool:
        return not self.is_success


@dataclass(frozen=True)
class ReviewsEffectiveConfig:
    enabled: bool
    poll_interval_seconds: int
    request_pause_seconds: float
    fetch_page_size: int
    max_catchup_reviews: int
    error_alert_threshold: int
    max_backoff_seconds: int
    alerts_enabled: bool


DEFAULT_SOURCES: list[dict[str, Any]] = [
    # --- YANDEX MAPS (6 branches) ---
    {
        "platform": ReviewPlatform.YANDEX,
        "branch_name": "УО Датахаб",
        "external_id": "1093602317",
        "url": "https://yandex.ru/maps/org/kolledzh_avtomatizatsii_i_informatsionnykh_tekhnologiy_20_uchebnoye_otdeleniye_datakhab/1093602317/reviews/",
    },
    {
        "platform": ReviewPlatform.YANDEX,
        "branch_name": "УО Моссовет",
        "external_id": "178601188856",
        "url": "https://yandex.ru/maps/org/uchebnoye_otdeleniye_mossovet/178601188856/reviews/",
    },
    {
        "platform": ReviewPlatform.YANDEX,
        "branch_name": "УО Диджитал",
        "external_id": "1011075765",
        "url": "https://yandex.ru/maps/org/uchebnoye_otdeleniye_didzhital/1011075765/reviews/",
    },
    {
        "platform": ReviewPlatform.YANDEX,
        "branch_name": "УО АртТех",
        "external_id": "1092943436",
        "url": "https://yandex.ru/maps/org/uchebnoye_otdeleniye_arttekh/1092943436/reviews/",
    },
    {
        "platform": ReviewPlatform.YANDEX,
        "branch_name": "УО Техно",
        "external_id": "1077069995",
        "url": "https://yandex.ru/maps/org/uchebnoye_otdeleniye_tekhno/1077069995/reviews/",
    },
    {
        "platform": ReviewPlatform.YANDEX,
        "branch_name": "УО Кибер",
        "external_id": "1003750456",
        "url": "https://yandex.ru/maps/org/uchebnoye_otdeleniye_kiber/1003750456/reviews/",
    },
    # --- 2GIS (6 branches) ---
    {
        "platform": ReviewPlatform.DGIS,
        "branch_name": "УО Датахаб",
        "external_id": "4504127908536611",
        "url": "https://2gis.ru/moscow/firm/4504127908536611/tab/reviews",
    },
    {
        "platform": ReviewPlatform.DGIS,
        "branch_name": "УО Техно",
        "external_id": "4504127908536614",
        "url": "https://2gis.ru/moscow/firm/4504127908536614/tab/reviews",
    },
    {
        "platform": ReviewPlatform.DGIS,
        "branch_name": "УО АртТех",
        "external_id": "4504127908536613",
        "url": "https://2gis.ru/moscow/firm/4504127908536613/tab/reviews",
    },
    {
        "platform": ReviewPlatform.DGIS,
        "branch_name": "УО Кибер",
        "external_id": "4504127908544789",
        "url": "https://2gis.ru/moscow/firm/4504127908544789/tab/reviews",
    },
    {
        "platform": ReviewPlatform.DGIS,
        "branch_name": "УО Диджитал",
        "external_id": "4504127908536612",
        "url": "https://2gis.ru/moscow/firm/4504127908536612/tab/reviews",
    },
    {
        "platform": ReviewPlatform.DGIS,
        "branch_name": "УО Моссовет",
        "external_id": "4504127908875284",
        "url": "https://2gis.ru/moscow/firm/4504127908875284/tab/reviews",
    },
]
