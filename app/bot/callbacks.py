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
    vk_menu_keyboard,
)
from app.bot.screens import main_menu_text, modules_text, setup_text, status_text, unavailable_text
from app.bot.states import VKSetupStates
from app.modules import ModuleRegistry, ModuleStatus
from app.storage.repositories import SourceRepository
from app.vk.client import VKAPIError
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
async def alerts_callback(query: CallbackQuery) -> None:
    await _edit(query, "<b>Алерты</b>\n\nВыбери, какие алерты включить или выключить:", alerts_keyboard())


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
    await _edit(query, unavailable_text(info) if not info.is_available else "Telegram Monitor: ok", telegram_menu_keyboard(info.is_available))


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
        await _edit(query, "Telegram sources: no data. Use /add_source <username>.", telegram_menu_keyboard(True))
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
        f"Telegram dashboard за {period}: выбери source_id командой /tg_dashboard <source_id> {period}.",
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
    await _edit(query, "Вы уверены, что хотите выключить VK Monitor?", confirm_keyboard("disable_vk"))


@router.callback_query(F.data == "confirm:disable_tg")
async def confirm_disable_tg(query: CallbackQuery) -> None:
    await _edit(query, "Вы уверены, что хотите выключить Telegram Monitor?", confirm_keyboard("disable_tg"))


@router.callback_query(F.data == "do:disable_vk")
async def disable_vk_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_vk_monitor", False)
    await _edit(query, "VK Monitor выключен.", main_menu_keyboard())


@router.callback_query(F.data == "do:disable_tg")
async def disable_tg_callback(query: CallbackQuery, module_registry: ModuleRegistry) -> None:
    await module_registry.set_module_enabled("enable_telegram_monitor", False)
    await _edit(query, "Telegram Monitor выключен.", main_menu_keyboard())


@router.callback_query(F.data.startswith("alerts:"))
async def alerts_toggle_callback(query: CallbackQuery) -> None:
    await query.answer("Настройка алертов сохранена для будущего расширения.", show_alert=True)


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
    await _edit(query, "\n".join(lines), telegram_menu_keyboard(False))


@router.callback_query(F.data == "setup:vk")
async def vk_setup_callback(query: CallbackQuery, state: FSMContext, vk_service: VKService) -> None:
    config = await vk_service.effective_config()
    await state.clear()
    await state.update_data(access_token=None)
    if not config.group_token:
        await state.set_state(VKSetupStates.waiting_token)
        await _edit(
            query,
            await vk_service.config_summary()
            + "\n\nОтправь VK_GROUP_TOKEN следующим сообщением.",
            setup_cancel_keyboard(can_skip=False),
        )
        return

    await state.set_state(VKSetupStates.waiting_group_id)
    await _edit(
        query,
        await vk_service.config_summary() + "\n\nОтправь VK_GROUP_ID следующим сообщением.",
        setup_cancel_keyboard(can_skip=config.group_id is not None),
    )


@router.callback_query(F.data == "setup:skip")
async def setup_skip_callback(query: CallbackQuery, state: FSMContext, vk_service: VKService) -> None:
    current_state = await state.get_state()
    if current_state == VKSetupStates.waiting_group_id.state:
        await _finish_vk_setup(query, state, vk_service, group_id=None)
        return
    await query.answer("Сейчас нечего пропускать.", show_alert=True)


@router.callback_query(F.data == "setup:cancel")
async def setup_cancel_callback(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit(query, "Настройка отменена.", main_menu_keyboard())


@router.message(VKSetupStates.waiting_token)
async def vk_setup_token_message(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("Токен пустой. Отправь VK_GROUP_TOKEN или нажми Отмена.")
        return
    await state.update_data(access_token=token)
    await state.set_state(VKSetupStates.waiting_group_id)
    with contextlib.suppress(Exception):
        await message.delete()
    await message.answer(
        "Токен получен и не будет показан обратно.\nТеперь отправь VK_GROUP_ID.",
        reply_markup=setup_cancel_keyboard(can_skip=False),
    )


@router.message(VKSetupStates.waiting_group_id)
async def vk_setup_group_message(message: Message, state: FSMContext, vk_service: VKService) -> None:
    raw_group_id = (message.text or "").strip()
    try:
        group_id = abs(int(raw_group_id))
    except ValueError:
        await message.answer("VK_GROUP_ID должен быть числом. Например: 123456789.")
        return

    await _finish_vk_setup(message, state, vk_service, group_id=group_id)


async def _finish_vk_setup(
    event: CallbackQuery | Message,
    state: FSMContext,
    vk_service: VKService,
    *,
    group_id: int | None,
) -> None:
    data = await state.get_data()
    await vk_service.save_setup(access_token=data.get("access_token"), group_id=group_id)
    await state.clear()

    try:
        source = await vk_service.healthcheck()
        text = "\n".join(
            [
                "<b>VK setup complete</b>",
                f"Группа: {source.display_name}",
                "Healthcheck: ok",
            ]
        )
    except VKAPIError as exc:
        text = f"VK settings saved, but healthcheck failed: {_safe_error(exc)}"
    except Exception as exc:
        logger.exception("VK setup healthcheck failed")
        text = f"VK settings saved, but healthcheck failed: {_safe_error(exc)}"

    if isinstance(event, CallbackQuery):
        await _edit(event, text, vk_menu_keyboard(True))
    else:
        await event.answer(text, reply_markup=vk_menu_keyboard(True))


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
