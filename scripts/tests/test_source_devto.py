from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest
import respx

from fetching import FetchConfig, new_client
from sources import devto
from validation import validate_dataset_items

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "devto"
CONFIG = FetchConfig(max_attempts=1, base_delay=0, max_delay=0)


def load_fixture():
    return json.loads((FIXTURES / "articles.json").read_text(encoding="utf-8"))


def article(article_id, **overrides):
    record = {
        "id": article_id,
        "title": f"Article {article_id}",
        "description": "<p>Example summary</p>",
        "url": f"https://dev.to/example/{article_id}",
        "canonical_url": f"https://dev.to/example/{article_id}",
        "published_timestamp": "2026-01-01T00:00:00Z",
        "published_at": "2026-01-01T00:00:00Z",
        "positive_reactions_count": 5,
        "comments_count": 2,
        "tag_list": ["python"],
        "user": {"name": "Example Author", "username": "example"},
        "cover_image": None,
        "social_image": None,
    }
    record.update(overrides)
    return record


def mock_tags(router, responses):
    for tag in devto.TAGS:
        response = responses.get(tag, [])
        if isinstance(response, httpx.Response):
            route_response = response
        else:
            route_response = httpx.Response(200, json=response)
        router.get(
            f"{devto.BASE}?tag={tag}&top=2&per_page=30"
        ).mock(return_value=route_response)


async def run_fetch(**options):
    async with new_client(CONFIG) as client:
        return await devto.fetch(client, CONFIG, START, **options)


@pytest.mark.asyncio
async def test_synthetic_fixture_maps_and_validates_with_no_rejections():
    with respx.mock() as router:
        mock_tags(router, {"python": load_fixture()})
        result = await run_fetch()

    report = validate_dataset_items("news", result.records)
    assert result.status == "success"
    assert report.rejected == []
    assert report.dropped_optional == []
    assert report.accepted == result.records
    first, second = result.records
    assert first["title"] == "Synthetic Python article"
    assert first["summary"] == "A sample Python article."
    assert first["upvotes"] == 0
    assert first["commentsCount"] == 2
    assert first["thumbnailUrl"] == "https://dev.to/assets/example-cover.png"
    assert second["communityOrAuthor"] == "anotherauthor"
    assert second["summary"] is None
    assert second["upvotes"] is None and second["commentsCount"] is None
    assert "thumbnailUrl" not in second
    assert all(record["source"] == "Dev.to" for record in result.records)
    assert all(record["url"].startswith("https://") for record in result.records)


@pytest.mark.asyncio
async def test_tag_list_string_html_fallback_urls_and_unsafe_thumbnails():
    with respx.mock() as router:
        mock_tags(router, {
            "python": [
                article(
                    1,
                    title="<b>React</b> article",
                    description="<p>Readable <em>summary</em>.</p><script>bad()</script>",
                    canonical_url="http://dev.to/unsafe",
                    url="https://dev.to/example/safe",
                    published_timestamp=None,
                    published_at="2026-01-02T00:00:00Z",
                    tag_list="javascript, python",
                    cover_image="javascript:alert(1)",
                    social_image="http://dev.to/insecure.png",
                )
            ]
        })
        result = await run_fetch()

    record, = result.records
    assert record["title"] == "React article"
    assert record["summary"] == "Readable summary."
    assert record["publishedAtIso"] == "2026-01-02T00:00:00Z"
    assert record["url"] == "https://dev.to/example/safe"
    assert "WebDev" in record["tags"]
    assert "javascript" in record["tags"] and "python" in record["tags"]
    assert "thumbnailUrl" not in record
    report = validate_dataset_items("news", result.records)
    assert report.rejected == []


@pytest.mark.asyncio
async def test_cross_tag_deduplication_unions_tags_and_sorts_deterministically():
    duplicate = article(10, tag_list=["python"], published_timestamp="2026-01-02T00:00:00Z")
    older = article(2, published_timestamp="2026-01-01T00:00:00Z")
    equal_date_high_id = article(20, published_timestamp="2026-01-02T00:00:00Z")
    with respx.mock() as router:
        mock_tags(router, {
            "ai": [duplicate, equal_date_high_id],
            "python": [
                article(10, tag_list="javascript, python",
                        published_timestamp="2026-01-02T00:00:00Z"),
                older,
            ],
        })
        result = await run_fetch()

    assert result.status == "success"
    assert [record["id"] for record in result.records] == [
        "devto-10", "devto-20", "devto-2",
    ]
    assert result.records[0]["tags"] == ["python", "javascript", "WebDev"]
    assert len(result.records) == 3


@pytest.mark.asyncio
async def test_one_tag_failure_returns_partial_when_other_tags_have_records():
    with respx.mock() as router:
        mock_tags(router, {
            "ai": httpx.Response(500),
            "python": [article(1)],
        })
        result = await run_fetch()

    assert result.status == "partial"
    assert [record["id"] for record in result.records] == ["devto-1"]

    with respx.mock() as router:
        mock_tags(router, {"ai": httpx.Response(500)})
        empty_partial = await run_fetch()
    assert empty_partial.status == "partial"
    assert empty_partial.records == []


@pytest.mark.asyncio
async def test_all_tags_failing_returns_failed_and_429_is_rate_limited():
    with respx.mock() as router:
        mock_tags(router, {
            tag: httpx.Response(500) for tag in devto.TAGS
        })
        failed = await run_fetch()
    assert failed.status == "failed"
    assert failed.records == []

    with respx.mock() as router:
        mock_tags(router, {
            tag: httpx.Response(429) for tag in devto.TAGS
        })
        limited = await run_fetch()
    assert limited.status == "rate_limited"


@pytest.mark.asyncio
async def test_empty_successful_tag_responses_return_no_data():
    with respx.mock() as router:
        mock_tags(router, {})
        result = await run_fetch()

    assert result.status == "no_data"
    assert result.records == []
