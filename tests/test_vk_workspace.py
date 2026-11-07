import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.vk_likes_keyboards import likes_keyboard, period_keyboard
from app.bot.vk_likes_ui import calendar_period, profile_id
from app.storage.database import Database
from app.storage.repositories import RuntimeSettingsRepository
from app.storage.schema import init_schema
from app.vk.workspace import INITIAL_BINDINGS, VKLikesWorkspace
from tests.test_vk_presentation import template_bytes


@pytest.fixture
async def workspace(tmp_path):
    database = Database(tmp_path / "argus.sqlite3")
    await database.connect()
    await init_schema(database)
    runtime = RuntimeSettingsRepository(database)
    service = SimpleNamespace(
        vk_service=SimpleNamespace(
            runtime_settings=runtime,
            settings=SimpleNamespace(
                database_path=database.path, timezone="Europe/Moscow", vk_api_version="5.199"
            ),
        )
    )
    result = VKLikesWorkspace(service)
    yield result
    await database.close()


@pytest.mark.asyncio
async def test_confirmed_defaults_seed_once_and_do_not_undo_user_changes(workspace):
    await workspace.seed_bindings()
    assert await workspace.bindings() == INITIAL_BINDINGS
    await workspace.settings.set("vk_likes_name_bindings", "{}")
    await workspace.seed_bindings()
    assert await workspace.bindings() == {}


@pytest.mark.asyncio
async def test_seed_preserves_existing_explicit_binding(workspace):
    name = "Цой Владислав Витальевич"
    await workspace.settings.set("vk_likes_name_bindings", json.dumps({name: 123}))
    await workspace.seed_bindings()
    assert (await workspace.bindings())[name] == 123
    assert len(await workspace.bindings()) == 3


@pytest.mark.asyncio
async def test_template_persists_and_invalid_replacement_keeps_working_copy(workspace):
    assert await workspace.template() is None
    source = template_bytes(["Цой Владислав Витальевич"])
    await workspace.save_template(source, "source.pptx")
    assert await workspace.template() == source
    assert await workspace.settings.get("vk_likes_template_name") == "source.pptx"
    restored = VKLikesWorkspace(workspace.service)
    assert await restored.template() == source
    with pytest.raises(ValueError):
        await workspace.save_template(b"not a presentation", "broken.pptx")
    assert await workspace.template() == source
    assert await workspace.settings.get("vk_likes_template_name") == "source.pptx"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("12.10.2026 19.10.2026", "2026-10-12 2026-10-19"),
        ("2026-10-12 2026-10-19", "2026-10-12 2026-10-19"),
    ],
)
def test_calendar_entry(text, expected):
    assert calendar_period(text) == expected


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("572058492", 572058492),
        ("https://vk.ru/id1076022785", 1076022785),
        ("vk.com/id374396012", 374396012),
        ("0", 0),
    ],
)
@pytest.mark.asyncio
async def test_numeric_profile_links_need_no_vk_call(workspace, reference, expected):
    assert await profile_id(reference, workspace) == expected


@pytest.mark.asyncio
async def test_nickname_resolves_to_numeric_id(workspace, monkeypatch):
    workspace.service.vk_service.effective_config = AsyncMock(
        return_value=SimpleNamespace(user_access_token="test_only")
    )
    client = SimpleNamespace(request=AsyncMock(return_value=[{"id": 572058492}]))
    monkeypatch.setattr("app.bot.vk_likes_ui.VKClient", lambda **kwargs: client)
    assert await profile_id("https://vk.ru/ghostsoy", workspace) == 572058492
    client.request.assert_awaited_once_with("users.get", {"user_ids": "ghostsoy"})
    with pytest.raises(ValueError):
        await profile_id("https://evil.test/ghostsoy", workspace)


def test_buttons_cover_core_flows_and_callback_lengths():
    keyboard = likes_keyboard()
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert {
        "vklui:period:table",
        "vklui:period:pptx",
        "vkl:report",
        "vklui:saved",
        "vklui:template",
        "vklui:bindings",
    } <= callbacks
    for board in [keyboard, period_keyboard("table"), period_keyboard("pptx")]:
        assert all(
            len(button.callback_data.encode()) <= 64
            for row in board.inline_keyboard
            for button in row
        )
