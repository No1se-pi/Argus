import asyncio
import csv
import io
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.vk_likes import collect_and_send, latest_command
from app.storage.database import Database
from app.storage.schema import init_schema
from app.vk.client import VKAPIError, VKClient
from app.vk.likes import VKLikesService, parse_selection, parse_vk_link, report_csv, report_text
from app.vk.likes_repository import VKLikesRepository

GROUP = 173208026


@pytest.mark.asyncio
async def test_collect_can_use_service_key_without_user_token(collection, monkeypatch):
    from pydantic import SecretStr

    service, client, config = collection
    config.user_access_token = None
    service.vk_service.settings.vk_service_access_token = SecretStr("service-key")
    received = []

    def factory(**kwargs):
        received.append(kwargs["access_token"])
        return client

    monkeypatch.setattr("app.vk.likes.VKClient", factory)
    await service.collect(parse_selection("last 1", "Europe/Moscow"))
    assert received == ["service-key"]


def post(post_id, date="2026-10-15T12:00:00+00:00", **extra):
    return {
        "id": post_id,
        "owner_id": -GROUP,
        "date": int(datetime.fromisoformat(date).timestamp()),
        **extra,
    }


@pytest.fixture
async def collection(tmp_path, monkeypatch):
    db = Database(tmp_path / "argus.sqlite3")
    await db.connect()
    await init_schema(db)
    # Exercise the startup migration twice against the same database.
    await init_schema(db)
    repository = VKLikesRepository(db)
    config = SimpleNamespace(enabled=True, group_id=GROUP, user_access_token="test-only")
    vk = SimpleNamespace(
        effective_config=AsyncMock(return_value=config),
        settings=SimpleNamespace(vk_api_version="5.199", timezone="Europe/Moscow"),
    )
    service = VKLikesService(vk, repository)
    client = SimpleNamespace(
        get_wall_page=AsyncMock(return_value={"count": 2, "items": [post(2), post(1)]}),
        get_likes_page=AsyncMock(
            side_effect=[
                {"count": 2, "items": [10, 20]},
                {"count": 2, "items": [20, 30]},
            ]
        ),
        get_users=AsyncMock(
            return_value=[
                {"id": 10, "first_name": "Иван", "last_name": "Иванов"},
                {"id": 20, "first_name": "Анна", "last_name": "Петрова"},
            ]
        ),
        get_groups=AsyncMock(return_value=[{"id": 42, "name": "Колледж"}]),
        get_wall_post=AsyncMock(return_value=post(7730)),
        resolve_group_id=AsyncMock(return_value=GROUP),
    )
    monkeypatch.setattr("app.vk.likes.VKClient", lambda **kwargs: client)

    async def request(callback, *args, **kwargs):
        return await callback(*args, **kwargs)

    monkeypatch.setattr(service, "_request", request)
    yield service, client, config
    await db.close()


@pytest.mark.asyncio
async def test_discovers_people_counts_per_post_and_persists(collection):
    service, client, _ = collection
    report = await service.collect(parse_selection("2026-10-12 2026-10-19", "Europe/Moscow"))
    assert [(r["user_id"], r["likes"]) for r in report["rows"]] == [(20, 2), (30, 1), (10, 1)]
    assert report["rows"][0]["full_name"] == "Анна Петрова"
    assert report["rows"][1]["full_name"] == "id30"  # Missing/deleted names keep the ID.
    assert report["posts_count"] == 2
    assert await service.latest() == report
    client.get_users.assert_awaited_once_with([10, 20, 30])


@pytest.mark.asyncio
async def test_rerun_replaces_current_counts_and_handles_removed_likes(collection):
    service, client, _ = collection
    selection = parse_selection("last 2", "Europe/Moscow")
    first = await service.collect(selection)
    client.get_likes_page.side_effect = None
    client.get_likes_page.return_value = {"count": 1, "items": [10]}
    second = await service.collect(selection)
    assert first["rows"][0]["user_id"] == 20
    assert [(r["user_id"], r["likes"]) for r in second["rows"]] == [(10, 2)]
    assert await service.latest() == second


