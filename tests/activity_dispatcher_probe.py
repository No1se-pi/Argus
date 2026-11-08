"""Isolated dispatcher smoke test: aiogram routers belong to one dispatcher."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update

from app.bot.factory import create_dispatcher
from app.config import Settings


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append((method, timeout))
        return Message(
            message_id=len(self.calls),
            date=datetime.now(timezone.utc),
            chat=Chat(id=42, type="private"),
        )

    async def stream_content(self, *args, **kwargs):
        yield b""


async def main():
    root = Path(sys.argv[1])
    directory = root / "exports/activity" / ("a" * 32)
    directory.mkdir(parents=True)
    for name in ("activity.pptx", "audit.zip"):
        (directory / name).write_bytes(b"fixture")
    (directory / "manifest.json").write_text(
        json.dumps({"period": "12.10.2026–19.10.2026"}), encoding="utf-8"
    )
    service = SimpleNamespace(
        database=SimpleNamespace(path=root / "argus.sqlite3"),
        timezone_name="Europe/Moscow",
        generate=AsyncMock(
            return_value=SimpleNamespace(
                directory=directory, slides=29, reviews=50, posts=17, warnings=()
            )
        ),
    )
    settings = Settings(_env_file=None, bot_token="123456:TEST_ONLY", ADMIN_IDS="42")
    dispatcher = create_dispatcher(settings, activity_report_service=service)
    session = Session()
    bot = Bot(token="123456:TEST_ONLY", session=session)
    sequence = 0

    async def message(user):
        nonlocal sequence
        sequence += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": sequence,
                    "message": {
                        "message_id": sequence,
                        "date": 1794014400,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": user, "is_bot": False, "first_name": "Admin"},
                        "text": "/activity_pptx 12.10.2026 19.10.2026",
                    },
                }
            ),
        )

    async def click(user, data):
        nonlocal sequence
        sequence += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": sequence,
                    "callback_query": {
                        "id": str(sequence),
                        "chat_instance": "fixture",
                        "from": {"id": user, "is_bot": False, "first_name": "Admin"},
                        "data": data,
                        "message": {
                            "message_id": 1,
                            "date": 1794014400,
                            "chat": {"id": 42, "type": "private"},
                            "text": "Menu",
                        },
                    },
                }
            ),
        )

    await message(43)
    service.generate.assert_not_awaited()
    await click(43, "activity:retry:" + directory.name)
    assert not [call for call, _ in session.calls if call.__api_method__ == "sendDocument"]
    await message(42)
    service.generate.assert_awaited_once()
    assert len([call for call, _ in session.calls if call.__api_method__ == "sendDocument"]) == 2
    await click(42, "activity:retry:" + directory.name)
    assert len([call for call, _ in session.calls if call.__api_method__ == "sendDocument"]) == 4
    service.generate.assert_awaited_once()
    await click(42, "activity:retry:../../secrets")
    assert len([call for call, _ in session.calls if call.__api_method__ == "sendDocument"]) == 4
    assert all(
        timeout == 300 for call, timeout in session.calls if call.__api_method__ == "sendDocument"
    )
    await click(42, "activity:custom")
    state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    assert await state.get_state()
    await click(42, "activity:menu")
    assert await state.get_state() is None
    await dispatcher.storage.close()
    await bot.session.close()


asyncio.run(main())
