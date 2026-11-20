"""Explicit Telegram transfer deadlines for larger presentation files."""

from pathlib import Path

from aiohttp import FormData
from aiohttp.payload import AsyncIterablePayload
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import SendDocument
from aiogram.types import FSInputFile, Message

FILE_TIMEOUT = 300


class ReportInputFile(FSInputFile):
    """A saved report that can be streamed with a known multipart length."""


class SizedFilePayload(AsyncIterablePayload):
    def __init__(self, stream, size: int):
        super().__init__(stream)
        self._size = size


class ArgusBotSession(AiohttpSession):
    def build_form_data(self, bot, method):
        if not isinstance(method, SendDocument) or not isinstance(method.document, ReportInputFile):
            return super().build_form_data(bot, method)
        files = {}
        form = FormData(quote_fields=False)
        for key, raw in method.model_dump(warnings=False).items():
            value = self.prepare_value(raw, bot=bot, files=files)
            if value:
                form.add_field(key, value)
        for key, file in files.items():
            stream = file.read(bot)
            if isinstance(file, ReportInputFile):
                stream = SizedFilePayload(stream, Path(file.path).stat().st_size)
            form.add_field(key, stream, filename=file.filename or key)
        return form


async def send_file(message: Message, document, *, request_timeout=FILE_TIMEOUT, **kwargs):
    method = message.answer_document(document, **kwargs)
    return await message.bot(method, request_timeout=request_timeout)