@pytest.mark.asyncio
async def test_failed_post_does_not_publish_partial_report(collection):
    service, client, _ = collection
    selection = parse_selection("last 2", "Europe/Moscow")
    previous = await service.collect(selection)
    client.get_likes_page.side_effect = [{"count": 1, "items": [10]}, VKAPIError("Denied")]
    with pytest.raises(VKAPIError):
        await service.collect(selection)
    assert await service.latest() == previous
    assert not service._lock.locked()


@pytest.mark.asyncio
async def test_more_than_1000_likes(collection):
    service, client, _ = collection
    client.get_likes_page.side_effect = [
        {"count": 1003, "items": list(range(1, 1001))},
        {"count": 1003, "items": [1001, 1002, 1003]},
    ]
    assert await service._likers(client, GROUP, 1) == set(range(1, 1004))
    assert client.get_likes_page.await_args_list[1].kwargs["offset"] == 1000


@pytest.mark.parametrize(
    "pages",
    [
        [{"count": 3, "items": [1, 2]}, {"count": 3, "items": []}],
        [{"count": 3, "items": [1, 2]}, {"count": 3, "items": [2]}],
        [{"count": 1, "items": ["10"]}],
        [{"count": 1, "items": [0]}],
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_invalid_likes_are_rejected(collection, pages):
    service, client, _ = collection
    client.get_likes_page.side_effect = pages
    with pytest.raises(VKAPIError):
        await service._likers(client, GROUP, 1)
    assert await service.latest() is None


@pytest.mark.asyncio
async def test_wall_pagination_and_old_pinned_post(collection):
    service, client, _ = collection
    client.get_wall_page.side_effect = [
        {
            "count": 4,
            "items": [
                post(99, "2025-01-01T00:00:00+00:00", is_pinned=1),
                post(3, "2026-10-16T00:00:00+00:00"),
            ],
        },
        {"count": 4, "items": [post(2), post(1, "2026-10-01T00:00:00+00:00")]},
    ]
    selection = parse_selection("2026-10-12 2026-10-19", "Europe/Moscow")
    assert [p["id"] for p in await service._posts(client, GROUP, selection)] == [3, 2]
    assert client.get_wall_page.await_args_list[1].kwargs["offset"] == 2


@pytest.mark.asyncio
async def test_last_n_excludes_old_pinned_post(collection):
    service, client, _ = collection
    client.get_wall_page.side_effect = [
        {"count": 5, "items": [post(99, "2025-01-01T00:00:00+00:00", is_pinned=1), post(3)]},
        {"count": 5, "items": [post(2, "2026-10-14T00:00:00+00:00")]},
    ]
    result = await service._posts(client, GROUP, parse_selection("last 2", "Europe/Moscow"))
    assert [p["id"] for p in result] == [3, 2]


@pytest.mark.asyncio
async def test_calendar_dates_include_end_date_and_exclude_next_midnight(collection):
    service, client, _ = collection
    client.get_wall_page.return_value = {
        "count": 4,
        "items": [
            post(4, "2026-10-19T21:00:00+00:00"),  # Oct 20, 00:00 Moscow
            post(3, "2026-10-19T20:59:59+00:00"),
            post(2, "2026-10-11T21:00:00+00:00"),
            post(1, "2026-10-11T20:59:59+00:00"),
        ],
    }
    selection = parse_selection("2026-10-12 2026-10-19", "Europe/Moscow")
    assert [p["id"] for p in await service._posts(client, GROUP, selection)] == [3, 2]


@pytest.mark.asyncio
async def test_missing_wall_page_is_not_empty_success(collection):
    service, client, _ = collection
    client.get_wall_page.return_value = {"count": 100, "items": []}
    with pytest.raises(VKAPIError):
        await service.collect(parse_selection("last 17", "Europe/Moscow"))


@pytest.mark.asyncio
async def test_empty_period_and_no_likes(collection):
    service, client, _ = collection
    client.get_wall_page.return_value = {"count": 0, "items": []}
    report = await service.collect(parse_selection("last 17", "Europe/Moscow"))
    assert report["posts_count"] == 0
    assert "постов нет" in report_text(report, "Europe/Moscow")
    client.get_likes_page.assert_not_awaited()
    client.get_users.assert_not_awaited()
    client.get_wall_page.return_value = {"count": 1, "items": [post(1)]}
    client.get_likes_page.side_effect = None
    client.get_likes_page.return_value = {"count": 0, "items": []}
    report = await service.collect(parse_selection("last 1", "Europe/Moscow"))
    assert report["rows"] == []
    assert "лайков нет" in report_text(report, "Europe/Moscow")


@pytest.mark.asyncio
async def test_disabled_and_missing_token_make_no_requests(collection):
    service, client, config = collection
    config.enabled = False
    with pytest.raises(ValueError, match="выключен"):
        await service.collect(parse_selection("7d", "Europe/Moscow"))
    config.enabled = True
    config.user_access_token = None
    with pytest.raises(ValueError, match="VK_USER_ACCESS_TOKEN"):
        await service.collect(parse_selection("7d", "Europe/Moscow"))
    client.get_wall_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_collection_rejected_and_cancellation_releases_lock(collection):
    service, client, _ = collection
    waiting = asyncio.Event()

    async def wait_for_cancel(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()

    client.get_wall_page.side_effect = wait_for_cancel
    task = asyncio.create_task(service.collect(parse_selection("7d", "Europe/Moscow")))
    await waiting.wait()
    with pytest.raises(ValueError, match="уже выполняется"):
        await service.collect(parse_selection("7d", "Europe/Moscow"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._lock.locked()
    assert await service.latest() is None


@pytest.mark.parametrize(
    "url",
    [
        "https://vk.ru/kait_20_official?w=reactions-173208026_7730%3FtrackCode%3Dabc%26type%3Dpost",
        "https://vk.com/wall-173208026_7730",
        "https://m.vk.ru/kait_20_official?w=wall-173208026_7730",
    ],
)
def test_user_reaction_and_post_links(url):
    assert parse_vk_link(url) == (str(GROUP), 7730)
    selection = parse_selection(url, "Europe/Moscow")
    assert selection.post_id == 7730
    assert selection.group_reference == str(GROUP)


@pytest.mark.asyncio
async def test_collect_single_post_link_and_community_link(collection):
    service, client, config = collection
    config.group_id = None
    report = await service.collect(
        parse_selection("https://vk.ru/wall-173208026_7730", "Europe/Moscow")
    )
    assert report["posts_count"] == 1
    assert report["group_id"] == GROUP
    client.get_wall_page.assert_not_awaited()
    client.get_wall_post.assert_awaited_once_with(GROUP, 7730)
    client.get_likes_page.side_effect = None
    client.get_likes_page.return_value = {"count": 0, "items": []}
    await service.collect(parse_selection("https://vk.ru/kait_20_official last 2", "Europe/Moscow"))
    client.resolve_group_id.assert_awaited_once_with("kait_20_official")


@pytest.mark.parametrize(
    "args",
    [
        "last 0",
        "last 1001",
        "0d",
        "-1d",
        "100000000000000d",
        "2026-10-19 2026-10-12",
        "https://evil.test/club1",
    ],
)
def test_invalid_selection(args):
    with pytest.raises((ValueError, OverflowError)):
        parse_selection(args, "Europe/Moscow")


@pytest.mark.asyncio
async def test_exports_escape_names_and_include_proof_links(collection):
    service, client, _ = collection
    client.get_users.return_value = [{"id": 20, "first_name": "=1+1<&>", "last_name": ""}]
    report = await service.collect(parse_selection("last 2", "Europe/Moscow"))
    text = report_text(report, "Europe/Moscow")
    assert "=1+1&lt;&amp;&gt;" in text
    rows = list(csv.reader(io.StringIO(report_csv(report).decode("utf-8-sig")), delimiter=";"))
    assert rows[1][0] == "'=1+1<&>"
    assert rows[1][3:5] == ["2", "2"]
    assert "wall-173208026_2" in rows[1][-1]
    assert "wall-173208026_1" in rows[1][-1]
    report["rows"] *= 100
    for row in report["rows"]:
        row["full_name"] = "&" * 100
    assert len(report_text(report, "Europe/Moscow")) < 4096


@pytest.mark.asyncio
async def test_vk_client_parameters_and_schema_validation():
    client = VKClient(access_token="test-only", api_version="5.199")
    client.request = AsyncMock(return_value={"count": 1, "items": [10]})
    await client.get_likes_page(GROUP, 7730, offset=1000)
    method, params = client.request.await_args.args
    assert method == "likes.getList"
    assert params["owner_id"] == -GROUP
    assert params["item_id"] == 7730
    assert params["offset"] == 1000
    assert params["count"] == 1000
    assert params["friends_only"] == 0
    assert params["skip_own"] == 0
    client.request.return_value = {"items": []}
    with pytest.raises(VKAPIError):
        await client.get_wall_page(GROUP)


@pytest.mark.asyncio
async def test_bot_sends_csv_and_reopens_saved_report(collection):
    service, _, _ = collection
    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock())
    await collect_and_send(message, "last 2", service)
    assert message.answer_document.await_count == 1
    assert "Анна Петрова" in message.answer.await_args.args[0]
    await latest_command(message, service)
    assert message.answer_document.await_count == 2


@pytest.mark.asyncio
async def test_likes_from_community_have_distinct_id_and_link(collection):
    service, client, _ = collection
    client.get_likes_page.side_effect = None
    client.get_likes_page.return_value = {"count": 2, "items": [10, -42]}
    report = await service.collect(parse_selection("last 1", "Europe/Moscow"))
    row = next(row for row in report["rows"] if row["user_id"] == -42)
    assert row["full_name"] == "Колледж"
    assert row["url"] == "https://vk.com/club42"
    assert row["likes"] == 1
    client.get_users.assert_awaited_once_with([10])
    client.get_groups.assert_awaited_once_with([-42])


@pytest.mark.asyncio
async def test_names_are_paginated(collection):
    service, client, _ = collection
    await service._names(client, list(range(1, 1004)))
    assert client.get_users.await_count == 2
    assert len(client.get_users.await_args_list[0].args[0]) == 1000
    assert client.get_users.await_args_list[1].args[0] == [1001, 1002, 1003]


@pytest.mark.asyncio
async def test_rate_limit_retries_are_bounded(collection, monkeypatch):
    service, _, _ = collection
    sleep = AsyncMock()
    monkeypatch.setattr("app.vk.likes.asyncio.sleep", sleep)
    callback = AsyncMock(side_effect=[VKAPIError("Rate", code=6), {"ok": True}])
    # Use the actual throttle/retry method, bypassed by the collection fixture.
    result = await VKLikesService._request(service, callback, "argument")
    assert result == {"ok": True}
    assert sleep.await_count == 2
    callback.side_effect = VKAPIError("Rate", code=6)
    callback.reset_mock()
    with pytest.raises(VKAPIError):
        await VKLikesService._request(service, callback)
    assert callback.await_count == 3


@pytest.mark.asyncio
async def test_bot_error_keeps_partial_data_out_of_chat(collection):
    service, client, _ = collection
    client.get_likes_page.side_effect = VKAPIError("Token not printed", code=5)
    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock())
    await collect_and_send(message, "last 2", service)
    message.answer_document.assert_not_awaited()
    assert "код 5" in message.answer.await_args.args[0]
    assert "Token not printed" not in message.answer.await_args.args[0]
