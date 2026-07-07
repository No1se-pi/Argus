from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.analytics.dashboard import DashboardService
from app.analytics.periods import parse_period
from app.bot.keyboards import main_menu_keyboard, setup_cancel_keyboard, vk_menu_keyboard
from app.bot.screens import main_menu_text, modules_text, setup_text, status_text
from app.bot.states import VKSetupStates
from app.collectors.telegram import LargeFloodWait, TelegramCollector, TelegramSourceError
from app.config import Settings
from app.modules import ModuleRegistry, ModuleStatus
from app.storage.repositories import SourceRepository
from app.vk.service import VKService

router = Router(name="admin")


HELP_TEXT = """<b>Argus commands</b>

Core:
/start - открыть панель управления
/menu - открыть панель управления
/help - помощь
/status - статус системы
/modules - статусы модулей
/setup - настройка

VK:
/vk_status
/vk_setup
/vk_recent_posts
/vk_recent_comments
/vk_sync
/vk_dashboard &lt;period&gt;
/vk_watch_on
/vk_watch_off

Telegram Monitor:
/tg_status
/tg_sources
/tg_dashboard &lt;source_id&gt; &lt;period&gt;
/tg_sync_posts &lt;source_id&gt;

Legacy Telegram commands still work when Telegram Monitor is available:
/sources
/add_source &lt;link_or_username&gt;
/remove_source &lt;source_id&gt;
/sync_posts &lt;source_id&gt;
/dashboard &lt;source_id&gt; &lt;period&gt;
"""


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, module_registry: ModuleRegistry) -> None:
    await message.answer(
        await main_menu_text(module_registry),
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("status"))
async def status_command(message: Message, module_registry: ModuleRegistry) -> None:
    await message.answer(await status_text(module_registry))


@router.message(Command("modules"))
async def modules_command(message: Message, module_registry: ModuleRegistry) -> None:
    await message.answer(await modules_text(module_registry))


@router.message(Command("setup"))
async def setup_command(message: Message, module_registry: ModuleRegistry) -> None:
    await message.answer(await setup_text(module_registry))


@router.message(Command("vk_status"))
async def vk_status_command(message: Message, vk_service: VKService) -> None:
    await message.answer(await vk_service.render_status())


@router.message(Command("vk_setup"))
async def vk_setup_command(message: Message, state: FSMContext, vk_service: VKService) -> None:
    config = await vk_service.effective_config()
    await state.clear()
    await state.update_data(access_token=None)
    if not config.group_token:
        await state.set_state(VKSetupStates.waiting_token)
        await message.answer(
            await vk_service.config_summary()
            + "\n\nОтправь VK_GROUP_TOKEN следующим сообщением.",
            reply_markup=setup_cancel_keyboard(can_skip=False),
        )
        return

    await state.set_state(VKSetupStates.waiting_group_id)
    await message.answer(
        await vk_service.config_summary() + "\n\nОтправь VK_GROUP_ID следующим сообщением.",
        reply_markup=setup_cancel_keyboard(can_skip=config.group_id is not None),
    )


@router.message(Command("vk_sync"))
async def vk_sync_command(message: Message, vk_service: VKService) -> None:
    try:
        result = await vk_service.sync_now()
    except Exception as exc:
        await message.answer(f"VK sync failed: {_format_error(exc)}")
        return

    await message.answer(
        "\n".join(
            [
                "<b>VK sync complete</b>",
                f"Постов обработано: {result.posts_processed}",
                f"Комментариев обработано: {result.comments_processed}",
                f"Новых постов: {len(result.new_posts)}",
                f"Новых комментариев: {len(result.new_comments)}",
            ]
        )
    )


@router.message(Command("vk_recent_posts"))
async def vk_recent_posts_command(message: Message, vk_service: VKService) -> None:
    try:
        await message.answer(await vk_service.render_recent_posts(), disable_web_page_preview=True)
    except Exception as exc:
        await message.answer(f"VK posts unavailable: {_format_error(exc)}")


