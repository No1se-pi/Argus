import asyncio
import io
import json
import logging
from html import escape
from uuid import uuid4
from zipfile import BadZipFile

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)

from app.bot.files import FILE_TIMEOUT, send_file
from app.bot.vk_likes_keyboards import likes_keyboard
from app.vk.client import VKAPIError, vk_error_text
from app.vk.likes import VKLikesService, parse_selection, report_csv, report_text
from app.vk.presentation import fill_presentation, name_tokens

router = Router(name="vk_likes")
logger = logging.getLogger(__name__)

LIKES_HELP = """<b>Лайки по профилям ВК</b>

Бот собирает всех, кто лайкнул посты сообщества. Каждый лайк добавляет 1 к счету профиля.
Список людей формируется автоматически.

/vk_likes 7d — посты за последние 7 суток
/vk_likes 2026-10-12 2026-10-19 — обе даты включительно
/vk_likes last 17 — последние 17 постов
/vk_likes https://vk.ru/wall-173208026_7730 — один пост
/vk_likes https://vk.ru/kait_20_official 7d — сообщество и период
/vk_likes_report — последний сохраненный отчет без обращения к ВК
/vk_likes_pptx 7d — ответьте этой командой на файл PPTX
/vk_likes_bind 12345 | ФИО — привязать строку к VK ID

Результат: первые 20 строк в сообщении и полный CSV со ссылками на профили и посты.
Период относится к дате публикации постов; лайки считаются на момент сбора.
Нужен VK_USER_ACCESS_TOKEN (/vk_setup). Без ссылки используется VK_GROUP_ID.
Поддерживаются также ссылки на окно reactions со списком лайков."""


async def send_report(message: Message, report: dict, service: VKLikesService) -> None:
    await message.answer(
        report_text(report, service.vk_service.settings.timezone),
        disable_web_page_preview=True,
    )
    await message.answer_document(
        BufferedInputFile(report_csv(report), filename=f"vk_likes_{report['group_id']}.csv"),
        caption="Полный отчет по профилям ВК",
        reply_markup=likes_keyboard(),
    )


async def collect_and_send(message: Message, args: str, service: VKLikesService) -> None:
    try:
        selection = parse_selection(args, service.vk_service.settings.timezone)
    except (ValueError, OverflowError):
        await message.answer(
            "Укажите период: /vk_likes 7d, /vk_likes last 17 или /vk_likes 2026-10-12 2026-10-19."
        )
        return
    await message.answer("Собираю посты и лайки ВК. Большой период может занять несколько минут.")
    try:
        report = await service.collect(selection)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    except VKAPIError as exc:
        logger.warning(
            "VK likes collection failed: method=%s code=%s reason=%s",
            exc.method,
            exc.code,
            exc.auth_reason,
        )
        detail = escape(str(exc)) if exc.code is None else vk_error_text(exc)
        await message.answer(
            f"ВК не позволил завершить сбор. {detail}\nПредыдущий отчет сохранен: /vk_likes_report."
        )
        return
    except (aiohttp.ClientError, TimeoutError):
        await message.answer(
            "Ошибка соединения с ВК. Повторите запрос позже. "
            "Предыдущий отчет сохранен: /vk_likes_report."
        )
        return
    await send_report(message, report, service)


@router.message(Command("vk_likes"))
async def likes_command(
    message: Message,
    command: CommandObject,
    vk_likes_service: VKLikesService,
    state: FSMContext,
) -> None:
    await state.clear()
    if not command.args:
        await message.answer(
            "<b>❤️ Лайки и презентации ВК</b>\n\n"
            "Соберите лайки за период или создайте PPTX по сохранённому шаблону.\n"
            "Шаблон и привязки профилей настраиваются кнопками ниже.",
            reply_markup=likes_keyboard(),
        )
        return
    await collect_and_send(message, command.args, vk_likes_service)


@router.message(Command("vk_likes_report"))
async def latest_command(message: Message, vk_likes_service: VKLikesService) -> None:
    try:
        report = await vk_likes_service.latest()
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if report is None:
        await message.answer("Сохраненного отчета нет. Запустите /vk_likes 7d.")
        return
    await send_report(message, report, vk_likes_service)


