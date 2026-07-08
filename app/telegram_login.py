import asyncio
import contextlib
from getpass import getpass

from telethon import TelegramClient, errors

from app.config import get_settings
from app.storage.database import Database
from app.storage.repositories import RuntimeSettingsRepository
from app.storage.schema import init_schema


class LoginCancelled(Exception):
    """Raised when local console input was cancelled."""


async def main() -> int:
    settings = get_settings()
    if settings.tg_api_id is None or settings.tg_api_hash is None:
        print("TG_API_ID and TG_API_HASH must be set in .env first.")
        return 2

    settings.telethon_session_file.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        settings.telethon_session,
        settings.tg_api_id,
        settings.tg_api_hash.get_secret_value(),
    )

    await client.connect()
    try:
        if await client.is_user_authorized():
            print(f"Telethon session is already authorized: {settings.telethon_session_file}")
            await _enable_telegram_monitor()
            return 0

        phone = _read_phone(settings.telegram_phone)
        if not phone:
            print("Номер телефона пустой.")
            return 2

        sent_code = await client.send_code_request(phone)
        print("")
        print(f"Код Telegram отправлен на аккаунт: {_mask_phone(phone)}")
        print(_delivery_hint(sent_code))
        print("Введи сюда именно login-code из Telegram, а не номер телефона.")
        print("Не отправляй этот код в Telegram-чаты, боты или saved messages.")
        print("")

        for attempt in range(1, 4):
            code = _read_code()
            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=sent_code.phone_code_hash,
                )
                break
            except errors.PhoneCodeInvalidError:
                if attempt >= 3:
                    print("Telegram отклонил код: неверный код.")
                    return 1
                print("Telegram отклонил код. Проверь цифры и попробуй ещё раз.")
            except errors.SessionPasswordNeededError:
                password = getpass("Telegram 2FA password: ")
                await client.sign_in(password=password)
                break

        if not await client.is_user_authorized():
            print("Вход не завершён. Попробуй позже.")
            return 1

        await _enable_telegram_monitor()
        print(f"Telethon session создана: {settings.telethon_session_file}")
        print("Перезапусти Argus, чтобы Telegram Monitor подключился к новой session.")
        return 0
    except errors.PhoneCodeInvalidError:
        print("Telegram rejected the code: invalid code.")
        return 1
    except errors.PhoneCodeExpiredError:
        print(
            "Telegram rejected the code: expired code. "
            "Run the command again and use a fresh code."
        )
        return 1
    except errors.PhoneNumberInvalidError:
        print("Telegram rejected the phone number. Check the format, e.g. +79991234567.")
        return 1
    except errors.PasswordHashInvalidError:
        print("Telegram rejected the 2FA password.")
        return 1
    except errors.FloodWaitError as exc:
        seconds = int(getattr(exc, "seconds", 0) or 0)
        print(f"Telegram rate limited login. Wait {seconds} seconds before trying again.")
        return 1
    except LoginCancelled:
        print("")
        print("Вход отменён.")
        return 130
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await client.disconnect()


def _read_code() -> str:
    while True:
        raw_code = _prompt("Login code, обычно 5 цифр: ").strip()
        code = "".join(char for char in raw_code if char.isdigit())
        if _looks_like_phone(raw_code, code):
            print("Это похоже на номер телефона. Здесь нужен код входа из Telegram.")
            continue
        if 4 <= len(code) <= 8:
            return code
        print("Код должен быть коротким: обычно 5 цифр из сообщения Telegram.")


def _read_phone(configured_phone: str | None) -> str:
    if configured_phone:
        phone = configured_phone.strip()
        print(f"Использую TELEGRAM_PHONE из .env: {_mask_phone(phone)}")
        print("Если номер неверный, измени TELEGRAM_PHONE в .env или очисти его.")
        return phone
    return _prompt("Номер телефона, например +79991234567: ").strip()


def _prompt(text: str) -> str:
    try:
        return input(text)
    except EOFError as exc:
        raise LoginCancelled from exc


def _looks_like_phone(raw_code: str, digits: str) -> bool:
    return raw_code.startswith("+") or len(digits) >= 10


def _mask_phone(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) < 6:
        return "***"
    return f"+{digits[:1]}***{digits[-4:]}"


def _delivery_hint(sent_code: object) -> str:
    code_type = sent_code.type.__class__.__name__  # type: ignore[attr-defined]
    timeout = getattr(sent_code, "timeout", None)

    if code_type == "SentCodeTypeApp":
        hint = "Способ доставки: сообщение в уже авторизованном Telegram-клиенте."
    elif code_type == "SentCodeTypeSms":
        hint = "Способ доставки: SMS."
    elif code_type == "SentCodeTypeCall":
        hint = "Способ доставки: звонок."
    elif code_type == "SentCodeTypeFlashCall":
        hint = "Способ доставки: flash-call."
    elif code_type == "SentCodeTypeMissedCall":
        hint = "Способ доставки: пропущенный звонок."
    elif code_type == "SentCodeTypeEmailCode":
        hint = "Способ доставки: email."
    else:
        hint = f"Способ доставки: {code_type}."

    if timeout:
        hint += f" Повторный запрос обычно доступен через {timeout} сек."
    return hint


async def _enable_telegram_monitor() -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    await database.connect()
    try:
        await init_schema(database)
        runtime_settings = RuntimeSettingsRepository(database)
        await runtime_settings.set("enable_telegram_monitor", "true", is_secret=False)
    finally:
        await database.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("")
        print("Вход отменён.")
        raise SystemExit(130) from None
