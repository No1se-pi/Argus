import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from telethon import TelegramClient

from app.alerts.service import AlertService
from app.bot.factory import create_dispatcher
from app.collectors.telegram import TelegramCollector
from app.config import get_settings
from app.logging import setup_logging
from app.modules import ModuleRegistry
from app.scheduler.jobs import BackgroundScheduler
from app.scheduler.rate_limit import TelegramRateLimiter
from app.storage.database import Database
from app.storage.repositories import RepositoryBundle
from app.storage.schema import init_schema
from app.vk.scheduler import VKPollingScheduler
from app.vk.service import VKService

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    database = Database(settings.database_path)
    await database.connect()
    await init_schema(database)
    repositories = RepositoryBundle(database)

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    alert_service = AlertService(bot=bot, settings=settings, alerts=repositories.alerts)

    telegram_client, collector = await _start_telegram_monitor(settings, repositories)
    vk_service = VKService(
        settings=settings,
        runtime_settings=repositories.runtime_settings,
        repository=repositories.vk,
    )
    module_registry = ModuleRegistry(
        settings=settings,
        runtime_settings=repositories.runtime_settings,
        vk_service=vk_service,
        telegram_collector=collector,
    )

    dispatcher = create_dispatcher(
        settings=settings,
        source_repo=repositories.sources,
        collector=collector,
        dashboard_service=repositories.dashboard_service(),
        scheduler_state_repo=repositories.scheduler_state,
        module_registry=module_registry,
        vk_service=vk_service,
        runtime_settings_repo=repositories.runtime_settings,
    )

    schedulers = []
    tasks: list[asyncio.Task] = []
    if collector is not None:
        telegram_scheduler = BackgroundScheduler(
            settings=settings,
            sources=repositories.sources,
            scheduler_state=repositories.scheduler_state,
            collector=collector,
            alerts=alert_service,
            runtime_settings=repositories.runtime_settings,
        )
        schedulers.append(telegram_scheduler)
        tasks.append(asyncio.create_task(telegram_scheduler.run(), name="argus-telegram-scheduler"))

    vk_scheduler = VKPollingScheduler(settings=settings, service=vk_service, alerts=alert_service)
    schedulers.append(vk_scheduler)
    tasks.append(asyncio.create_task(vk_scheduler.run(), name="argus-vk-scheduler"))

    logger.info("Argus started")

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        logger.info("Argus shutdown started")
        for scheduler in schedulers:
            scheduler.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        if telegram_client is not None:
            await telegram_client.disconnect()
        await database.close()


async def _start_telegram_monitor(
    settings,
    repositories: RepositoryBundle,
) -> tuple[TelegramClient | None, TelegramCollector | None]:
    if not settings.enable_telegram_monitor and not settings.require_telethon:
        logger.info("Telegram Monitor is disabled")
        return None, None

    if not settings.has_telegram_monitor_config:
        logger.warning("Telegram Monitor config is missing; Bot UI will keep running")
        if settings.require_telethon and settings.fail_fast:
            raise RuntimeError("TG_API_ID/TG_API_HASH are required.")
        return None, None

    if not settings.telethon_session_file.exists():
        logger.warning("Telethon session file is missing: %s", settings.telethon_session_file)
        if settings.require_telethon and settings.fail_fast:
            raise RuntimeError("Telethon session file is required.")
        return None, None

    settings.telethon_session_file.parent.mkdir(parents=True, exist_ok=True)
    telegram_client = TelegramClient(
        settings.telethon_session,
        settings.tg_api_id,
        settings.tg_api_hash.get_secret_value(),
    )
    try:
        await telegram_client.connect()
        if not await telegram_client.is_user_authorized():
            await telegram_client.disconnect()
            logger.warning("Telethon session exists but is not authorized")
            return None, None
    except Exception:
        logger.exception("Failed to start Telegram Monitor")
        with contextlib.suppress(Exception):
            await telegram_client.disconnect()
        if settings.require_telethon and settings.fail_fast:
            raise
        return None, None

    rate_limiter = TelegramRateLimiter(min_delay_seconds=settings.source_sync_pause_seconds)
    collector = TelegramCollector(
        client=telegram_client,
        settings=settings,
        repositories=repositories,
        rate_limiter=rate_limiter,
    )
    return telegram_client, collector


if __name__ == "__main__":
    asyncio.run(main())