@router.message(Command("vk_likes_bind"))
async def bind_profile_command(
    message: Message, command: CommandObject, vk_likes_service: VKLikesService
) -> None:
    try:
        raw_id, name = (command.args or "").split("|", maxsplit=1)
        user_id, name = int(raw_id.strip()), name.strip()
        if user_id < 0 or len(name_tokens(name)) < 2 or len(name) > 200:
            raise ValueError
    except ValueError:
        await message.answer(
            "Формат: /vk_likes_bind 12345 | Фамилия Имя Отчество. "
            "Для удаления привязки укажите ID 0."
        )
        return
    settings = vk_likes_service.vk_service.runtime_settings
    bindings = json.loads(await settings.get("vk_likes_name_bindings") or "{}")
    # Remove normalized duplicates so corrections replace the previous binding.
    bindings = {
        key: value for key, value in bindings.items() if name_tokens(key) != name_tokens(name)
    }
    if user_id:
        bindings[name] = user_id
    await settings.set(
        "vk_likes_name_bindings", json.dumps(bindings, ensure_ascii=False), is_secret=False
    )
    await message.answer("Привязка сохранена." if user_id else "Привязка удалена.")


@router.message(Command("vk_likes_pptx"))
async def presentation_command(
    message: Message, command: CommandObject, vk_likes_service: VKLikesService
) -> None:
    document = message.document or (
        message.reply_to_message.document if message.reply_to_message else None
    )
    if document is None or not (document.file_name or "").lower().endswith(".pptx"):
        await message.answer(
            "Прикрепите исходный PPTX с подписью /vk_likes_pptx 7d "
            "или ответьте этой командой на файл. Можно указать две даты."
        )
        return
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await message.answer("Шаблон PPTX должен быть не больше 20 МБ.")
        return
    try:
        selection = parse_selection(
            command.args or "7d", vk_likes_service.vk_service.settings.timezone
        )
    except (ValueError, OverflowError):
        await message.answer("Укажите 7d или две даты: /vk_likes_pptx 2026-10-12 2026-10-19.")
        return
    template = io.BytesIO()
    try:
        await message.bot.download(document, destination=template, timeout=FILE_TIMEOUT)
    except (aiohttp.ClientError, asyncio.TimeoutError, TelegramNetworkError):
        await message.answer("Не удалось загрузить файл из Telegram. Повторите отправку.")
        return
    if template.getbuffer().nbytes > 20 * 1024 * 1024:
        await message.answer("Шаблон PPTX должен быть не больше 20 МБ.")
        return
    await message.answer("Собираю лайки и заполняю PPTX. Отдельно пришлю сверку совпадений.")
    try:
        report = await vk_likes_service.collect(selection)
        settings = vk_likes_service.vk_service.runtime_settings
        bindings = json.loads(await settings.get("vk_likes_name_bindings") or "{}")
        await send_presentation(message, template.getvalue(), report, bindings, vk_likes_service)
    except VKAPIError as exc:
        await message.answer(vk_error_text(exc), reply_markup=likes_keyboard())
        return
    except (ValueError, BadZipFile, aiohttp.ClientError, TimeoutError):
        await message.answer(
            "Не удалось создать PPTX. Проверьте файл, период и доступ к VK API. "
            "Исходный файл сохранен без изменений."
        )
        return


async def send_presentation(
    message: Message,
    template: bytes,
    report: dict,
    bindings: dict,
    service: VKLikesService,
) -> None:
    result = await asyncio.to_thread(
        fill_presentation,
        template,
        report,
        bindings=bindings,
        timezone_name=service.vk_service.settings.timezone,
    )
    export_dir = service.vk_service.settings.database_path.parent / "exports"
    def save_exports() -> None:
        export_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (("argus_vk_likes.pptx", result.pptx),
                              ("argus_vk_likes_matches.csv", result.audit_csv())):
            target = export_dir / name
            temporary = target.with_suffix(target.suffix + "." + uuid4().hex + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
    await asyncio.to_thread(save_exports)
    try:
        await send_file(
            message, BufferedInputFile(result.pptx, filename="argus_vk_likes.pptx"),
            caption="Презентация: ФИО и оформление сохранены; неподтвержденные значения — н/д.",
        )
        await send_file(
            message, BufferedInputFile(result.audit_csv(), filename="argus_vk_likes_matches.csv"),
            caption="Сверка: исходные и текущие числа, опечатки, неоднозначные и ненайденные профили. "
            "Привязка вручную: /vk_likes_bind VK_ID | ФИО.", reply_markup=likes_keyboard(),
        )
    except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError):
        await message.answer(
            "PPTX сформирован, но Telegram не подтвердил передачу файлов. "
            "Шаблон и отчёт сохранены. Повторите «PPTX из отчёта» — без нового сбора ВК.",
            reply_markup=likes_keyboard(),
        )


@router.callback_query(F.data.startswith("vkl:collect:"))
async def likes_collect(query: CallbackQuery, vk_likes_service: VKLikesService) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await collect_and_send(
            query.message, query.data.removeprefix("vkl:collect:"), vk_likes_service
        )


@router.callback_query(F.data == "vkl:report")
async def likes_latest(query: CallbackQuery, vk_likes_service: VKLikesService) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await latest_command(query.message, vk_likes_service)
