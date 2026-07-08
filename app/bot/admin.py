from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.access import AccessRequestService
from app.config import Settings


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.settings.admin_ids:
            if isinstance(event, Message):
                await self._handle_denied_message(event, data)
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа. Отправь /request_access в чат с ботом.", show_alert=True)
            return None
        return await handler(event, data)

    async def _handle_denied_message(self, event: Message, data: dict[str, Any]) -> None:
        command = (event.text or "").split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        if command in {"/start", "/request_access"} and event.from_user is not None:
            access_service = data.get("access_service")
            if isinstance(access_service, AccessRequestService):
                await access_service.request_access(event.bot, event.from_user, event.chat.id)
                return
        await event.answer("Нет доступа. Отправь /request_access, чтобы запросить его у админов.")
