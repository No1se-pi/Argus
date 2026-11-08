import io
from datetime import datetime
from zipfile import ZipFile

import pytest
from lxml import etree

from app.reports.data import Period, select_vk_report
from app.reports.identities import suggestions
from app.reports.pptx import CT, REL, A, P, R, Slide, TemplateDeck
from app.reports.render import RenderedPage, review_card
from app.reviews.client_2gis import DGisReviewsClient
from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import ReviewPlatform, ReviewSource
from tests.test_vk_presentation import template_bytes


def test_period_includes_both_moscow_calendar_days():
    period = Period.parse("12.10.2026 19.10.2026")
    assert period.start.isoformat() == "2026-10-11T21:00:00+00:00"
    assert period.end.isoformat() == "2026-10-19T21:00:00+00:00"


def test_full_activity_bot_permissions_generation_retry_and_cancel(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("activity_dispatcher_probe.py")),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("value", ["bad", "19.10.2026 12.10.2026", "01.01.2024 01.01.2026"])
def test_invalid_period(value):
    with pytest.raises(ValueError):
        Period.parse(value)


def test_saved_vk_collection_is_recounted_by_posts_and_inclusive_boundaries():
    period = Period.parse("12.10.2026 19.10.2026")

    def post(identity, date, users):
        return {
            "id": identity,
            "date": int(datetime.fromisoformat(date).timestamp()),
            "user_ids": users,
        }

    parent = {
        "period_start": "2026-10-01T00:00:00+00:00",
        "period_end_exclusive": "2026-11-01T00:00:00+00:00",
        "posts": [
            post(1, "2026-10-11T20:59:59+00:00", [1]),
            post(2, "2026-10-11T21:00:00+00:00", [1, 1]),
            post(3, "2026-10-19T20:59:59+00:00", [2]),
            post(4, "2026-10-19T21:00:00+00:00", [1]),
        ],
        "rows": [
            {"user_id": 1, "full_name": "One", "likes": 3},
            {"user_id": 2, "full_name": "Two", "likes": 1},
        ],
    }
    result, warnings = select_vk_report([parent], period)
    assert [p["id"] for p in result["posts"]] == [2, 3]
    assert {r["user_id"]: r["likes"] for r in result["rows"]} == {1: 1, 2: 1}
    assert not warnings
    parent["period_start"] = "2026-10-13T00:00:00+00:00"
    assert select_vk_report([parent], period)[0] is None


def test_html_preserves_stars_and_escapes_untrusted_text():
    card = review_card(
        {
            "author_name": "<Admin>",
            "text": "<script>alert(1)</script>\nSecond line",
            "rating": 2,
            "published_at": "2026-10-11T22:00:00Z",
        },
        "Europe/Moscow",
    )
    assert "<script>" not in card and "&lt;script&gt;" in card
    assert "★★<span class='empty'>★★★</span>" in card
    assert "12.10.2026" in card
    assert "★" not in review_card({"rating": None}, "Europe/Moscow")


def test_transliteration_is_suggestion_not_identity_binding():
    rows = [{"user_id": 1, "full_name": "Miss Eivazova", "likes": 1}]
    result = suggestions("Эйвазова Бадам Рамиз кызы", rows)
    assert result[0]["user_id"] == 1
    assert result[0]["reason"] and "score" in result[0]
    assert not suggestions(
        "Оголенкова Анастасия Сергеевна", [{"user_id": 2, "full_name": "Улитка Огонькова"}]
    )


def test_rating_metadata_requires_matching_business_and_valid_rating():
    client = YandexReviewsClient()
    client._read_rating({"id": "123", "ratingData": {"ratingValue": 4.7, "reviewCount": 20}}, "123")
    assert client.last_rating == 4.7 and client.last_total_count == 20
    client._read_rating({"id": "999", "ratingData": {"ratingValue": 5}}, "123")
    client._read_rating({"@type": ["Organization"]}, "123")
    assert client.last_rating == 4.7


@pytest.mark.asyncio
async def test_2gis_uses_server_cursor_instead_of_repeating_first_page():
    import urllib.parse

    recorded = []
    date = "2026-10-30T10:00:00+07:00"
    responses = [
        {
            "reviews": [
                {"id": "1", "rating": 5, "text": "One", "date_created": date, "user": {"name": "A"}}
            ],
            "meta": {
                "next_link": "https://public-api.reviews.2gis.com/3.0/branches/123/reviews?offset_date="
                + urllib.parse.quote(date, safe="")
            },
        },
        {
            "reviews": [
                {
                    "id": "2",
                    "rating": 4,
                    "text": "Two",
                    "date_created": "2026-10-29T10:00:00+07:00",
                    "user": {"name": "B"},
                }
            ],
            "meta": {},
        },
    ]

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return responses.pop(0)

    class Session:
        closed = False

        def get(self, url, **kwargs):
            recorded.append(url)
            return Response()

    source = ReviewSource(
        1, ReviewPlatform.DGIS, "Branch", "123", "https://2gis.ru/moscow/firm/123"
    )
    client = DGisReviewsClient(session=Session())
    first = await client.fetch_reviews(source, page=1, page_size=1)
    second = await client.fetch_reviews(source, page=2, page_size=1)
    assert first[1][0].external_review_id != second[1][0].external_review_id
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(recorded[1]).query)
    assert query["offset_date"] == [date]
    assert "page" not in query and not client.has_next_page(source, 1, 2)


