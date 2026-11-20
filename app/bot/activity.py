"""Admin-only full presentation workflow and retry of already generated files."""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from html import escape

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.files import FILE_TIMEOUT, ReportInputFile, send_file
from app.reports.data import Period
from app.reports.pptx import report_title
from app.reports.service import ActivityReportService
from app.time import resolve_timezone
from app.vk.client import VKAPIError, vk_error_text

logger = logging.getLogger(__name__)
router = Router(name="activity_reports")
NETWORK_ERRORS = (TelegramNetworkError, TelegramServerError, aiohttp.ClientError, asyncio.TimeoutError)
NOTICE_TIMEOUT = 10


class ActivityStates(StatesGroup):
    waiting_period = State()


def keyboard(export_id: str | None = None, retry_file: str | None = None):
    rows = []
    if export_id:
        callback = f"activity:retry:{export_id}"
        if retry_file:
            callback += f":{retry_file}"
        rows.append(
            [
                InlineKeyboardButton(
                    text="📤 Повторить отправку", callback_data=callback
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="📅 Указать даты", callback_data="activity:custom")],
            [
                InlineKeyboardButton(text="Последние 7 дней", callback_data="activity:7d"),
                InlineKeyboardButton(text="30 дней", callback_data="activity:30d"),
            ],
            [
                InlineKeyboardButton(text="🔐 Подключить ВК", callback_data="vklui:connect"),
                InlineKeyboardButton(text="📎 Шаблон", callback_data="vklui:template"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def report_notice(message, text, **kwargs):
    try:
        await message.bot(message.answer(text, **kwargs), request_timeout=NOTICE_TIMEOUT)
    except NETWORK_ERRORS as exc:
        logger.warning("Activity report notice failed error=%s", type(exc).__name__)


async def deliver(message, directory, only_file=None):
    filename = "activity.pptx"
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        period = manifest.get("period", "")
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}–\d{2}\.\d{2}\.\d{4}", period):
            filename = report_title(period) + ".pptx"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    documents = (
        ("pptx", "activity.pptx", filename, "Презентация по выбранному периоду"),
        ("audit", "audit.zip", "audit.zip", "Сверка данных и ограничения полноты."),
    )
    failed = []
    for kind, basename, display_name, caption in documents:
        if only_file is not None and only_file != kind:
            continue
        path = directory / basename
        if not path.is_file():
            logger.error("Activity report file missing export=%s file=%s", directory.name, basename)
            failed.append(kind)
            continue
        # Allow a slow transfer, but abandon a stalled first connection in time to retry.
        first_timeout = min(FILE_TIMEOUT, max(30, path.stat().st_size // (64 * 1024) + 15))
        for attempt, timeout in enumerate((first_timeout, FILE_TIMEOUT), start=1):
            try:
                await send_file(
                    message,
                    ReportInputFile(path, filename=display_name),
                    caption=caption,
                    request_timeout=timeout,
                )
            except NETWORK_ERRORS as exc:
                logger.warning(
                    "Activity report transfer failed export=%s file=%s attempt=%s error=%s cause=%s",
                    directory.name, basename, attempt, type(exc).__name__,
                    type(exc.__cause__).__name__,
                )
            else:
                logger.info("Activity report sent export=%s file=%s", directory.name, basename)
                break
        else:
            failed.append(kind)
    if failed:
        retry_file = failed[0] if len(failed) == 1 else None
        if retry_file == "pptx":
            status = "Telegram не подтвердил отправку презентации."
        elif retry_file == "audit":
            status = "Telegram не подтвердил отправку архива сверки."
        else:
            status = "Telegram не подтвердил отправку презентации и архива."
        await report_notice(
            message, status + " Файлы сохранены. Можно повторить отправку.",
            reply_markup=keyboard(directory.name, retry_file),
        )


async def generate(message, value, service):
    if service is None:
        await message.answer("Генератор презентаций пока недоступен.")
        return
    try:
        period = Period.parse(value, service.timezone_name)
    except ValueError as exc:
        await message.answer(escape(str(exc)), reply_markup=keyboard())
        return
    try:
        await asyncio.wait_for(service.check_vk_access(), timeout=12)
    except VKAPIError as exc:
        await report_notice(
            message,
            f"{escape(vk_error_text(exc))}\nПосле подключения заново выберите период.",
            reply_markup=keyboard(),
        )
        return
    except ValueError as exc:
        await report_notice(message, escape(str(exc)), reply_markup=keyboard())
        return
    except (aiohttp.ClientError, asyncio.TimeoutError):
        await report_notice(
            message, "ВК не ответил на проверку доступа. Повторите попытку.",
            reply_markup=keyboard(),
        )
        return
    await report_notice(
        message,
        "Собираю свежие лайки ВК и отзывы Яндекса/2ГИС за выбранные даты, "
        "затем подготовлю полную презентацию. Это может занять несколько минут."
    )
    try:
        result = await service.generate(period, collect_vk=True)
    except ValueError as exc:
        await message.answer(escape(str(exc)), reply_markup=keyboard())
        return
    except Exception as exc:
        logger.exception("Activity report failed error=%s", type(exc).__name__)
        await message.answer(
            "Не удалось сформировать презентацию. Предыдущие файлы сохранены. "
            "Проверьте наличие Chromium и доступность шаблона.",
            reply_markup=keyboard(),
        )
        return
    warning = (
        f"\nЕсть ограничения данных: {len(result.warnings)}. Подробности в сверке."
        if result.warnings
        else ""
    )
    if getattr(result, "vk_error", None):
        warning += f"\n\n⚠️ Лайки ВК — н/д: {escape(result.vk_error)}"
    await report_notice(
        message,
        f"Готово: {result.slides} слайдов, {result.reviews} отзывов, "
        f"постов ВК: {result.posts if result.posts is not None else 'н/д'}.{warning}",
        reply_markup=keyboard(result.directory.name),
    )
    await deliver(message, result.directory)


@router.message(Command("activity_pptx"))
async def activity_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    activity_report_service: ActivityReportService | None = None,
):
    await state.clear()
    if not command.args:
        await message.answer(
            "Полная презентация: отзывы Яндекс/2ГИС, сводки, лайки и критические случаи.\n"
            "Укажите период: /activity_pptx 12.10.2026 19.10.2026",
            reply_markup=keyboard(),
        )
        return
    retry = re.fullmatch(r"retry\s+([a-f0-9]{32})(?:\s+(pptx|audit))?", command.args.strip())
    if retry is not None and activity_report_service is not None:
        await retry_export(message, activity_report_service, *retry.groups())
        return
    await generate(message, command.args, activity_report_service)


@router.callback_query(F.data == "activity:menu")
async def activity_menu(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.answer(
            "<b>📑 Полная презентация</b>\n\n"
            "Отзывы всех тональностей, сводки по отделениям, рейтинги, лайки и критические случаи. "
            "После выбора периода бот сам соберёт лайки и отзывы. "
            "ФИО остаются как в исходном шаблоне.",
            reply_markup=keyboard(),
        )


@router.callback_query(F.data == "activity:custom")
async def activity_dates(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(ActivityStates.waiting_period)
    if isinstance(query.message, Message):
        await query.message.answer(
            "Отправьте две даты: <code>12.10.2026 19.10.2026</code>. "
            "Оба дня включаются, время — Москва.",
            reply_markup=keyboard(),
        )


@router.message(ActivityStates.waiting_period, F.text & ~F.text.startswith("/"))
async def activity_period(
    message: Message,
    state: FSMContext,
    activity_report_service: ActivityReportService | None = None,
):
    if activity_report_service is None:
        await message.answer("Генератор презентаций пока недоступен.")
        return
    try:
        Period.parse(message.text, activity_report_service.timezone_name)
    except ValueError as exc:
        await message.answer(escape(str(exc)))
        return
    await state.clear()
    await generate(message, message.text, activity_report_service)


@router.callback_query(F.data.in_({"activity:7d", "activity:30d"}))
async def activity_recent(
    query: CallbackQuery, activity_report_service: ActivityReportService | None = None
):
    await query.answer()
    if not isinstance(query.message, Message) or activity_report_service is None:
        return
    days = 7 if query.data.endswith("7d") else 30
    last = datetime.now(resolve_timezone(activity_report_service.timezone_name)).date()
    first = last - timedelta(days=days - 1)
    await generate(query.message, f"{first:%d.%m.%Y} {last:%d.%m.%Y}", activity_report_service)


@router.callback_query(F.data.startswith("activity:retry:"))
async def activity_retry(
    query: CallbackQuery, activity_report_service: ActivityReportService | None = None
):
    try:
        await query.bot(query.answer(), request_timeout=NOTICE_TIMEOUT)
    except NETWORK_ERRORS as exc:
        logger.warning("Activity report retry acknowledgment failed error=%s", type(exc).__name__)
    if not isinstance(query.message, Message) or activity_report_service is None:
        return
    match = re.fullmatch(r"activity:retry:([a-f0-9]{32})(?::(pptx|audit))?", query.data)
    if match is None:
        return
    await retry_export(query.message, activity_report_service, *match.groups())


async def retry_export(message, service, identity, only_file=None):
    directory = service.database.path.parent / "exports" / "activity" / identity
    if not (directory / "manifest.json").is_file():
        await report_notice(message, "Сохранённая презентация не найдена.", reply_markup=keyboard())
        return
    await deliver(message, directory, only_file)
