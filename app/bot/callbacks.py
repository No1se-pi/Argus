import contextlib
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    alerts_keyboard,
    confirm_keyboard,
    dashboards_keyboard,
    main_menu_keyboard,
    modules_keyboard,
    settings_keyboard,
    setup_cancel_keyboard,
    status_keyboard,
    telegram_menu_keyboard,
    telegram_setup_keyboard,
    vk_menu_keyboard,
    vk_setup_keyboard,
)
from app.bot.screens import (
    main_menu_text,
    modules_text,
    setup_text,
    status_text,
    telegram_auth_cli_text,
    unavailable_text,
)
from app.bot.states import TelegramAuthStates, VKSetupStates
from app.config import Settings
from app.modules import ModuleRegistry, ModuleStatus
from app.storage.repositories import RuntimeSettingsRepository, SourceRepository
from app.telegram_auth import TelegramAuthService
from app.vk.service import VKService

logger = logging.getLogger(__name__)
router = Router(name="callbacks")


@router.callback_query(F.data == "menu:main")
async def main_menu_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await _edit(query, await main_menu_text(module_registry), main_menu_keyboard())


@router.callback_query(F.data == "menu:status")
async def status_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await _edit(query, await status_text(module_registry), status_keyboard())


@router.callback_query(F.data == "menu:modules")
async def modules_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await _edit(query, await modules_text(module_registry), modules_keyboard())


@router.callback_query(F.data == "menu:dash")
async def dashboards_callback(query: CallbackQuery) -> None:
    await _edit(query, "<b>Дашборды</b>\n\nВыбери период:", dashboards_keyboard())


@router.callback_query(F.data == "menu:settings")
async def settings_callback(query: CallbackQuery) -> None:
    await _edit(query, "<b>Настройка</b>\n\nВыбери действие:", settings_keyboard())


@router.callback_query(F.data == "menu:alerts")
async def alerts_callback(
    query: CallbackQuery,
    runtime_settings_repo: RuntimeSettingsRepository,
    settings: Settings,
) -> None:
    await _edit(query, await _alerts_text(runtime_settings_repo, settings), alerts_keyboard())


@router.callback_query(F.data == "vk:menu")
async def vk_menu_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    info = await module_registry.vk_info(check_network=False)
    if info.status != ModuleStatus.OK:
        await _edit(query, unavailable_text(info), vk_menu_keyboard(False))
        return
    await _edit(query, "<b>VK Monitor</b>\n\nВыбери действие:", vk_menu_keyboard(True))


@router.callback_query(F.data == "tg:menu")
async def tg_menu_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    info = await module_registry.telegram_info()
    if info.status != ModuleStatus.OK:
        await _edit(query, unavailable_text(info), telegram_menu_keyboard(False))
        return
    await _edit(query, "<b>Telegram Monitor</b>\n\nВыбери действие:", telegram_menu_keyboard(True))


@router.callback_query(F.data == "vk:status")
async def vk_status_callback(query: CallbackQuery, vk_service: VKService) -> None:
    await _edit(query, await vk_service.render_status(), vk_menu_keyboard(True))


@router.callback_query(F.data == "vk:sync")
async def vk_sync_callback(query: CallbackQuery, vk_service: VKService) -> None:
    await query.answer("Синхронизация VK запущена")
    try:
        result = await vk_service.sync_now()
    except Exception as exc:
        logger.exception("VK sync failed")
        await _edit(query, f"VK sync failed: {_safe_error(exc)}", vk_menu_keyboard(False))
        return

    await _edit(
        query,
        "\n".join(
            [
                "<b>VK sync complete</b>",
                f"Постов обработано: {result.posts_processed}",
                f"Комментариев обработано: {result.comments_processed}",
                f"Новых постов: {len(result.new_posts)}",
                f"Новых комментариев: {len(result.new_comments)}",
            ]
        ),
        vk_menu_keyboard(True),
    )


