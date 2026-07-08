from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message


CAPTION_LIMIT = 1024


async def answer_dashboard(
    message: Message,
    *,
    text: str,
    chart_png: bytes | None,
    filename: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if not chart_png:
        await message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
        return

    photo = BufferedInputFile(chart_png, filename=filename)
    if len(text) <= CAPTION_LIMIT:
        await message.answer_photo(photo, caption=text, reply_markup=reply_markup)
        return

    await message.answer_photo(photo, caption=_short_caption(text))
    await message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)


def _short_caption(text: str) -> str:
    header = "\n".join(line for line in text.splitlines()[:3] if line.strip()).strip()
    if not header:
        header = "<b>Argus dashboard</b>"
    caption = f"{header}\n\nПолный текст ниже."
    if len(caption) <= CAPTION_LIMIT:
        return caption
    return "<b>Argus dashboard</b>\n\nПолный текст ниже."