@router.message(Command("vk_recent_comments"))
async def vk_recent_comments_command(message: Message, vk_service: VKService) -> None:
    try:
        await message.answer(await vk_service.render_recent_comments(), disable_web_page_preview=True)
    except Exception as exc:
        await message.answer(f"VK comments unavailable: {_format_error(exc)}")


@router.message(Command("vk_dashboard"))
async def vk_dashboard_command(message: Message, command: CommandObject, vk_service: VKService) -> None:
    period = _single_argument(command) or "7d"
    try:
        await message.answer(await vk_service.render_dashboard(period), disable_web_page_preview=True)
    except Exception as exc:
        await message.answer(f"VK dashboard unavailable: {_format_error(exc)}")


@router.message(Command("vk_watch_on"))
async def vk_watch_on_command(message: Message, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_vk_monitor", True)
    await message.answer("VK Monitor включён.")


@router.message(Command("vk_watch_off"))
async def vk_watch_off_command(message: Message, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_vk_monitor", False)
    await message.answer("VK Monitor выключен.")


@router.message(Command("tg_status"))
async def tg_status_command(message: Message, module_registry: ModuleRegistry) -> None:
    info = await module_registry.telegram_info()
    reason = f"\nReason: {escape(info.reason)}" if info.reason else ""
    await message.answer(f"<b>Telegram Monitor</b>\nStatus: {info.status.value}{reason}")


@router.message(Command("tg_sources", "sources"))
async def sources_command(
    message: Message,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if message.text and message.text.startswith("/tg_"):
        if not await _ensure_telegram_available(message, module_registry):
            return

    sources = await source_repo.list_sources()
    if not sources:
        await message.answer("Источников пока нет. Добавьте: /add_source <link_or_username>")
        return

    lines = ["<b>Telegram sources</b>"]
    for source in sources:
        username = f" @{escape(source.username)}" if source.username else ""
        last = source.last_message_id if source.last_message_id is not None else "not initialized"
        error = f"\n  last_error: {escape(source.last_error)}" if source.last_error else ""
        lines.append(
            f"#{source.id} {escape(source.display_name)}{username}\n"
            f"  last_message_id: {last}{error}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("add_source"))
async def add_source_command(
    message: Message,
    command: CommandObject,
    collector: TelegramCollector | None,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return

    argument = _single_argument(command)
    if argument is None:
        await message.answer("Использование: /add_source <link_or_username>")
        return

    try:
        resolved = await collector.resolve_source(argument)
        source = await source_repo.upsert_telegram_source(
            link=resolved.link,
            username=resolved.username,
            title=resolved.title,
            entity_id=resolved.entity_id,
            access_hash=resolved.access_hash,
            entity_type=resolved.entity_type,
        )
    except (TelegramSourceError, LargeFloodWait) as exc:
        await message.answer(_format_error(exc))
        return

    await message.answer(
        "\n".join(
            [
                "Источник добавлен.",
                f"ID: {source.id}",
                f"Название: {escape(source.display_name)}",
                "Автоматическая подписка не выполнялась.",
            ]
        )
    )


@router.message(Command("remove_source"))
async def remove_source_command(
    message: Message,
    command: CommandObject,
    source_repo: SourceRepository,
) -> None:
    source_id = _source_id_argument(command)
    if source_id is None:
        await message.answer("Использование: /remove_source <source_id>")
        return

    removed = await source_repo.deactivate(source_id)
    await message.answer("Источник отключён." if removed else "Источник не найден или уже отключён.")


@router.message(Command("tg_sync_posts", "sync_posts"))
async def sync_posts_command(
    message: Message,
    command: CommandObject,
    collector: TelegramCollector | None,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return

    source = await _get_active_source(message, command, source_repo, usage="/tg_sync_posts <source_id>")
    if source is None:
        return

    try:
        result = await collector.sync_posts(source)
    except (TelegramSourceError, LargeFloodWait) as exc:
        await message.answer(_format_error(exc))
        return

    if result.initialized:
        await message.answer(
            f"Базовая позиция сохранена: last_message_id={result.last_message_id}. "
            "Старые посты не алертятся."
        )
        return

    await message.answer(
        f"Синхронизация завершена. Получено: {result.fetched_count}, "
        f"новых сохранено: {result.saved_count}."
    )


@router.message(Command("tg_dashboard", "dashboard"))
async def dashboard_command(
    message: Message,
    command: CommandObject,
    dashboard_service: DashboardService,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return

    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer("Использование: /tg_dashboard <source_id> <period>, например /tg_dashboard 1 7d")
        return

    try:
        source_id = int(args[0])
        dashboard = await dashboard_service.render(source_id, args[1])
    except ValueError as exc:
        await message.answer(str(exc))
        return

    await message.answer(dashboard, disable_web_page_preview=True)


@router.message(Command("join_source"))
async def join_source_command(
    message: Message,
    command: CommandObject,
    collector: TelegramCollector | None,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return

    source = await _get_active_source(message, command, source_repo, usage="/join_source <source_id>")
    if source is None:
        return
    try:
        await collector.join_source(source)
    except (TelegramSourceError, LargeFloodWait) as exc:
        await message.answer(_format_error(exc))
        return
    await message.answer("User-аккаунт подписан на источник.")


@router.message(Command("leave_source"))
async def leave_source_command(
    message: Message,
    command: CommandObject,
    collector: TelegramCollector | None,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return

    source = await _get_active_source(message, command, source_repo, usage="/leave_source <source_id>")
    if source is None:
        return
    try:
        await collector.leave_source(source)
    except (TelegramSourceError, LargeFloodWait) as exc:
        await message.answer(_format_error(exc))
        return
    await message.answer("User-аккаунт отписан от источника.")


@router.message(Command("sync_comments"))
async def sync_comments_command(
    message: Message,
    command: CommandObject,
    collector: TelegramCollector | None,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return

    source = await _get_active_source(message, command, source_repo, usage="/sync_comments <source_id>")
    if source is None:
        return

    period = parse_period("7d")
    try:
        result = await collector.sync_comments(source, period.start_iso, period.end_iso)
    except LargeFloodWait as exc:
        await message.answer(_format_error(exc))
        return

    await message.answer(
        f"Комментарии: обработано постов {result.processed_posts}, сохранено {result.saved_items}."
    )


@router.message(Command("sync_reactions"))
async def sync_reactions_command(
    message: Message,
    command: CommandObject,
    collector: TelegramCollector | None,
    source_repo: SourceRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return

    source = await _get_active_source(message, command, source_repo, usage="/sync_reactions <source_id>")
    if source is None:
        return

    period = parse_period("7d")
    try:
        result = await collector.sync_reactions(source, period.start_iso, period.end_iso)
    except LargeFloodWait as exc:
        await message.answer(_format_error(exc))
        return

    await message.answer(
        f"Реакции: обработано постов {result.processed_posts}, snapshots {result.saved_items}."
    )


async def _ensure_telegram_available(message: Message, module_registry: ModuleRegistry) -> bool:
    info = await module_registry.telegram_info()
    if info.status == ModuleStatus.OK:
        return True
    reason = f"\nПричина: {escape(info.reason)}" if info.reason else ""
    await message.answer(f"Telegram Monitor сейчас недоступен.{reason}")
    return False


def _single_argument(command: CommandObject) -> str | None:
    args = (command.args or "").strip()
    if not args:
        return None
    return args.split()[0]


def _source_id_argument(command: CommandObject) -> int | None:
    argument = _single_argument(command)
    if argument is None:
        return None
    try:
        return int(argument)
    except ValueError:
        return None


async def _get_active_source(
    message: Message,
    command: CommandObject,
    source_repo: SourceRepository,
    *,
    usage: str,
):
    source_id = _source_id_argument(command)
    if source_id is None:
        await message.answer(f"Использование: {usage}")
        return None

    source = await source_repo.get_source(source_id)
    if source is None or not source.is_active:
        await message.answer("Источник не найден или отключён.")
        return None
    return source


def _format_error(exc: Exception) -> str:
    if isinstance(exc, LargeFloodWait):
        return f"Telegram ограничил запросы. Повторите после {exc.seconds} секунд."
    text = str(exc)
    if len(text) > 500:
        text = f"{text[:497]}..."
    return escape(text)