@router.callback_query(F.data == "vk:posts")
async def vk_posts_callback(query: CallbackQuery, vk_service: VKService) -> None:
    try:
        text = await vk_service.render_recent_posts()
    except Exception as exc:
        text = f"VK posts unavailable: {_safe_error(exc)}"
    await _edit(query, text, vk_menu_keyboard(True))


@router.callback_query(F.data == "vk:comments")
async def vk_comments_callback(query: CallbackQuery, vk_service: VKService) -> None:
    try:
        text = await vk_service.render_recent_comments()
    except Exception as exc:
        text = f"VK comments unavailable: {_safe_error(exc)}"
    await _edit(query, text, vk_menu_keyboard(True))


@router.callback_query(F.data.startswith("vk:dash:"))
async def vk_dashboard_callback(query: CallbackQuery, vk_service: VKService) -> None:
    period = (query.data or "").split(":")[-1]
    try:
        text = await vk_service.render_dashboard(period)
    except Exception as exc:
        text = f"VK dashboard unavailable: {_safe_error(exc)}"
    await _edit(query, text, vk_menu_keyboard(True))


@router.callback_query(F.data == "tg:status")
async def tg_status_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    info = await module_registry.telegram_info()
    text = unavailable_text(info) if not info.is_available else "Telegram Monitor: ok"
    await _edit(query, text, telegram_menu_keyboard(info.is_available))


@router.callback_query(F.data == "tg:sources")
async def tg_sources_callback(
    query: CallbackQuery,
    module_registry: ModuleRegistry,
    source_repo: SourceRepository,
) -> None:
    info = await module_registry.telegram_info()
    if not info.is_available:
        await _edit(query, unavailable_text(info), telegram_menu_keyboard(False))
        return
    sources = await source_repo.list_sources()
    if not sources:
        await _edit(
            query,
            "Telegram sources: no data. Use /add_source <username>.",
            telegram_menu_keyboard(True),
        )
        return
    lines = ["<b>Telegram sources</b>"]
    lines.extend(f"#{source.id} {source.display_name}" for source in sources)
    await _edit(query, "\n".join(lines), telegram_menu_keyboard(True))


@router.callback_query(F.data.startswith("tg:dash:"))
async def tg_dashboard_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    info = await module_registry.telegram_info()
    if not info.is_available:
        await _edit(query, unavailable_text(info), telegram_menu_keyboard(False))
        return
    period = (query.data or "").split(":")[-1]
    await _edit(
        query,
        f"Telegram dashboard за {period}: "
        f"выбери source_id командой /tg_dashboard <source_id> {period}.",
        telegram_menu_keyboard(True),
    )


@router.callback_query(F.data == "vk:watch_on")
async def vk_watch_on_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_vk_monitor", True)
    await _edit(query, "VK Monitor включён.", settings_keyboard())


@router.callback_query(F.data == "tg:watch_on")
async def tg_watch_on_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_telegram_monitor", True)
    await _edit(query, "Telegram Monitor включён. Проверь /status.", settings_keyboard())


@router.callback_query(F.data == "confirm:disable_vk")
async def confirm_disable_vk(query: CallbackQuery) -> None:
    await _edit(
        query,
        "Вы уверены, что хотите выключить VK Monitor?",
        confirm_keyboard("disable_vk"),
    )


@router.callback_query(F.data == "confirm:disable_tg")
async def confirm_disable_tg(query: CallbackQuery) -> None:
    await _edit(
        query,
        "Вы уверены, что хотите выключить Telegram Monitor?",
        confirm_keyboard("disable_tg"),
    )


@router.callback_query(F.data == "do:disable_vk")
async def disable_vk_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_vk_monitor", False)
    await _edit(query, "VK Monitor выключен.", main_menu_keyboard())


