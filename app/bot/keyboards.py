from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Дашборды", callback_data="menu:dash"),
                InlineKeyboardButton(text="🔔 Алерты", callback_data="menu:alerts"),
            ],
            [
                InlineKeyboardButton(text="🟦 VK", callback_data="vk:menu"),
                InlineKeyboardButton(text="✈️ Telegram", callback_data="tg:menu"),
            ],
            [
                InlineKeyboardButton(text="🧩 Модули", callback_data="menu:modules"),
                InlineKeyboardButton(text="⚙️ Настройка", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton(text="🩺 Статус", callback_data="menu:status")],
        ]
    )


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:status")],
            [InlineKeyboardButton(text="🧩 Модули", callback_data="menu:modules")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def modules_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟦 VK Monitor", callback_data="vk:menu"),
                InlineKeyboardButton(text="✈️ Telegram Monitor", callback_data="tg:menu"),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:modules")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def vk_menu_keyboard(available: bool) -> InlineKeyboardMarkup:
    if not available:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настроить VK", callback_data="setup:vk")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🩺 VK статус", callback_data="vk:status"),
                InlineKeyboardButton(text="⚙️ VK настройка", callback_data="setup:vk"),
            ],
            [InlineKeyboardButton(text="🔄 Синхронизировать", callback_data="vk:sync")],
            [
                InlineKeyboardButton(text="💬 Комментарии", callback_data="vk:comments"),
                InlineKeyboardButton(text="📝 Посты", callback_data="vk:posts"),
            ],
            [
                InlineKeyboardButton(text="📊 24h", callback_data="vk:dash:24h"),
                InlineKeyboardButton(text="📊 7d", callback_data="vk:dash:7d"),
                InlineKeyboardButton(text="📊 30d", callback_data="vk:dash:30d"),
            ],
            [
                InlineKeyboardButton(text="🔕 Выключить", callback_data="confirm:disable_vk"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def telegram_menu_keyboard(available: bool) -> InlineKeyboardMarkup:
    if not available:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настроить Telegram", callback_data="setup:telegram")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🩺 TG статус", callback_data="tg:status"),
                InlineKeyboardButton(text="📚 Источники", callback_data="tg:sources"),
            ],
            [
                InlineKeyboardButton(text="📊 24h", callback_data="tg:dash:24h"),
                InlineKeyboardButton(text="📊 7d", callback_data="tg:dash:7d"),
                InlineKeyboardButton(text="📊 30d", callback_data="tg:dash:30d"),
            ],
            [
                InlineKeyboardButton(text="🔕 Выключить", callback_data="confirm:disable_tg"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def dashboards_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="VK за 24 часа", callback_data="vk:dash:24h"),
                InlineKeyboardButton(text="VK за 7 дней", callback_data="vk:dash:7d"),
            ],
            [InlineKeyboardButton(text="VK за 30 дней", callback_data="vk:dash:30d")],
            [
                InlineKeyboardButton(text="TG за 24 часа", callback_data="tg:dash:24h"),
                InlineKeyboardButton(text="TG за 7 дней", callback_data="tg:dash:7d"),
            ],
            [InlineKeyboardButton(text="TG за 30 дней", callback_data="tg:dash:30d")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Настроить VK", callback_data="setup:vk")],
            [InlineKeyboardButton(text="Настроить Telegram Monitor", callback_data="setup:telegram")],
            [
                InlineKeyboardButton(text="Включить VK", callback_data="vk:watch_on"),
                InlineKeyboardButton(text="Выключить VK", callback_data="confirm:disable_vk"),
            ],
            [
                InlineKeyboardButton(text="Включить TG", callback_data="tg:watch_on"),
                InlineKeyboardButton(text="Выключить TG", callback_data="confirm:disable_tg"),
            ],
            [InlineKeyboardButton(text="Проверить конфиг", callback_data="setup:start")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def vk_setup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Ввести VK_GROUP_TOKEN", callback_data="setup:vk_gt")],
            [InlineKeyboardButton(text="Ввести VK_USER_ACCESS_TOKEN", callback_data="setup:vk_ut")],
            [InlineKeyboardButton(text="Ввести VK_GROUP_ID", callback_data="setup:vk_gid")],
            [InlineKeyboardButton(text="Проверить VK", callback_data="vk:status")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def telegram_setup_keyboard(can_auth: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_auth:
        rows.append([InlineKeyboardButton(text="Авторизовать Telethon", callback_data="tg:auth")])
    rows.extend(
        [
            [InlineKeyboardButton(text="🩺 TG статус", callback_data="tg:status")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alerts_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Включить алерты VK", callback_data="alerts:vk_on")],
            [InlineKeyboardButton(text="Выключить алерты VK", callback_data="alerts:vk_off")],
            [InlineKeyboardButton(text="Включить алерты постов", callback_data="alerts:posts_on")],
            [InlineKeyboardButton(text="Выключить алерты постов", callback_data="alerts:posts_off")],
            [InlineKeyboardButton(text="Включить алерты комментариев", callback_data="alerts:comments_on")],
            [InlineKeyboardButton(text="Выключить алерты комментариев", callback_data="alerts:comments_off")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def setup_cancel_keyboard(can_skip: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if can_skip:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="setup:skip")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="setup:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"do:{action}"),
                InlineKeyboardButton(text="❌ Нет", callback_data="menu:main"),
            ]
        ]
    )
