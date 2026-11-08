"""Button-based workflows for VK reports, saved templates, and explicit identities."""

import asyncio
import io
import json
import re
from html import escape
from urllib.parse import urlparse
from zipfile import BadZipFile

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.states import VKLikesStates
from app.bot.vk_likes import (
    bind_profile_command,
    collect_and_send,
    send_presentation,
)
from app.bot.vk_likes_keyboards import (
    back_keyboard,
    bindings_keyboard,
    likes_keyboard,
    period_keyboard,
    template_keyboard,
)
from app.vk.client import VKAPIError, VKClient, vk_error_text
from app.vk.likes import likes_access_token, likes_web_token, parse_selection
from app.vk.presentation import name_tokens
from app.vk.workspace import VKLikesWorkspace

router = Router(name="vk_likes_ui")


@router.callback_query(F.data == "vklui:connect")
async def connect_vk(query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace):
    await query.answer()
    await state.clear()
    if not isinstance(query.message, Message) or query.message.chat.type != "private":
        if isinstance(query.message, Message):
            await query.message.answer("Для подключения ВК откройте личный чат с ботом.")
        return
    helper = vk_likes_workspace.auth_helper
    if helper is None:
        await show(
            query,
            "Помощник входа ещё не размещён. Администратору нужно настроить "
            "HTTPS-адрес VK_AUTH_PUBLIC_URL на сервере.",
            likes_keyboard(),
        )
        return
    config = await vk_likes_workspace.service.vk_service.effective_config()
    if not config.group_id:
        await show(query, "Сначала укажите сообщество в «Настроить ВК».", likes_keyboard())
        return
    try:
        url = await helper.issue(query.message.chat.id, config.group_id)
    except ValueError as exc:
        await show(query, str(exc), likes_keyboard())
        return
    await show(
        query,
        "<b>🔐 Подключение ВК с телефона</b>\n\n"
        "Откройте временную ссылку ниже. Это окно браузера на вашем сервере: "
        "войдите в ВК и подтвердите вход, если ВК попросит.\n\n"
        "Рабочий доступ к лайкам будет проверен и сохранён автоматически. "
        "Ссылки с токеном, пароль и коды присылать в чат не нужно.\n\n"
        "Ссылка действует 10 минут, только для одного браузера. Не пересылайте её. "
        "Новая ссылка отменяет предыдущую.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть вход в ВК", url=url)],
                [InlineKeyboardButton(text="🔑 Проверить доступ", callback_data="vklui:auth")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="vkl:menu")],
            ]
        ),
    )


@router.callback_query(F.data == "vklui:auth")
async def check_auth(query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace):
    await query.answer("Проверяю доступ к ВК…")
    await state.clear()
    service = vk_likes_workspace.service.vk_service
    config = await service.effective_config()
    token = likes_access_token(service.settings, config)
    source = (
        "настройки бота (веб-сеанс ВК, SQLite)"
        if getattr(config, "web_access_token", None)
        else ".env / переменная окружения VK_WEB_ACCESS_TOKEN (веб-запросы ВК)"
        if getattr(service.settings, "vk_web_access_token", None)
        else ".env / переменная окружения VK_SERVICE_ACCESS_TOKEN"
        if getattr(service.settings, "vk_service_access_token", None)
        else ".env / переменная окружения VK_USER_ACCESS_TOKEN"
        if getattr(service.settings, "vk_user_access_token", None)
        else "настройки бота (SQLite)"
        if config.user_access_token
        else "не задан"
    )
    text = f"<b>🔑 Доступ к ВК</b>\n\nИсточник токена: {escape(source)}\n\n"
    if not token:
        text += "Токен не задан. Укажите VK_SERVICE_ACCESS_TOKEN или откройте «Настроить ВК»."
    elif not config.group_id:
        text += "Сначала укажите сообщество в «Настроить ВК»."
    else:
        try:
            client = VKClient(
                access_token=token,
                api_version=service.settings.vk_api_version,
                web_mode=bool(likes_web_token(service.settings, config)),
            )
            wall = await client.request("wall.get", {"owner_id": -abs(config.group_id), "count": 1})
            text += "✅ Чтение постов: wall.get доступен.\n"
            posts = wall.get("items", []) if isinstance(wall, dict) else []
            if posts:
                await asyncio.sleep(0.35)
                await client.request(
                    "likes.getList",
                    {
                        "type": "post",
                        "owner_id": -abs(config.group_id),
                        "item_id": posts[0]["id"],
                        "count": 1,
                    },
                )
                text += "✅ Список лайков: likes.getList доступен.\n"
                await asyncio.sleep(0.35)
                await client.request("users.get", {"user_ids": "1"})
                text += "✅ Имена профилей: users.get доступен. Можно запускать сбор."
            else:
                text += "Нет постов для проверки likes.getList; доступ к списку лайков не проверен."
        except VKAPIError as exc:
            text += vk_error_text(exc)
        except (aiohttp.ClientError, TimeoutError):
            text += "Не удалось связаться с ВК. Повторите проверку позже."
    await show(query, text, likes_keyboard())