@router.callback_query(F.data == "do:disable_tg")
async def disable_tg_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_telegram_monitor", False)
    await _edit(query, "Telegram Monitor выключен.", main_menu_keyboard())


@router.callback_query(F.data == "setup:start")
async def setup_start_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await _edit(query, await setup_text(module_registry), settings_keyboard())


@router.callback_query(F.data == "setup:telegram")
async def telegram_setup_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    info = await module_registry.telegram_info()
    lines = [
        "<b>Telegram Monitor setup</b>",
        "",
        f"Status: {info.status.value}",
        f"Reason: {info.reason or 'ok'}",
        "",
        "Если TG_API_ID/TG_API_HASH пока нет, Bot UI всё равно работает.",
        "Telethon session создаётся локально, не через бота.",
    ]
    await _edit(query, "\n".join(lines), telegram_setup_keyboard(can_auth=True))


@router.callback_query(F.data == "setup:vk")
async def vk_setup_callback(query: CallbackQuery, state: FSMContext, vk_service: VKService) -> None:
    await state.clear()
    await _edit(query, await vk_service.config_summary(), vk_setup_keyboard())


@router.callback_query(F.data == "setup:vk_gt")
async def vk_setup_group_token_callback(
    query: CallbackQuery,
    state: FSMContext,
    vk_service: VKService,
) -> None:
    await state.clear()
    await state.set_state(VKSetupStates.waiting_group_token)
    await _edit(
        query,
        await vk_service.config_summary() + "\n\nОтправь VK_GROUP_TOKEN следующим сообщением.",
        setup_cancel_keyboard(),
    )


@router.callback_query(F.data == "setup:vk_ut")
async def vk_setup_user_token_callback(
    query: CallbackQuery,
    state: FSMContext,
    vk_service: VKService,
) -> None:
    await state.clear()
    await state.set_state(VKSetupStates.waiting_user_token)
    await _edit(
        query,
        await vk_service.config_summary()
        + "\n\nОтправь VK_USER_ACCESS_TOKEN следующим сообщением. Он нужен для /vk_sync истории.",
        setup_cancel_keyboard(),
    )


@router.callback_query(F.data == "setup:vk_gid")
async def vk_setup_group_id_callback(
    query: CallbackQuery,
    state: FSMContext,
    vk_service: VKService,
) -> None:
    await state.clear()
    await state.set_state(VKSetupStates.waiting_group_id)
    await _edit(
        query,
        await vk_service.config_summary() + "\n\nОтправь VK_GROUP_ID следующим сообщением.",
        setup_cancel_keyboard(),
    )


@router.callback_query(F.data == "setup:cancel")
async def setup_cancel_callback(
    query: CallbackQuery,
    state: FSMContext,
    telegram_auth_service: TelegramAuthService,
) -> None:
    await telegram_auth_service.cancel(query.from_user.id)
    await state.clear()
    await _edit(query, "Настройка отменена.", main_menu_keyboard())


