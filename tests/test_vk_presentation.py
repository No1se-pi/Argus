import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from aiogram.filters import CommandObject
from lxml import etree

from app.bot.vk_likes import bind_profile_command, presentation_command
from app.vk.presentation import NS, fill_presentation


def template_bytes(names, values=None):
    values = values or ["15"] * len(names)
    root = etree.Element(f"{{{NS['p']}}}sld", nsmap=NS)
    tree = etree.SubElement(root, f"{{{NS['p']}}}cSld")
    table = etree.SubElement(tree, f"{{{NS['a']}}}tbl")
    for name, value in [("ФИО", "Лайки"), *zip(names, values, strict=True)]:
        row = etree.SubElement(table, f"{{{NS['a']}}}tr")
        for text in (name, value):
            cell = etree.SubElement(row, f"{{{NS['a']}}}tc")
            body = etree.SubElement(cell, f"{{{NS['a']}}}txBody")
            paragraph = etree.SubElement(body, f"{{{NS['a']}}}p")
            # Names and numeric values can consist of several styled runs.
            for part in (text[:2], text[2:]):
                run = etree.SubElement(paragraph, f"{{{NS['a']}}}r")
                properties = etree.SubElement(run, f"{{{NS['a']}}}rPr", sz="2400", b="1")
                etree.SubElement(properties, f"{{{NS['a']}}}latin", typeface="Arial")
                etree.SubElement(run, f"{{{NS['a']}}}t").text = part
            etree.SubElement(cell, f"{{{NS['a']}}}tcPr", marL="3000")
    for text in [
        "Актуально на 19 октября в 22:57",
        "Счет за последние 17 постов",
        "За неделю с 12.10 по 19.10 вышло 17 постов",
    ]:
        shape = etree.SubElement(tree, f"{{{NS['p']}}}sp")
        etree.SubElement(shape, f"{{{NS['a']}}}t").text = text
    target = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    presentation = f'''<p:presentation xmlns:p="{NS["p"]}" xmlns:r="{target}">
      <p:sldIdLst><p:sldId id="256" r:id="slideA"/><p:sldId id="257" r:id="slideB"/>
      </p:sldIdLst></p:presentation>'''
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="slideA" Target="slides/slide42.xml"/>
      <Relationship Id="slideB" Target="slides/slide1.xml"/></Relationships>"""
    result = io.BytesIO()
    with ZipFile(result, "w") as package:
        package.comment = b"original metadata"
        package.writestr("ppt/presentation.xml", presentation)
        package.writestr("ppt/_rels/presentation.xml.rels", relationships)
        package.writestr("ppt/slides/slide42.xml", etree.tostring(root))
        package.writestr("ppt/slides/slide1.xml", f'<p:sld xmlns:p="{NS["p"]}"/>')
        package.writestr("ppt/media/image1.png", b"unchanged image bytes")
        package.writestr("ppt/theme/theme1.xml", b"unchanged theme bytes")
    return result.getvalue()


def report_for(profiles):
    # Profiles: (ID, VK display name, number of liked posts).
    return {
        "posts_count": 3,
        "posts": [
            {"id": post, "user_ids": [uid for uid, name, count in profiles if count >= post]}
            for post in range(1, 4)
        ],
        "rows": [
            {"user_id": uid, "full_name": name, "likes": count}
            for uid, name, count in profiles
            if count
        ],
        "finished_at": "2026-11-07T03:30:00+00:00",
        "period_start": "2026-10-11T21:00:00+00:00",
        "period_end_exclusive": "2026-10-19T21:00:00+00:00",
    }


def slide_xml(pptx):
    with ZipFile(io.BytesIO(pptx)) as package:
        return etree.fromstring(package.read("ppt/slides/slide42.xml"))


def test_preserves_names_styles_other_slides_media_and_slide_order():
    source = template_bytes(["Колындёнков Вадим Дмитриевич", "Сорокина Татьяна Алексеевна "])
    report = report_for([(1, "Вадим Колынденков", 3), (2, "Татьяна Сорокина", 2)])
    result = fill_presentation(source, report)
    assert result.slides_updated == [1]  # Relationship order, not slide42 filename.
    assert [item.value for item in result.matches] == ["3", "2"]
    assert all(item.status == "exact" for item in result.matches)
    old, new = slide_xml(source), slide_xml(result.pptx)
    for before, after in zip(
        old.xpath(".//a:tr/a:tc[1]", namespaces=NS),
        new.xpath(".//a:tr/a:tc[1]", namespaces=NS),
        strict=True,
    ):
        assert etree.tostring(before, method="c14n") == etree.tostring(after, method="c14n")
    assert [etree.tostring(node) for node in old.xpath(".//a:rPr|.//a:tcPr", namespaces=NS)] == [
        etree.tostring(node) for node in new.xpath(".//a:rPr|.//a:tcPr", namespaces=NS)
    ]
    with ZipFile(io.BytesIO(source)) as original, ZipFile(io.BytesIO(result.pptx)) as output:
        assert original.namelist() == output.namelist()
        assert output.comment == original.comment
        for name in original.namelist():
            if name != "ppt/slides/slide42.xml":
                assert original.read(name) == output.read(name)
    text = " ".join(new.xpath(".//a:t/text()", namespaces=NS))
    assert "Собрано 07.11.2026 в 06:30" in text
    assert "Счет за последние 3 постов" in text


@pytest.mark.parametrize(
    "roster,profile,status",
    [
        ("Кольцова Наталья Васильевна", "Наталия Кольцова", "typo"),
        ("Колындёнков Вадим Дмитриевич", "Вадим Колынденков", "exact"),
        ("Волкова Наталья Николаевна", "Волкова Наталья", "exact"),
        ("Кирюшин Роман Викторович", "Роман Кирюшни", "typo"),
        ("Гусев Виталий Витальевич", "Мария Гусева", "not_found"),
        ("Цой Владислав Витальевич", "Владислав Чой", "not_found"),
        ("Андреева Дарья Юрьевна", "Дарья Удачина", "not_found"),
        ("Кольцова Наталья Васильевна", "Наталия Кальцова", "not_found"),
        ("Волкова Наталья Николаевна", "Наталья Волкова Петровна", "not_found"),
    ],
)
def test_conservative_typo_rules(roster, profile, status):
    result = fill_presentation(template_bytes([roster]), report_for([(1, profile, 2)]))
    assert result.matches[0].status == status
    assert result.matches[0].full_name == roster


def test_same_names_never_select_profile_with_largest_count():
    result = fill_presentation(
        template_bytes(["Первов Герман Алексеевич"]),
        report_for([(1, "Герман Первов", 3), (2, "Герман Первов", 1)]),
    )
    match = result.matches[0]
    assert (match.status, match.value, match.user_id) == ("ambiguous", "н/д", None)
    assert "id1" in match.candidates and "id2" in match.candidates


def test_unknown_is_not_zero_and_explicit_no_profile_is_preserved():
    result = fill_presentation(
        template_bytes(
            ["Иванов Иван Иванович", "Лапина Екатерина Андреевна"], ["15", "нет профиля ВК"]
        ),
        report_for([]),
    )
    assert [(item.status, item.value) for item in result.matches] == [
        ("not_found", "н/д"),
        ("no_profile", "нет профиля ВК"),
    ]


def test_explicit_binding_resolves_ambiguity_and_can_establish_zero():
    result = fill_presentation(
        template_bytes(["Первов Герман Алексеевич", "Иванов Иван Иванович"]),
        report_for([(1, "Герман Первов", 3), (2, "Герман Первов", 1)]),
        bindings={"Первов Герман Алексеевич": 2, "Иванов Иван Иванович": 99},
    )
    assert [(m.status, m.value, m.user_id) for m in result.matches] == [
        ("bound", "1", 2),
        ("bound_zero", "0", 99),
    ]


def test_one_profile_cannot_silently_be_assigned_to_different_people():
    result = fill_presentation(
        template_bytes(["Иванов Иван Петрович", "Иванов Иван Сергеевич"]),
        report_for([(1, "Иван Иванов", 3)]),
    )
    assert all(item.status == "conflict" and item.value == "н/д" for item in result.matches)


def test_invalid_snapshot_and_file_rejected():
    with pytest.raises(ValueError, match="PPTX"):
        fill_presentation(b"not a zip", report_for([]))
    report = report_for([(1, "Иван Иванов", 3)])
    report["rows"][0]["likes"] = 2
    with pytest.raises(ValueError, match="расходятся"):
        fill_presentation(template_bytes(["Иванов Иван Иванович"]), report)
    with pytest.raises(ValueError, match="положительный"):
        fill_presentation(
            template_bytes(["Иванов Иван Иванович"]),
            report_for([]),
            bindings={"Иванов Иван Иванович": -1},
        )


def test_audit_keeps_original_and_current_counts_and_typo_warning():
    result = fill_presentation(
        template_bytes(["Кольцова Наталья Васильевна"]), report_for([(1, "Наталия Кольцова", 3)])
    )
    text = result.audit_csv().decode("utf-8-sig")
    assert "Возможная опечатка: проверить" in text
    assert "Отличается" in text
    assert ";15;3;" in text


@pytest.mark.asyncio
async def test_bot_upload_returns_filled_pptx_and_separate_audit():
    source = template_bytes(["Колындёнков Вадим Дмитриевич"])
    document = SimpleNamespace(file_name="template.pptx", file_size=len(source))
    settings = SimpleNamespace(get=AsyncMock(return_value=None))
    service = SimpleNamespace(
        vk_service=SimpleNamespace(
            settings=SimpleNamespace(timezone="Europe/Moscow"), runtime_settings=settings
        ),
        collect=AsyncMock(return_value=report_for([(1, "Вадим Колынденков", 3)])),
    )

    async def download(_document, *, destination):
        destination.write(source)

    message = SimpleNamespace(
        document=document,
        reply_to_message=None,
        bot=SimpleNamespace(download=AsyncMock(side_effect=download)),
        answer=AsyncMock(),
        answer_document=AsyncMock(),
    )
    await presentation_command(
        message, CommandObject(command="vk_likes_pptx", args="2026-10-12 2026-10-19"), service
    )
    assert service.collect.await_count == 1
    assert message.answer_document.await_count == 2
    pptx, audit = [call.args[0] for call in message.answer_document.await_args_list]
    assert pptx.filename.endswith(".pptx") and pptx.data.startswith(b"PK")
    assert audit.filename.endswith(".csv")
    assert "Колындёнков Вадим Дмитриевич" in audit.data.decode("utf-8-sig")


@pytest.mark.asyncio
async def test_bot_binding_is_persisted_and_correctable():
    import json

    settings = SimpleNamespace(get=AsyncMock(return_value="{}"), set=AsyncMock())
    service = SimpleNamespace(vk_service=SimpleNamespace(runtime_settings=settings))
    message = SimpleNamespace(answer=AsyncMock())
    await bind_profile_command(
        message,
        CommandObject(command="vk_likes_bind", args="123 | Колындёнков Вадим Дмитриевич"),
        service,
    )
    assert json.loads(settings.set.await_args.args[1]) == {"Колындёнков Вадим Дмитриевич": 123}
    settings.get.return_value = settings.set.await_args.args[1]
    await bind_profile_command(
        message,
        CommandObject(command="vk_likes_bind", args="0 | Колынденков Вадим Дмитриевич"),
        service,
    )
    assert json.loads(settings.set.await_args.args[1]) == {}


@pytest.mark.asyncio
async def test_invalid_upload_does_not_collect():
    service = SimpleNamespace(collect=AsyncMock())
    message = SimpleNamespace(document=None, reply_to_message=None, answer=AsyncMock())
    await presentation_command(message, CommandObject(command="vk_likes_pptx"), service)
    service.collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_dispatcher_routes_document_caption_and_enforces_admin_access(
    monkeypatch, tmp_path
):
    from datetime import datetime, timezone

    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, Message, Update

    from app.bot.factory import create_dispatcher
    from app.config import Settings
    from app.vk.workspace import VKLikesWorkspace

    class Session(BaseSession):
        def __init__(self):
            super().__init__()
            self.methods = []

        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            self.methods.append(method)
            return Message(
                message_id=len(self.methods),
                date=datetime.now(timezone.utc),  # noqa: UP017 - Python 3.10
                chat=Chat(id=42, type="private"),
            )

        async def stream_content(self, *args, **kwargs):
            yield b""

    source = template_bytes(["Колындёнков Вадим Дмитриевич"])

    async def download(self, document, *, destination, **kwargs):
        destination.write(source)

    monkeypatch.setattr(Bot, "download", download)
    values = {}

    async def set_value(key, value, **kwargs):
        values[key] = value

    runtime = SimpleNamespace(
        get=AsyncMock(side_effect=values.get), set=AsyncMock(side_effect=set_value)
    )
    report = report_for([(1, "Вадим Колынденков", 3)])
    report["label"] = "12–19 октября"
    service = SimpleNamespace(
        vk_service=SimpleNamespace(
            settings=SimpleNamespace(timezone="Europe/Moscow", database_path=tmp_path / "argus.db"),
            runtime_settings=runtime,
        ),
        collect=AsyncMock(return_value=report),
        latest=AsyncMock(return_value=report),
    )
    workspace = VKLikesWorkspace(service)
    settings = Settings(_env_file=None, bot_token="123456:TEST_ONLY", ADMIN_IDS="42")
    dp = create_dispatcher(settings, vk_likes_service=service, vk_likes_workspace=workspace)
    session = Session()
    bot = Bot(token="123456:TEST_ONLY", session=session)
    raw_message = {
        "message_id": 1,
        "date": 1794014400,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
        "caption": "/vk_likes_pptx 2026-10-12 2026-10-19",
        "document": {
            "file_id": "file",
            "file_unique_id": "unique",
            "file_name": "template.pptx",
            "file_size": len(source),
        },
    }
    await dp.feed_update(bot, Update(update_id=1, message=Message.model_validate(raw_message)))
    assert service.collect.await_count == 1
    assert (
        len([method for method in session.methods if method.__api_method__ == "sendDocument"]) == 2
    )
    # The exact same upload from a non-admin must never trigger collection.
    raw_message["from"]["id"] = 43
    await dp.feed_update(bot, Update(update_id=2, message=Message.model_validate(raw_message)))
    assert service.collect.await_count == 1
    assert "Нет доступа" in session.methods[-1].text

    # Exercise button callbacks and stateful uploads through the same real dispatcher.
    sequence = 10

    async def click(data):
        nonlocal sequence
        sequence += 1
        update = Update.model_validate(
            {
                "update_id": sequence,
                "callback_query": {
                    "id": str(sequence),
                    "chat_instance": "preview",
                    "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
                    "message": {
                        "message_id": 9,
                        "date": 1794014400,
                        "chat": {"id": 42, "type": "private"},
                        "text": "Menu",
                    },
                    "data": data,
                },
            }
        )
        await dp.feed_update(bot, update)

    await click("vkl:menu")
    await click("vklui:upload")
    raw_message["from"]["id"] = 42
    raw_message.pop("caption")
    await dp.feed_update(bot, Update(update_id=20, message=Message.model_validate(raw_message)))
    assert await workspace.template() == source
    assert values["vk_likes_template_name"] == "template.pptx"
    before = service.collect.await_count
    await click("vklui:saved")
    assert service.collect.await_count == before  # Saved PPTX never queries VK again.
    await click("vklui:period:pptx")
    await click("vklui:run:pptx:7d")
    assert service.collect.await_count == before + 1
    await click("vklui:custom:pptx")
    text_message = {key: value for key, value in raw_message.items() if key != "document"}
    text_message["text"] = "bad dates"
    await dp.feed_update(bot, Update(update_id=30, message=Message.model_validate(text_message)))
    assert "Проверьте даты" in session.methods[-1].text
    text_message["text"] = "12.10.2026 19.10.2026"
    await dp.feed_update(bot, Update(update_id=31, message=Message.model_validate(text_message)))
    assert service.collect.await_count == before + 2
    await click("vklui:bind")
    text_message["text"] = "Цой Владислав Витальевич | https://vk.ru/id572058492"
    await dp.feed_update(bot, Update(update_id=32, message=Message.model_validate(text_message)))
    assert (await workspace.bindings())["Цой Владислав Витальевич"] == 572058492
    await click("vklui:bindings")
    assert "572058492" in session.methods[-1].text
    await click("vklui:custom:pptx")
    await click("vkl:menu")  # Cancel must discard the pending input state.
    state = dp.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    assert await state.get_state() is None
    await dp.storage.close()
    await bot.session.close()
