from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.reports.data import Period
from app.reports.service import ActivityReportService
from app.vk.client import VKAPIError


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    None, VKAPIError("Unauthorized", code=5, method="wall.get"),
    ValueError("VK is disabled"), RuntimeError("Transport failed"),
])
async def test_full_report_collects_exact_period_and_never_uses_stale_likes(monkeypatch, failure):
    import app.reports.service as module

    fresh = {"fresh": True}
    collector = AsyncMock(return_value=fresh, side_effect=failure)
    workspace = SimpleNamespace(template=AsyncMock(return_value=b"template"),
                                service=SimpleNamespace(collect=collector))
    service = ActivityReportService(SimpleNamespace(path=Path("unused.sqlite3")), workspace)
    monkeypatch.setattr(module, "read_snapshot", lambda *args: {
        "sources": [], "warnings": [], "vk": {"stale": True},
    })
    service.generate_snapshot = AsyncMock(side_effect=lambda template, snapshot: snapshot)
    period = Period.parse("09.11.2026 16.11.2026")
    result = await service.generate(period, collect_vk=True)
    collector.assert_awaited_once()
    selection = collector.await_args.args[0]
    assert selection.start == period.start and selection.end == period.end
    if failure:
        assert result["vk"] is None and result["vk_collection_error"]
        assert result["warnings"]
    else:
        assert result["vk"] == fresh and result["vk_collection_error"] is None


@pytest.mark.asyncio
async def test_offline_export_does_not_trigger_live_vk_requests(monkeypatch):
    import app.reports.service as module

    workspace = SimpleNamespace(template=AsyncMock(return_value=b"template"),
                                service=SimpleNamespace(collect=AsyncMock()))
    service = ActivityReportService(SimpleNamespace(path=Path("unused.sqlite3")), workspace)
    monkeypatch.setattr(module, "read_snapshot", lambda *args: {"sources": [], "warnings": []})
    service.generate_snapshot = AsyncMock(return_value="exported")
    assert await service.generate(Period.parse("09.11.2026 16.11.2026")) == "exported"
    workspace.service.collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_listing_excludes_disappeared_cached_reviews(monkeypatch):
    import app.reports.service as module

    workspace = SimpleNamespace(template=AsyncMock(return_value=b"template"))
    service = ActivityReportService(SimpleNamespace(path=Path("unused.sqlite3")), workspace)
    service.history = SimpleNamespace(ensure=AsyncMock(return_value=[]), observed={1: {"seen"}})
    monkeypatch.setattr(module, "read_snapshot", lambda *args: {
        "sources": [{"id": 1, "coverage_complete": True},
                    {"id": 2, "coverage_complete": False}],
        "warnings": [],
        "reviews": [{"source_id": 1, "external_review_id": "seen"},
                    {"source_id": 1, "external_review_id": "removed"},
                    {"source_id": 2, "external_review_id": "unknown"}],
    })
    service.generate_snapshot = AsyncMock(side_effect=lambda template, snapshot: snapshot)
    result = await service.generate(Period.parse("09.11.2026 16.11.2026"))
    assert [item["external_review_id"] for item in result["reviews"]] == ["seen", "unknown"]
    assert result["excluded_cached_reviews"] == 1