@router.message(VKSetupStates.waiting_group_token)
async def vk_setup_group_token_message(
    message: Message,
    state: FSMContext,
    vk_service: VKService,
) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("Токен пустой. Отправь VK_GROUP_TOKEN или нажми Отмена.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    await vk_service.save_setup(group_token=token, user_access_token=None, group_id=None)
    await state.clear()
    await message.answer(
        "VK_GROUP_TOKEN сохранён и не будет показан обратно.",
        reply_markup=vk_setup_keyboard(),
    )


@router.message(VKSetupStates.waiting_user_token)
async def vk_setup_user_token_message(
    message: Message,
    state: FSMContext,
    vk_service: VKService,
) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("Токен пустой. Отправь VK_USER_ACCESS_TOKEN или нажми Отмена.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    await vk_service.save_setup(group_token=None, user_access_token=token, group_id=None)
    await state.clear()
    await message.answer(
        "VK_USER_ACCESS_TOKEN сохранён и не будет показан обратно.",
        reply_markup=vk_setup_keyboard(),
    )


@router.message(VKSetupStates.waiting_group_id)
async def vk_setup_group_message(
    message: Message,
    state: FSMContext,
    vk_service: VKService,
) -> None:
    raw_group_id = (message.text or "").strip()
    try:
        group_id = abs(int(raw_group_id))
    except ValueError:
        await message.answer("VK_GROUP_ID должен быть числом. Например: 123456789.")
        return

    await vk_service.save_setup(group_token=None, user_access_token=None, group_id=group_id)
    await state.clear()
    try:
        source = await vk_service.healthcheck()
        text = f"VK_GROUP_ID сохранён. Healthcheck ok: {source.display_name}"
    except Exception as exc:
        text = f"VK_GROUP_ID сохранён, но healthcheck failed: {_safe_error(exc)}"
    await message.answer(text, reply_markup=vk_setup_keyboard())


@router.callback_query(F.data == "tg:auth")
async def tg_auth_callback(
    query: CallbackQuery,
    state: FSMContext,
    telegram_auth_service: TelegramAuthService,
) -> None:
    await state.clear()
    await _edit(
        query,
        telegram_auth_cli_text(telegram_auth_service.is_configured()),
        telegram_setup_keyboard(can_auth=telegram_auth_service.is_configured()),
    )


@router.message(TelegramAuthStates.waiting_phone)
@router.message(TelegramAuthStates.waiting_code)
@router.message(TelegramAuthStates.waiting_password)
async def tg_auth_legacy_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        telegram_auth_cli_text(configured=True),
        reply_markup=telegram_setup_keyboard(can_auth=True),
    )


@router.callback_query(F.data.startswith("alerts:"))
async def alerts_toggle_callback(
    query: CallbackQuery,
    runtime_settings_repo: RuntimeSettingsRepository,
    settings: Settings,
) -> None:
    mapping = {
        "alerts:vk_on": ("alerts_vk_enabled", True),
        "alerts:vk_off": ("alerts_vk_enabled", False),
        "alerts:posts_on": ("alerts_vk_posts_enabled", True),
        "alerts:posts_off": ("alerts_vk_posts_enabled", False),
        "alerts:comments_on": ("alerts_vk_comments_enabled", True),
        "alerts:comments_off": ("alerts_vk_comments_enabled", False),
    }
    key, enabled = mapping.get(query.data, ("", True))
    if not key:
        await query.answer("Неизвестная настройка алертов.", show_alert=True)
        return
    await runtime_settings_repo.set(key, "true" if enabled else "false", is_secret=False)
    await _edit(query, await _alerts_text(runtime_settings_repo, settings), alerts_keyboard())


async def _edit(query: CallbackQuery, text: str, reply_markup=None) -> None:
    await query.answer()
    if query.message is None:
        return
    try:
        await query.message.edit_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        await query.message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    if len(text) > 300:
        return f"{text[:297]}..."
    return text


async def _alerts_text(runtime_settings_repo: RuntimeSettingsRepository, settings: Settings) -> str:
    vk_enabled = await runtime_settings_repo.get_bool(
        "alerts_vk_enabled",
        settings.alerts_vk_enabled,
    )
    posts_enabled = await runtime_settings_repo.get_bool(
        "alerts_vk_posts_enabled",
        settings.alerts_vk_posts_enabled,
    )
    comments_enabled = await runtime_settings_repo.get_bool(
        "alerts_vk_comments_enabled",
        settings.alerts_vk_comments_enabled,
    )
    return "\n".join(
        [
            "<b>Алерты</b>",
            "",
            f"VK alerts: {_flag(vk_enabled)}",
            f"Новые посты: {_flag(posts_enabled)}",
            f"Новые комментарии: {_flag(comments_enabled)}",
            "",
            "Выбери, что включить или выключить:",
        ]
    )


def _flag(value: bool) -> str:
    return "on" if value else "off"
