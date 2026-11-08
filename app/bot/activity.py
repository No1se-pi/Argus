"""Admin-only full presentation workflow and retry of already generated files."""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from html import escape

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.files import send_file
from app.reports.data import Period
from app.reports.service import ActivityReportService
from app.time import resolve_timezone

logger = logging.getLogger(__name__)
router = Router(name="activity_reports")


class ActivityStates(StatesGroup):
    waiting_period = State()


def keyboard(export_id: str | None = None):
    rows = []
    if export_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📤 Повторить отправку", callback_data=f"activity:retry:{export_id}"
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
                InlineKeyboardButton(text="📎 Шаблон", callback_data="vklui:template"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def deliver(message, directory):
    try:
        await send_file(
            message,
            FSInputFile(directory / "activity.pptx"),
            caption="Презентация по выбранному периоду",
        )
        await send_file(
            message,
            FSInputFile(directory / "audit.zip"),
            caption="Сверка данных, совпадения ФИО, предположения и ограничения полноты.",
        )
    except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError):
        await message.answer(
            "Файлы сохранены, но Telegram не подтвердил передачу. Можно повторить отправку.",
            reply_markup=keyboard(directory.name),
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
    await message.answer(
        "Проверяю историю отзывов и готовлю полную презентацию. Новый сбор ВК не запускается. "
        "Дозагрузка истории может занять несколько минут."
    )
    try:
        result = await service.generate(period)
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
    await message.answer(
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
    await generate(message, command.args, activity_report_service)


@router.callback_query(F.data == "activity:menu")
async def activity_menu(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    if isinstance(query.message, Message):
        await query.message.answer(
            "<b>📑 Полная презентация</b>\n\n"
            "Отзывы всех тональностей, сводки по отделениям, рейтинги, лайки и критические случаи. "
            "ФИО остаются как в исходном шаблоне. Выберите период.",
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
    await query.answer()
    if not isinstance(query.message, Message) or activity_report_service is None:
        return
    identity = query.data.rsplit(":", 1)[-1]
    if not re.fullmatch("[a-f0-9]{32}", identity):
        return
    directory = activity_report_service.database.path.parent / "exports" / "activity" / identity
    if not (directory / "manifest.json").is_file():
        await query.message.answer("Сохранённая презентация не найдена.", reply_markup=keyboard())
        return
    await deliver(query.message, directory)
