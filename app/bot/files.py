"""Explicit Telegram transfer deadlines for larger presentation files."""

from aiogram.types import Message

FILE_TIMEOUT = 300


async def send_file(message: Message, document, **kwargs):
    method = message.answer_document(document, **kwargs)
    return await message.bot(method, request_timeout=FILE_TIMEOUT)
