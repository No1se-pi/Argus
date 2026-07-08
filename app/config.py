from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    enable_telegram_bot_ui: bool = True
    enable_vk_monitor: bool = True
    enable_telegram_monitor: bool = False

    fail_fast: bool = False
    require_vk: bool = False
    require_telethon: bool = False

    bot_token: SecretStr
    admin_ids_text: str = Field(default="", validation_alias="ADMIN_IDS")

    vk_group_token: SecretStr | None = None
    vk_user_access_token: SecretStr | None = None
    vk_access_token: SecretStr | None = None
    vk_group_id: int | None = None
    vk_monitor_mode: str = "longpoll"
    vk_enable_polling_fallback: bool = True
    vk_recent_posts_limit: int = 15
    vk_comments_poll_interval_seconds: int = 2700
    vk_posts_poll_interval_seconds: int = 900
    vk_api_version: str = "5.199"

    tg_api_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("TG_API_ID", "TELEGRAM_API_ID"),
    )
    tg_api_hash: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TG_API_HASH", "TELEGRAM_API_HASH"),
    )
    tg_session_name: str = Field(
        default="argus_user",
        validation_alias=AliasChoices("TG_SESSION_NAME", "TELETHON_SESSION_NAME"),
    )
    tg_session_dir: Path = Field(
        default=Path("sessions"),
        validation_alias=AliasChoices("TG_SESSION_DIR", "TELETHON_SESSION_DIR"),
    )
    legacy_telethon_session: str | None = Field(default=None, validation_alias="TELETHON_SESSION")
    telegram_phone: str | None = None

    database_path: Path = Path("data/argus.sqlite3")
    alert_chat_id: int | None = None
    alerts_vk_enabled: bool = True
    alerts_vk_posts_enabled: bool = True
    alerts_vk_comments_enabled: bool = True
    alerts_telegram_enabled: bool = True
    alerts_telegram_posts_enabled: bool = True
    alerts_telegram_comments_enabled: bool = True
    alerts_telegram_keywords_enabled: bool = True

    poll_interval_seconds: int = 60
    source_sync_pause_seconds: float = 2.0
    sync_limit: int = 20
    initial_bootstrap_limit: int = 1
    comments_limit_per_post: int = 100
    reactions_sync_limit: int = 50
    tg_discussion_fetch_limit: int = 20
    tg_tracked_posts_limit: int = 20
    tg_comment_alerts_per_cycle: int = 10
    tg_reactions_sync_interval_seconds: int = 1800
    flood_wait_small_seconds: int = 60
    log_level: str = "INFO"

    @field_validator("alert_chat_id", "tg_api_id", "vk_group_id", mode="before")
    @classmethod
    def parse_optional_int(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        return int(value)  # type: ignore[arg-type]

    @field_validator(
        "vk_access_token",
        "vk_group_token",
        "vk_user_access_token",
        "tg_api_hash",
        mode="before",
    )
    @classmethod
    def parse_optional_secret(cls, value: object) -> object | None:
        if value is None or value == "":
            return None
        return value

    @property
    def admin_ids(self) -> set[int]:
        return {int(item.strip()) for item in self.admin_ids_text.split(",") if item.strip()}

    @property
    def telethon_session(self) -> str:
        if self.legacy_telethon_session:
            return self.legacy_telethon_session
        return str(self.tg_session_dir / self.tg_session_name)

    @property
    def telethon_session_file(self) -> Path:
        if self.legacy_telethon_session:
            legacy_path = Path(self.legacy_telethon_session)
            if legacy_path.suffix == ".session":
                return legacy_path
            return legacy_path.with_suffix(".session")
        session_path = self.tg_session_dir / self.tg_session_name
        if session_path.suffix == ".session":
            return session_path
        return session_path.with_suffix(".session")

    @property
    def has_telegram_monitor_config(self) -> bool:
        return self.tg_api_id is not None and self.tg_api_hash is not None

    @property
    def has_vk_config(self) -> bool:
        return (
            self.vk_group_token is not None
            or self.vk_user_access_token is not None
            or self.vk_access_token is not None
        ) and self.vk_group_id is not None


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.admin_ids:
        raise ValueError("ADMIN_IDS must contain at least one Telegram user id.")
    return settings
