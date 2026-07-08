from contextlib import suppress
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User

from app.config import Settings


class AccessRequestService:
    def __init__(self, settings: Settings, env_path: Path = Path(".env")) -> None:
        self.settings = settings
        self.env_path = env_path
        self._pending: set[int] = set()

    async def request_access(self, bot: Bot, user: User, chat_id: int) -> None:
        if user.id in self.settings.admin_ids:
            await bot.send_message(chat_id, "Доступ уже есть. Открой /start.")
            return

        if user.id in self._pending:
            await bot.send_message(chat_id, "Заявка уже отправлена администраторам.")
            return

        self._pending.add(user.id)
        await bot.send_message(chat_id, "Заявка на доступ отправлена администраторам.")

        text = "\n".join(
            [
                "<b>Запрос доступа к Argus</b>",
                f"ID: <code>{user.id}</code>",
                f"Имя: {escape(user.full_name)}",
                f"Username: @{escape(user.username)}" if user.username else "Username: -",
            ]
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Выдать доступ",
                        callback_data=f"access:approve:{user.id}",
                    ),
                    InlineKeyboardButton(
                        text="Отклонить",
                        callback_data=f"access:deny:{user.id}",
                    ),
                ]
            ]
        )
        for admin_id in self.settings.admin_ids:
            with suppress(Exception):
                await bot.send_message(admin_id, text, reply_markup=keyboard)

    def approve(self, user_id: int) -> bool:
        admin_ids = set(self.settings.admin_ids)
        already_added = user_id in admin_ids
        admin_ids.add(user_id)
        self._write_admin_ids(admin_ids)
        self._pending.discard(user_id)
        return not already_added

    def deny(self, user_id: int) -> None:
        self._pending.discard(user_id)

    def _write_admin_ids(self, admin_ids: set[int]) -> None:
        value = ",".join(str(item) for item in sorted(admin_ids))
        if self.env_path.exists():
            text = self.env_path.read_text(encoding="utf-8")
            lines = text.splitlines()
        else:
            lines = []

        updated_lines: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("ADMIN_IDS=") or stripped.startswith("export ADMIN_IDS="):
                prefix = "export ADMIN_IDS=" if stripped.startswith("export ADMIN_IDS=") else "ADMIN_IDS="
                updated_lines.append(f"{prefix}{value}")
                replaced = True
            else:
                updated_lines.append(line)

        if not replaced:
            if updated_lines and updated_lines[-1].strip():
                updated_lines.append("")
            updated_lines.append(f"ADMIN_IDS={value}")

        self.env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        self.settings.admin_ids_text = value
