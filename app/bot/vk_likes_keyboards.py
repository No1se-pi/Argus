from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def likes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Собрать лайки", callback_data="vklui:period:table"),
                InlineKeyboardButton(text="📽 Создать PPTX", callback_data="vklui:period:pptx"),
            ],
            [
                InlineKeyboardButton(text="📋 Последний отчёт", callback_data="vkl:report"),
                InlineKeyboardButton(text="📽 PPTX из отчёта", callback_data="vklui:saved"),
            ],
            [InlineKeyboardButton(text="📎 Шаблон презентации", callback_data="vklui:template")],
            [InlineKeyboardButton(text="👥 Привязки профилей", callback_data="vklui:bindings")],
            [InlineKeyboardButton(text="🔐 Подключить / обновить ВК", callback_data="vklui:connect")],
            [
                InlineKeyboardButton(text="🔑 Проверить доступ ВК", callback_data="vklui:auth"),
                InlineKeyboardButton(text="⚙️ Настроить ВК", callback_data="setup:vk"),
            ],
            [InlineKeyboardButton(text="❔ Как пользоваться", callback_data="vklui:help")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def period_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="7 суток", callback_data=f"vklui:run:{action}:7d"),
                InlineKeyboardButton(text="30 суток", callback_data=f"vklui:run:{action}:30d"),
            ],
            [
                InlineKeyboardButton(
                    text="Последние 17 постов", callback_data=f"vklui:run:{action}:last 17"
                )
            ],
            [InlineKeyboardButton(text="📅 Указать даты", callback_data=f"vklui:custom:{action}")],
            [InlineKeyboardButton(text="⬅️ Лайки и презентации", callback_data="vkl:menu")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="vkl:menu")],
        ]
    )


def template_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Загрузить / заменить PPTX", callback_data="vklui:upload"
                )
            ],
            [InlineKeyboardButton(text="📥 Скачать шаблон", callback_data="vklui:download")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="vkl:menu")],
        ]
    )


def bindings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить / изменить", callback_data="vklui:bind")],
            [
                InlineKeyboardButton(
                    text="📥 Скачать привязки", callback_data="vklui:bindings_export"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="vkl:menu")],
        ]
    )