async def show(query: CallbackQuery, text: str, keyboard) -> None:
    if isinstance(query.message, Message):
        # Edit text panels; document messages need a separate text panel.
        if query.message.text:
            try:
                await query.message.edit_text(
                    text, reply_markup=keyboard, disable_web_page_preview=True
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in exc.message.lower():
                    raise
        else:
            await query.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data == "vkl:menu")
async def menu(query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace):
    await query.answer()
    await state.clear()
    saved = await vk_likes_workspace.service.latest()
    template = vk_likes_workspace.template_path.is_file()
    text = "<b>❤️ Лайки и презентации ВК</b>\n\n"
    text += f"Шаблон PPTX: {'✅ загружен' if template else 'нужно загрузить'}\n"
    text += f"Привязок профилей: {len(await vk_likes_workspace.bindings())}\n"
    if saved:
        text += f"Последний отчёт: {escape(saved['label'])} · {saved['posts_count']} постов\n"
    else:
        text += "Отчётов пока нет\n"
    text += "\nСоберите лайки за период или создайте презентацию по сохранённому шаблону."
    await show(query, text, likes_keyboard())


@router.callback_query(F.data.startswith("vklui:period:"))
async def choose_period(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    action = query.data.rsplit(":", 1)[1]
    if action not in {"table", "pptx"}:
        return
    label = "таблицы лайков" if action == "table" else "презентации"
    await show(
        query,
        f"<b>Период для {label}</b>\n\n7 и 30 суток — до текущего момента. "
        "Для календарного периода нажмите «Указать даты».",
        period_keyboard(action),
    )


async def run_action(message: Message, action: str, period: str, workspace: VKLikesWorkspace):
    if action == "table":
        await collect_and_send(message, period, workspace.service)
        return
    template = await workspace.template()
    if template is None:
        await message.answer(
            "Сначала загрузите шаблон презентации.", reply_markup=template_keyboard()
        )
        return
    try:
        selection = parse_selection(period, workspace.service.vk_service.settings.timezone)
        await message.answer(
            "⏳ Собираю лайки и готовлю презентацию. Это может занять несколько минут."
        )
        report = await workspace.service.collect(selection)
        await send_presentation(
            message, template, report, await workspace.bindings(), workspace.service
        )
    except VKAPIError as exc:
        await message.answer(vk_error_text(exc), reply_markup=likes_keyboard())
    except (ValueError, BadZipFile):
        await message.answer(
            "Не удалось создать презентацию. Проверьте период, настройки ВК и шаблон PPTX.",
            reply_markup=likes_keyboard(),
        )
    except (aiohttp.ClientError, TimeoutError):
        await message.answer(
            "Ошибка соединения. Попробуйте ещё раз; сохранённые данные доступны.",
            reply_markup=likes_keyboard(),
        )


@router.callback_query(F.data.startswith("vklui:run:"))
async def run_period(query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace):
    await query.answer()
    await state.clear()
    _prefix, _run, action, period = query.data.split(":", maxsplit=3)
    if action in {"table", "pptx"} and period in {"7d", "30d", "last 17"}:
        if isinstance(query.message, Message):
            await run_action(query.message, action, period, vk_likes_workspace)


@router.callback_query(F.data.startswith("vklui:custom:"))
async def custom_period(query: CallbackQuery, state: FSMContext):
    await query.answer()
    action = query.data.rsplit(":", 1)[1]
    if action not in {"table", "pptx"}:
        return
    await state.set_state(VKLikesStates.waiting_period)
    await state.update_data(action=action)
    await show(
        query,
        "<b>📅 Ваш период</b>\n\nОтправьте даты начала и конца:\n"
        "<code>12.10.2026 19.10.2026</code>\n\nОба дня включаются, время — Москва.",
        back_keyboard(),
    )


def calendar_period(text: str) -> str:
    from datetime import datetime

    values = text.split()
    if len(values) != 2:
        raise ValueError
    dates = []
    for value in values:
        parsed = datetime.strptime(value, "%d.%m.%Y" if "." in value else "%Y-%m-%d")
        dates.append(parsed.strftime("%Y-%m-%d"))
    return " ".join(dates)


@router.message(VKLikesStates.waiting_period, F.text, ~F.text.startswith("/"))
async def receive_period(message: Message, state: FSMContext, vk_likes_workspace: VKLikesWorkspace):
    try:
        period = calendar_period(message.text)
        parse_selection(period, vk_likes_workspace.service.vk_service.settings.timezone)
    except (ValueError, OverflowError):
        await message.answer(
            "Проверьте даты. Пример: 12.10.2026 19.10.2026. "
            "Дата окончания не должна быть раньше начала.",
            reply_markup=back_keyboard(),
        )
        return
    action = (await state.get_data()).get("action", "table")
    await state.clear()
    await run_action(message, action, period, vk_likes_workspace)


@router.callback_query(F.data == "vklui:saved")
async def saved_presentation(
    query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace
):
    await query.answer()
    await state.clear()
    if not isinstance(query.message, Message):
        return
    template = await vk_likes_workspace.template()
    if template is None:
        await query.message.answer(
            "Сначала загрузите шаблон PPTX.", reply_markup=template_keyboard()
        )
        return
    report = await vk_likes_workspace.service.latest()
    if report is None:
        await query.message.answer(
            "Сначала соберите лайки за нужный период.", reply_markup=likes_keyboard()
        )
        return
    await query.message.answer("Готовлю PPTX из сохранённого отчёта. Нового запроса к ВК не будет.")
    await send_presentation(
        query.message,
        template,
        report,
        await vk_likes_workspace.bindings(),
        vk_likes_workspace.service,
    )


@router.callback_query(F.data == "vklui:template")
async def template_panel(
    query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace
):
    await query.answer()
    await state.clear()
    filename = await vk_likes_workspace.settings.get("vk_likes_template_name")
    exists = vk_likes_workspace.template_path.is_file()
    description = escape(filename or "Шаблон PPTX") if exists else "Ещё не загружен"
    await show(
        query,
        f"<b>📎 Шаблон презентации</b>\n\n{description}\n\n"
        "Загрузите один раз — бот будет использовать его для следующих презентаций. "
        "ФИО и оформление сохраняются. Нужны таблицы с колонками «ФИО» и «Лайки».",
        template_keyboard(),
    )


@router.callback_query(F.data == "vklui:upload")
async def request_template(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(VKLikesStates.waiting_template)
    await show(
        query,
        "<b>📤 Загрузка шаблона</b>\n\nОтправьте файл .pptx до 20 МБ. "
        "Он заменит сохранённый шаблон после проверки.",
        back_keyboard(),
    )


@router.message(
    VKLikesStates.waiting_template,
    (F.document & (~F.caption | ~F.caption.startswith("/"))) | (F.text & ~F.text.startswith("/")),
)
async def receive_template(
    message: Message, state: FSMContext, vk_likes_workspace: VKLikesWorkspace
):
    document = message.document
    if not document or not (document.file_name or "").lower().endswith(".pptx"):
        await message.answer("Отправьте файл презентации .pptx.", reply_markup=back_keyboard())
        return
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await message.answer(
            "Файл слишком большой. Допустимо до 20 МБ.", reply_markup=back_keyboard()
        )
        return
    try:
        data = io.BytesIO()
        await message.bot.download(document, destination=data)
        await vk_likes_workspace.save_template(data.getvalue(), document.file_name)
    except (ValueError, BadZipFile):
        await message.answer(
            "В этом файле не найдены корректные таблицы «ФИО / Лайки». "
            "Предыдущий шаблон сохранён. Пришлите другой PPTX.",
            reply_markup=back_keyboard(),
        )
        return
    except (aiohttp.ClientError, TimeoutError):
        await message.answer(
            "Не удалось загрузить файл. Повторите отправку.", reply_markup=back_keyboard()
        )
        return
    await state.clear()
    await message.answer(
        "✅ Шаблон сохранён. Теперь можно создать презентацию кнопкой.",
        reply_markup=likes_keyboard(),
    )


@router.callback_query(F.data == "vklui:download")
async def download_template(query: CallbackQuery, vk_likes_workspace: VKLikesWorkspace):
    await query.answer()
    data = await vk_likes_workspace.template()
    if isinstance(query.message, Message):
        if data is None:
            await query.message.answer(
                "Сначала загрузите шаблон.", reply_markup=template_keyboard()
            )
        else:
            await query.message.answer_document(
                BufferedInputFile(data, filename="vk_likes_template.pptx")
            )


@router.callback_query(F.data == "vklui:bindings")
async def bindings_panel(
    query: CallbackQuery, state: FSMContext, vk_likes_workspace: VKLikesWorkspace
):
    await query.answer()
    await state.clear()
    items = await vk_likes_workspace.bindings()
    text = "<b>👥 Привязки профилей</b>\n\n"
    shown = 0
    for name, user_id in list(items.items())[:15]:
        line = f'• {escape(name[:120])} → <a href="https://vk.ru/id{user_id}">{user_id}</a>\n'
        if len(text) + len(line) > 3300:
            break
        text += line
        shown += 1
    if not items:
        text += "Пока нет привязок.\n"
    if len(items) > shown:
        text += f"\nВсего {len(items)}. Полный список — кнопкой «Скачать привязки».\n"
    text += "\nПри заполнении PPTX эти соответствия используются в первую очередь."
    await show(query, text, bindings_keyboard())


@router.callback_query(F.data == "vklui:bind")
async def request_binding(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(VKLikesStates.waiting_binding)
    await show(
        query,
        "<b>➕ Привязка профиля</b>\n\nОтправьте ФИО как в шаблоне и профиль через |:\n"
        "<code>Цой Владислав Витальевич | https://vk.ru/ghostsoy</code>\n\n"
        "Можно указать числовой VK ID. Для удаления: <code>ФИО | 0</code>.",
        back_keyboard(),
    )


async def profile_id(reference: str, workspace: VKLikesWorkspace) -> int:
    if reference.isdecimal():
        return int(reference)
    parsed = urlparse(reference if "://" in reference else f"https://{reference}")
    if parsed.hostname not in {"vk.ru", "vk.com", "www.vk.ru", "www.vk.com", "m.vk.ru", "m.vk.com"}:
        raise ValueError("Нужна ссылка на профиль ВК или числовой ID.")
    name = parsed.path.strip("/")
    if re.fullmatch(r"id[1-9]\d*", name):
        return int(name[2:])
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.]*", name):
        raise ValueError("Не удалось прочитать ссылку на профиль ВК.")
    config = await workspace.service.vk_service.effective_config()
    token = likes_access_token(workspace.service.vk_service.settings, config)
    if not token:
        raise ValueError(
            "Для короткой ссылки нужен действующий пользовательский токен ВК. "
            "Можно указать числовой ID."
        )
    client = VKClient(
        access_token=token,
        api_version=workspace.service.vk_service.settings.vk_api_version,
        web_mode=bool(likes_web_token(workspace.service.vk_service.settings, config)),
    )
    users = await client.request("users.get", {"user_ids": name})
    if (
        not isinstance(users, list)
        or len(users) != 1
        or not isinstance(users[0], dict)
        or type(users[0].get("id")) is not int
        or users[0]["id"] <= 0
    ):
        raise ValueError("Профиль не найден. Проверьте ссылку.")
    return users[0]["id"]


@router.message(VKLikesStates.waiting_binding, F.text, ~F.text.startswith("/"))
async def receive_binding(
    message: Message, state: FSMContext, vk_likes_workspace: VKLikesWorkspace
):
    try:
        name, reference = [part.strip() for part in message.text.split("|", maxsplit=1)]
        if len(name_tokens(name)) < 2 or len(name) > 200:
            raise ValueError("Укажите ФИО как в шаблоне.")
        user_id = await profile_id(reference, vk_likes_workspace)
    except ValueError as exc:
        await message.answer(
            escape(str(exc)) if str(exc) else "Формат: ФИО | ссылка на профиль.",
            reply_markup=back_keyboard(),
        )
        return
    except (VKAPIError, aiohttp.ClientError, TimeoutError):
        await message.answer(
            "Не удалось проверить ссылку через ВК. Попробуйте позже или укажите числовой ID.",
            reply_markup=back_keyboard(),
        )
        return
    await bind_profile_command(
        message,
        CommandObject(command="vk_likes_bind", args=f"{user_id} | {name}"),
        vk_likes_workspace.service,
    )
    await state.clear()
    await message.answer("Можно продолжить работу с отчётами.", reply_markup=likes_keyboard())


@router.callback_query(F.data == "vklui:bindings_export")
async def export_bindings(query: CallbackQuery, vk_likes_workspace: VKLikesWorkspace):
    await query.answer()
    if isinstance(query.message, Message):
        data = json.dumps(await vk_likes_workspace.bindings(), ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        await query.message.answer_document(
            BufferedInputFile(data, filename="vk_profile_bindings.json")
        )


@router.callback_query(F.data == "vklui:help")
async def help_panel(query: CallbackQuery):
    await query.answer()
    await show(
        query,
        "<b>Как получить презентацию</b>\n\n"
        "1. Загрузите исходный PPTX в «Шаблон презентации».\n"
        "2. Нажмите «Создать PPTX» и выберите период.\n"
        "3. Получите презентацию и сверку возможных ошибок.\n\n"
        "«PPTX из отчёта» использует последнюю выгрузку без запроса к ВК.\n"
        "Опечатки и одноимённые профили уточняются в «Привязки профилей».\n\n"
        "Период относится к дате постов, лайки — на момент сбора. "
        "Все команды доступны через /help.",
        likes_keyboard(),
    )
