from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.analytics.dashboard import DashboardService
from app.analytics.periods import parse_period
from app.bot.keyboards import (
    main_menu_keyboard,
    setup_cancel_keyboard,
    telegram_source_mode_keyboard,
    vk_setup_keyboard,
)
from app.bot.messages import answer_dashboard
from app.bot.screens import (
    main_menu_text,
    modules_text,
    setup_text,
    status_text,
    telegram_auth_cli_text,
)
from app.bot.states import TelegramSourceSetupStates
from app.collectors.telegram import LargeFloodWait, TelegramCollector, TelegramSourceError
from app.modules import ModuleRegistry, ModuleStatus
from app.storage.models import Post, Source, TelegramGroupMessage
from app.storage.repositories import (
    PostRepository,
    SourceRepository,
    TelegramGroupMessageRepository,
    TelegramKeywordRepository,
)
from app.telegram_auth import TelegramAuthService
from app.vk.service import VKService

router = Router(name="admin")
POST_LIST_MAX_LIMIT = 30


HELP_TEXT = """<b>Argus commands</b>

Core:
/start - открыть панель управления
/request_access - запросить доступ к боту
/menu - открыть панель управления
/help - помощь
/status - статус системы
/modules - статусы модулей
/setup - настройка

VK:
/vk_status
/vk_setup
/vk_recent_posts [limit]
/vk_posts &lt;period&gt; [limit]
/vk_recent_comments
/vk_sync
/vk_dashboard &lt;period&gt;
/vk_watch_on
/vk_watch_off

Telegram Monitor:
/tg_status
/tg_auth
/tg_add_source
/tg_sources
/tg_keywords
/tg_add_keyword &lt;text&gt;
/tg_remove_keyword &lt;id&gt;
/tg_recent_posts &lt;source_id_or_telegram_id&gt; [limit]
/tg_posts &lt;source_id_or_telegram_id&gt; &lt;period&gt; [limit]
/tg_dashboard &lt;source_id_or_telegram_id&gt; &lt;period&gt;
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


@router.message(Command("request_access"))
async def request_access_command(message: Message) -> None:
    await message.answer("У тебя уже есть доступ к Argus.")


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
    await state.clear()
    await message.answer(await vk_service.config_summary(), reply_markup=vk_setup_keyboard())


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
async def vk_recent_posts_command(
    message: Message,
    command: CommandObject,
    vk_service: VKService,
) -> None:
    try:
        limit = _limit_argument(command, default=10)
        await message.answer(
            await vk_service.render_recent_posts(limit=limit),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await message.answer(f"VK posts unavailable: {_format_error(exc)}")


@router.message(Command("vk_posts"))
async def vk_posts_command(
    message: Message,
    command: CommandObject,
    vk_service: VKService,
) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: /vk_posts &lt;period&gt; [limit], например /vk_posts 7d 10")
        return
    try:
        limit = _limit_from_parts(args, index=1, default=10)
        await message.answer(
            await vk_service.render_posts_by_period(args[0], limit=limit),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await message.answer(f"VK posts unavailable: {_format_error(exc)}")


@router.message(Command("vk_recent_comments"))
async def vk_recent_comments_command(message: Message, vk_service: VKService) -> None:
    try:
        await message.answer(
            await vk_service.render_recent_comments(),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await message.answer(f"VK comments unavailable: {_format_error(exc)}")


@router.message(Command("vk_dashboard"))
async def vk_dashboard_command(
    message: Message,
    command: CommandObject,
    vk_service: VKService,
) -> None:
    period = _single_argument(command) or "7d"
    try:
        text = await vk_service.render_dashboard(period)
        chart_png = await vk_service.render_dashboard_chart_png(period)
    except Exception as exc:
        await message.answer(f"VK dashboard unavailable: {_format_error(exc)}")
        return
    await answer_dashboard(
        message,
        text=text,
        chart_png=chart_png,
        filename=f"argus_vk_dashboard_{period}.png",
    )


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


@router.message(Command("tg_auth"))
async def tg_auth_command(
    message: Message,
    state: FSMContext,
    telegram_auth_service: TelegramAuthService,
) -> None:
    await state.clear()
    await message.answer(
        telegram_auth_cli_text(telegram_auth_service.is_configured()),
    )


@router.message(Command("tg_add_source"))
async def tg_add_source_command(
    message: Message,
    state: FSMContext,
    collector: TelegramCollector | None,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return
    await state.clear()
    await state.set_state(TelegramSourceSetupStates.waiting_forward)
    await message.answer(
        "Перешли сюда сообщение из канала или discussion-группы. "
        "После этого выберем режим мониторинга.",
        reply_markup=setup_cancel_keyboard(),
    )


@router.message(TelegramSourceSetupStates.waiting_forward)
async def tg_forwarded_source_message(
    message: Message,
    state: FSMContext,
    collector: TelegramCollector | None,
) -> None:
    if collector is None:
        await message.answer("Telegram Monitor недоступен: Telethon client is not running.")
        return
    try:
        resolved = await _resolve_source_from_forward(message, collector)
    except (TelegramSourceError, LargeFloodWait) as exc:
        await message.answer(_format_error(exc), reply_markup=setup_cancel_keyboard())
        return

    await state.update_data(
        link=resolved.link,
        username=resolved.username,
        title=resolved.title,
        entity_id=resolved.entity_id,
        access_hash=resolved.access_hash,
        entity_type=resolved.entity_type,
    )
    await message.answer(
        "\n".join(
            [
                "Источник найден.",
                f"Название: {escape(resolved.title)}",
                "Как мониторить этот источник?",
            ]
        ),
        reply_markup=telegram_source_mode_keyboard(),
    )


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
        await message.answer("Источников пока нет. Добавьте: /add_source &lt;link_or_username&gt;")
        return

    lines = ["<b>Telegram sources</b>"]
    for source in sources:
        username = f" @{escape(source.username)}" if source.username else ""
        last = source.last_message_id if source.last_message_id is not None else "not initialized"
        error = f"\n  last_error: {escape(source.last_error)}" if source.last_error else ""
        limit = source.tracked_posts_limit or "global"
        telegram_id = source.telegram_reference_id or "unknown"
        lines.append(
            f"#{source.id} {escape(source.display_name)}{username}\n"
            f"  telegram_id: {telegram_id}\n"
            f"  mode: {escape(source.telegram_monitor_mode)}, tracked_posts: {limit}\n"
            f"  last_message_id: {last}{error}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("tg_recent_posts"))
async def tg_recent_posts_command(
    message: Message,
    command: CommandObject,
    source_repo: SourceRepository,
    post_repo: PostRepository,
    group_message_repo: TelegramGroupMessageRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return

    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Использование: /tg_recent_posts &lt;source_id_or_telegram_id&gt; [limit], "
            "например /tg_recent_posts 1 10 или /tg_recent_posts -1001234567890 10"
        )
        return

    try:
        limit = _limit_from_parts(args, index=1, default=10)
    except ValueError:
        await message.answer("limit должен быть числом от 1 до 30.")
        return

    source = await _source_from_argument(message, source_repo, args[0])
    if source is None:
        return
    source_id = source.id

    if source.telegram_monitor_mode == "discussion":
        messages = await group_message_repo.list_recent(source_id, limit)
        text = _render_tg_messages(source, messages, limit=limit, title="TG recent discussion messages")
    else:
        posts = await post_repo.list_recent(source_id, limit)
        text = _render_tg_posts(source, posts, limit=limit, title="TG recent posts")
    await message.answer(text, disable_web_page_preview=True)


@router.message(Command("tg_posts"))
async def tg_posts_command(
    message: Message,
    command: CommandObject,
    source_repo: SourceRepository,
    post_repo: PostRepository,
    group_message_repo: TelegramGroupMessageRepository,
    module_registry: ModuleRegistry,
) -> None:
    if not await _ensure_telegram_available(message, module_registry):
        return

    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование: /tg_posts &lt;source_id_or_telegram_id&gt; &lt;period&gt; [limit], "
            "например /tg_posts 1 24h 10"
        )
        return

    try:
        period = parse_period(args[1])
        limit = _limit_from_parts(args, index=2, default=10)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    source = await _source_from_argument(message, source_repo, args[0])
    if source is None:
        return
    source_id = source.id

    if source.telegram_monitor_mode == "discussion":
        messages = await group_message_repo.list_by_period(
            source_id,
            period.start_iso,
            period.end_iso,
        )
        messages = sorted(messages, key=lambda item: item.date, reverse=True)[:limit]
        text = _render_tg_messages(
            source,
            messages,
            limit=limit,
            title=f"TG discussion messages за {period.label}",
        )
    else:
        posts = await post_repo.list_by_period(source_id, period.start_iso, period.end_iso)
        posts = sorted(posts, key=lambda item: item.date, reverse=True)[:limit]
        text = _render_tg_posts(source, posts, limit=limit, title=f"TG posts за {period.label}")
    await message.answer(text, disable_web_page_preview=True)


@router.message(Command("tg_keywords"))
async def tg_keywords_command(message: Message, keyword_repo: TelegramKeywordRepository) -> None:
    keywords = await keyword_repo.list_keywords()
    if not keywords:
        await message.answer("Ключевых слов пока нет. Добавь: /tg_add_keyword &lt;text&gt;")
        return
    lines = ["<b>Telegram keywords</b>"]
    lines.extend(f"#{keyword.id} {escape(keyword.keyword)}" for keyword in keywords)
    await message.answer("\n".join(lines))


@router.message(Command("tg_add_keyword"))
async def tg_add_keyword_command(
    message: Message,
    command: CommandObject,
    keyword_repo: TelegramKeywordRepository,
) -> None:
    keyword = (command.args or "").strip()
    if not keyword:
        await message.answer("Использование: /tg_add_keyword &lt;text&gt;")
        return
    try:
        saved = await keyword_repo.add_keyword(keyword)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(f"Ключевое слово добавлено: #{saved.id} {escape(saved.keyword)}")


@router.message(Command("tg_remove_keyword"))
async def tg_remove_keyword_command(
    message: Message,
    command: CommandObject,
    keyword_repo: TelegramKeywordRepository,
) -> None:
    keyword_id = _source_id_argument(command)
    if keyword_id is None:
        await message.answer("Использование: /tg_remove_keyword &lt;id&gt;")
        return
    removed = await keyword_repo.deactivate_keyword(keyword_id)
    await message.answer("Ключевое слово выключено." if removed else "Ключевое слово не найдено.")


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
        await message.answer("Использование: /add_source &lt;link_or_username&gt;")
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
            monitor_mode="posts",
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
        await message.answer("Использование: /remove_source &lt;source_id&gt;")
        return

    removed = await source_repo.deactivate(source_id)
    await message.answer(
        "Источник отключён." if removed else "Источник не найден или уже отключён."
    )


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

    source = await _get_active_source(
        message,
        command,
        source_repo,
        usage="/tg_sync_posts &lt;source_id&gt;",
    )
    if source is None:
        return

    try:
        if source.telegram_monitor_mode == "discussion":
            result = await collector.sync_discussion(source)
        else:
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
        await message.answer(
            "Использование: /tg_dashboard &lt;source_id_or_telegram_id&gt; &lt;period&gt;, "
            "например /tg_dashboard 1 7d"
        )
        return

    try:
        source = await _source_from_argument(message, dashboard_service.sources, args[0])
        if source is None:
            return
        dashboard = await dashboard_service.render(source.id, args[1])
        chart_png = await dashboard_service.render_chart_png(source.id, args[1])
    except ValueError as exc:
        await message.answer(str(exc))
        return

    await answer_dashboard(
        message,
        text=dashboard,
        chart_png=chart_png,
        filename=f"argus_tg_dashboard_{source.id}.png",
    )


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

    source = await _get_active_source(
        message,
        command,
        source_repo,
        usage="/join_source &lt;source_id&gt;",
    )
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

    source = await _get_active_source(
        message,
        command,
        source_repo,
        usage="/leave_source &lt;source_id&gt;",
    )
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

    source = await _get_active_source(
        message,
        command,
        source_repo,
        usage="/sync_comments &lt;source_id&gt;",
    )
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

    source = await _get_active_source(
        message,
        command,
        source_repo,
        usage="/sync_reactions &lt;source_id&gt;",
    )
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
        return _parse_int_token(argument)
    except ValueError:
        return None


def _parse_int_token(value: str) -> int:
    normalized = (
        value.strip()
        .replace("\u2212", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return int(normalized)


async def _source_from_argument(
    message: Message,
    source_repo: SourceRepository,
    value: str,
) -> Source | None:
    try:
        raw_id = _parse_int_token(value)
    except ValueError:
        await message.answer(
            "Источник должен быть числом: внутренний ID из /tg_sources "
            "или telegram_id вида -100...."
        )
        return None

    source = await source_repo.get_source(raw_id)
    if source is None and raw_id < 0:
        source = await source_repo.get_by_telegram_reference(raw_id)
    if source is None:
        source = await source_repo.get_by_telegram_reference(raw_id)
    if source is None or not source.is_active:
        await message.answer(
            "Источник не найден или отключён. Посмотри список: /tg_sources. "
            "Можно использовать #ID из списка или telegram_id вида -100...."
        )
        return None
    return source


def _limit_argument(command: CommandObject, *, default: int) -> int:
    return _limit_from_parts((command.args or "").split(), index=0, default=default)


def _limit_from_parts(parts: list[str], *, index: int, default: int) -> int:
    if len(parts) <= index:
        return default
    limit = _parse_int_token(parts[index])
    return min(max(limit, 1), POST_LIST_MAX_LIMIT)


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

    return await _source_from_argument(message, source_repo, str(source_id))


def _render_tg_posts(
    source: Source,
    posts: list[Post],
    *,
    limit: int,
    title: str,
) -> str:
    if not posts:
        return f"{escape(title)}: данных пока нет. Запусти /tg_sync_posts {source.id}."

    lines = [
        f"<b>{escape(title)}</b>",
        f"Источник: #{source.id} {escape(source.display_name)}",
        f"Показано: {len(posts)} из {limit}",
        "",
    ]
    for post in posts:
        views = post.views if post.views is not None else "?"
        url = f" ({escape(post.post_url)})" if post.post_url else ""
        text = _short(post.text or "(без текста)", 140)
        lines.append(
            "\n".join(
                [
                    f"- {escape(post.date[:16])} · msg {post.telegram_message_id} · "
                    f"views: {views}, reactions: {post.reactions_total}, comments: {post.comments_count}{url}",
                    f"  {escape(text)}",
                ]
            )
        )
    return _trim_message("\n".join(lines))


def _render_tg_messages(
    source: Source,
    messages: list[TelegramGroupMessage],
    *,
    limit: int,
    title: str,
) -> str:
    if not messages:
        return f"{escape(title)}: данных пока нет. Запусти /tg_sync_posts {source.id}."

    lines = [
        f"<b>{escape(title)}</b>",
        f"Источник: #{source.id} {escape(source.display_name)}",
        f"Показано: {len(messages)} из {limit}",
        "",
    ]
    for item in messages:
        url = f" ({escape(item.message_url)})" if item.message_url else ""
        text = _short(item.text or "(без текста)", 160)
        lines.append(
            "\n".join(
                [
                    f"- {escape(item.date[:16])} · msg {item.telegram_message_id} · "
                    f"from: {item.from_id or 'unknown'}{url}",
                    f"  {escape(text)}",
                ]
            )
        )
    return _trim_message("\n".join(lines))


def _short(value: str, length: int) -> str:
    normalized = value.replace("\n", " ").strip()
    if len(normalized) <= length:
        return normalized
    return f"{normalized[: length - 3]}..."


def _trim_message(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    trimmed = text[: limit - 20].rsplit("\n", maxsplit=1)[0].strip()
    return f"{trimmed}\n...обрезано."


async def _resolve_source_from_forward(message: Message, collector: TelegramCollector):
    origin = getattr(message, "forward_origin", None)
    chat = None
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)

    if chat is not None:
        username = getattr(chat, "username", None)
        if username:
            return await collector.resolve_source(username)
        chat_id = getattr(chat, "id", None)
        title = getattr(chat, "title", None) or getattr(chat, "full_name", None)
        if chat_id is not None:
            return await collector.resolve_source_by_chat_id(chat_id, fallback_title=title)

    fallback = (message.text or "").strip()
    if fallback.startswith("@") or "t.me/" in fallback or "telegram.me/" in fallback:
        return await collector.resolve_source(fallback)

    raise TelegramSourceError(
        "Не вижу публичный origin пересланного сообщения. "
        "Перешли сообщение из канала/группы без скрытого автора или используй /add_source &lt;link&gt;."
    )


def _format_error(exc: Exception) -> str:
    if isinstance(exc, LargeFloodWait):
        return f"Telegram ограничил запросы. Повторите после {exc.seconds} секунд."
    text = str(exc)
    if len(text) > 500:
        text = f"{text[:497]}..."
    return escape(text)
