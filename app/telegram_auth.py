import re
from dataclasses import dataclass

from telethon import TelegramClient, errors

from app.config import Settings


class TelegramAuthError(Exception):
    """Raised when Telethon auth cannot continue."""


class TelegramAuthRestartRequired(TelegramAuthError):
    """Raised when the current Telethon auth attempt cannot be reused."""


class TelegramAlreadyAuthorized(TelegramAuthError):
    """Raised when the configured Telethon session is already authorized."""


class TelegramPasswordRequired(Exception):
    """Raised when Telegram account has two-factor password enabled."""


CODE_PATTERN = re.compile(r"(?<!\d)(?:\d[\s-]*){4,8}(?!\d)")


@dataclass
class PendingTelegramAuth:
    client: TelegramClient
    phone: str
    phone_code_hash: str


class TelegramAuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pending: dict[int, PendingTelegramAuth] = {}

    def is_configured(self) -> bool:
        return self.settings.tg_api_id is not None and self.settings.tg_api_hash is not None

    async def send_code(self, admin_id: int, phone: str) -> None:
        if not self.is_configured():
            raise TelegramAuthError("TG_API_ID and TG_API_HASH must be set in .env first.")

        await self.cancel(admin_id)
        self.settings.telethon_session_file.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            self.settings.telethon_session,
            self.settings.tg_api_id,
            self.settings.tg_api_hash.get_secret_value(),
        )
        await client.connect()
        try:
            if await client.is_user_authorized():
                raise TelegramAlreadyAuthorized("Telethon session уже авторизована.")
            sent_code = await client.send_code_request(phone)
        except TelegramAlreadyAuthorized:
            await client.disconnect()
            raise
        except errors.PhoneNumberInvalidError as exc:
            await client.disconnect()
            raise TelegramAuthError(
                "Telegram отклонил номер телефона. Проверь формат, например +79991234567."
            ) from exc
        except errors.FloodWaitError as exc:
            await client.disconnect()
            seconds = _flood_seconds(exc)
            raise TelegramAuthRestartRequired(
                "Telegram временно ограничил отправку кодов. "
                f"Подожди {seconds} сек. и запусти /tg_auth заново."
            ) from exc
        except Exception:
            await client.disconnect()
            raise

        self._pending[admin_id] = PendingTelegramAuth(
            client=client,
            phone=phone,
            phone_code_hash=sent_code.phone_code_hash,
        )

    async def sign_in_code(self, admin_id: int, code: str) -> None:
        pending = self._pending.get(admin_id)
        if pending is None:
            raise TelegramAuthRestartRequired(
                "Нет активной попытки входа. Запусти /tg_auth заново."
            )

        normalized_code = _normalize_code(code)
        if not normalized_code:
            raise TelegramAuthError(
                "Не вижу код Telegram. Отправь только 5-6 цифр из сообщения Telegram."
            )

        try:
            await pending.client.sign_in(
                phone=pending.phone,
                code=normalized_code,
                phone_code_hash=pending.phone_code_hash,
            )
        except errors.SessionPasswordNeededError as exc:
            raise TelegramPasswordRequired("Telegram 2FA password is required.") from exc
        except errors.PhoneCodeExpiredError as exc:
            await self.cancel(admin_id)
            raise TelegramAuthRestartRequired(
                "Код Telegram истёк. Запусти /tg_auth заново и введи самый новый код."
            ) from exc
        except errors.PhoneCodeInvalidError as exc:
            raise TelegramAuthError(
                "Код Telegram неверный. Проверь цифры и отправь код ещё раз."
            ) from exc
        except (errors.PhoneCodeEmptyError, errors.PhoneCodeHashEmptyError) as exc:
            await self.cancel(admin_id)
            raise TelegramAuthRestartRequired(
                "Telegram не принял код. Запусти /tg_auth заново."
            ) from exc
        except errors.FloodWaitError as exc:
            await self.cancel(admin_id)
            seconds = _flood_seconds(exc)
            raise TelegramAuthRestartRequired(
                "Telegram временно ограничил вход. "
                f"Подожди {seconds} сек. и запусти /tg_auth заново."
            ) from exc

        await self._finish(admin_id)

    async def sign_in_password(self, admin_id: int, password: str) -> None:
        pending = self._pending.get(admin_id)
        if pending is None:
            raise TelegramAuthRestartRequired(
                "Нет активной попытки входа. Запусти /tg_auth заново."
            )

        try:
            await pending.client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            raise TelegramAuthError(
                "Telegram 2FA пароль неверный. Отправь пароль ещё раз."
            ) from exc
        except errors.FloodWaitError as exc:
            await self.cancel(admin_id)
            seconds = _flood_seconds(exc)
            raise TelegramAuthRestartRequired(
                "Telegram временно ограничил вход. "
                f"Подожди {seconds} сек. и запусти /tg_auth заново."
            ) from exc

        await self._finish(admin_id)

    async def cancel(self, admin_id: int) -> None:
        pending = self._pending.pop(admin_id, None)
        if pending is not None:
            await pending.client.disconnect()

    async def _finish(self, admin_id: int) -> None:
        pending = self._pending.pop(admin_id, None)
        if pending is not None:
            await pending.client.disconnect()


def _normalize_code(code: str) -> str:
    compact = "".join(char for char in code.strip() if char.isdigit())
    has_only_code_chars = all(
        char.isdigit() or char.isspace() or char == "-"
        for char in code
    )
    if 4 <= len(compact) <= 8 and has_only_code_chars:
        return compact

    match = CODE_PATTERN.search(code)
    if match is None:
        return ""

    return "".join(char for char in match.group(0) if char.isdigit())


def _flood_seconds(exc: errors.FloodWaitError) -> int:
    return int(getattr(exc, "seconds", 0) or 0)