def test_rebuilt_deck_removes_obsolete_images_notes_and_slides():
    with ZipFile(io.BytesIO(template_bytes(["Иванов Иван Иванович"]))) as source:
        parts = {item: source.read(item) for item in source.namelist()}
    presentation = etree.fromstring(parts["ppt/presentation.xml"])
    etree.SubElement(presentation, f"{{{P}}}sldSz", cx="9144000", cy="5143500")
    parts["ppt/presentation.xml"] = etree.tostring(presentation)
    parts["[Content_Types].xml"] = f'<Types xmlns="{CT}"/>'.encode()
    parts["_rels/.rels"] = (
        f'<Relationships xmlns="{REL}"><Relationship Id="p" Type="{R}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'.encode()
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        f'<Relationships xmlns="{REL}"><Relationship Id="slideA" Type="{R}/slide" Target="slides/slide42.xml"/><Relationship Id="slideB" Type="{R}/slide" Target="slides/slide1.xml"/></Relationships>'.encode()
    )
    parts[
        "ppt/slides/slide1.xml"
    ] = f'''<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree>
      <p:nvGrpSpPr/><p:grpSpPr/><p:sp><a:t>Скриншоты</a:t></p:sp>
      <p:pic><a:blip r:embed="oldImage"/></p:pic></p:spTree></p:cSld></p:sld>'''.encode()
    parts["ppt/slides/_rels/slide1.xml.rels"] = (
        f'<Relationships xmlns="{REL}"><Relationship Id="oldImage" Type="{R}/image" Target="../media/image1.png"/></Relationships>'.encode()
    )
    stream = io.BytesIO()
    with ZipFile(stream, "w") as target:
        for name, data in parts.items():
            target.writestr(name, data)
    snapshot = {
        "vk": None,
        "captured_at": "2026-11-08T00:00:00+00:00",
        "bindings": {},
        "period": "12.10.2026–19.10.2026",
    }
    deck = TemplateDeck(stream.getvalue(), snapshot, "Europe/Moscow")
    result = deck.build([Slide("ppt/slides/slide1.xml", image=RenderedPage(b"new image", []))])
    with ZipFile(io.BytesIO(result)) as output:
        assert "ppt/media/image1.png" not in output.namelist()
        assert "ppt/slides/slide42.xml" not in output.namelist()
        assert output.read("ppt/media/argus1.png") == b"new image"
        assert b"oldImage" not in output.read("ppt/slides/argus1.xml")


@pytest.mark.parametrize(
    "fraction,expected", [("9", 900000), ("98", 980000), ("98452", 984520), ("9845207", 984520)]
)
def test_api_fractional_dates_work_on_python310(fraction, expected):
    from app.time import parse_iso_datetime

    value = parse_iso_datetime(f"2026-10-31T19:58:42.{fraction}+07:00")
    assert value.microsecond == expected


@pytest.mark.asyncio
async def test_repeated_history_page_is_not_marked_complete_or_alerted(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.reports.history as history
    from app.reports.history import HistoricalReviewsLoader
    from app.reviews.models import ReviewItem, ReviewSyncStatus

    monkeypatch.setattr(history.asyncio, "sleep", AsyncMock())
    repo = SimpleNamespace(save_reviews=AsyncMock())
    service = SimpleNamespace(
        repository=repo,
        notify_since=None,
        settings=SimpleNamespace(reviews_request_pause_seconds=0),
    )
    loader = HistoricalReviewsLoader(service)
    items = [
        ReviewItem(
            str(i), ReviewPlatform.YANDEX, "Branch", "Test", 5, "Text", "2026-11-01T00:00:00Z"
        )
        for i in range(50)
    ]
    client = SimpleNamespace(
        fetch_reviews=AsyncMock(
            return_value=(ReviewSyncStatus.SUCCESS_NEW_REVIEWS, items, None, 200)
        ),
        last_rating=None,
    )
    source = ReviewSource(
        1, ReviewPlatform.YANDEX, "Branch", "123", "https://yandex.ru/maps/org/123"
    )
    assert not await loader._load(client, source, Period.parse("12.10.2026 19.10.2026"))
    assert client.fetch_reviews.await_count == 2
    calls = repo.save_reviews.await_args_list
    assert len(calls[0].args[0]) == 50 and calls[0].kwargs["mark_sent"] is True
    assert not calls[1].args[0]  # No old review is placed in the live notification queue.
