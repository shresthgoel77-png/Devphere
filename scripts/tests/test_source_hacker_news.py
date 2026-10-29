import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from fetching import FetchConfig, new_client
from sources import hacker_news
from validation import validate_dataset_items

BASE = hacker_news.BASE
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hn"
CONFIG = FetchConfig(max_attempts=1, base_delay=0, max_delay=0)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def story(story_id, **overrides):
    record = {
        "id": story_id,
        "type": "story",
        "title": f"Story {story_id}",
        "by": "author",
        "time": 1767225600,
        "score": 5,
        "descendants": 2,
        "url": f"https://example.com/{story_id}",
    }
    record.update(overrides)
    return record


def mock_lists(router, top_ids, best_ids):
    router.get(f"{BASE}/topstories.json").mock(
        return_value=httpx.Response(200, json=top_ids)
    )
    router.get(f"{BASE}/beststories.json").mock(
        return_value=httpx.Response(200, json=best_ids)
    )


def mock_items(router, items):
    for story_id, response in items.items():
        if isinstance(response, httpx.Response):
            result = response
        else:
            result = httpx.Response(200, json=response)
        router.get(f"{BASE}/item/{story_id}.json").mock(return_value=result)


async def run_fetch(**options):
    async with new_client(CONFIG) as client:
        return await hacker_news.fetch(client, CONFIG, START, **options)


@pytest.mark.asyncio
async def test_live_fixtures_validate_and_story_lists_are_deduplicated():
    top_ids = load_fixture("topstories.json")[:3]
    best_ids = [top_ids[1], top_ids[0], top_ids[2]]
    items = {
        item["id"]: item for item in load_fixture("items.json")
    }
    with respx.mock() as router:
        mock_lists(router, top_ids, best_ids)
        mock_items(router, items)
        result = await run_fetch()
        assert sum(route.called for route in router.routes) == 5

    report = validate_dataset_items("news", result.records)
    assert result.status == "success"
    assert len(result.records) == len(items) == 3
    assert report.rejected == []
    assert report.dropped_optional == []
    assert report.accepted == result.records
    assert all(record["source"] == "HackerNews" for record in result.records)
    assert all("thumbnailUrl" not in record for record in result.records)


@pytest.mark.asyncio
async def test_deleted_dead_non_story_and_missing_fields_are_counted(caplog):
    stories = {
        1: story(1, deleted=True),
        2: story(2, dead=True),
        3: story(3, type="comment"),
        4: story(4, title=None),
        5: story(5, time=None),
        6: story(6, by=None),
        7: story(7),
    }
    caplog.set_level(logging.INFO, logger="devnexus.hackernews")
    with respx.mock() as router:
        mock_lists(router, list(stories), [])
        mock_items(router, stories)
        result = await run_fetch()

    assert result.status == "success"
    assert [record["id"] for record in result.records] == ["hn-7"]
    for reason in (
        "'deleted': 1",
        "'dead': 1",
        "'not_story': 1",
        "'missing_required_fields': 3",
    ):
        assert reason in caplog.text


@pytest.mark.asyncio
async def test_unsafe_http_javascript_and_missing_urls_use_discussion_url(caplog):
    stories = {
        11: story(11, url="http://example.com/story"),
        12: story(12, url="javascript:alert(1)"),
        13: story(13, url=None),
    }
    caplog.set_level(logging.INFO, logger="devnexus.hackernews")
    with respx.mock() as router:
        mock_lists(router, list(stories), [])
        mock_items(router, stories)
        result = await run_fetch()

    assert result.status == "success"
    assert [record["url"] for record in result.records] == [
        "https://news.ycombinator.com/item?id=11",
        "https://news.ycombinator.com/item?id=12",
        "https://news.ycombinator.com/item?id=13",
    ]
    assert all(not record["url"].startswith("http:") for record in result.records)
    assert "'fallback_discussion_url': 3" in caplog.text


@pytest.mark.asyncio
async def test_unknown_counts_html_summary_truncation_and_hn_tags():
    text = "<p>" + ("safe words " * 100) + "<script>alert(1)</script></p>"
    stories = {
        21: story(21, title="<b>Show HN:</b> Build with React", text=text,
                  score=0, descendants=0),
        22: story(22, title="Ask HN: <i>Rust</i>?", text=None,
                  score=None, descendants=None),
    }
    with respx.mock() as router:
        mock_lists(router, list(stories), [])
        mock_items(router, stories)
        result = await run_fetch()

    records = {record["id"]: record for record in result.records}
    show = records["hn-21"]
    ask = records["hn-22"]
    assert show["title"] == "Show HN: Build with React"
    assert len(show["summary"]) <= 600
    assert show["summary"].endswith("…")
    assert "alert(1)" not in show["summary"]
    assert show["upvotes"] == show["commentsCount"] == 0
    assert "Show HN" in show["tags"] and "WebDev" in show["tags"]
    assert ask["title"] == "Ask HN: Rust?"
    assert ask["summary"] is None
    assert ask["upvotes"] is None and ask["commentsCount"] is None
    assert "Ask HN" in ask["tags"]


@pytest.mark.asyncio
async def test_one_list_failure_does_not_prevent_partial_records():
    item = story(31)
    with respx.mock() as router:
        router.get(f"{BASE}/topstories.json").mock(
            return_value=httpx.Response(503, text="secret upstream response")
        )
        router.get(f"{BASE}/beststories.json").mock(
            return_value=httpx.Response(200, json=[31])
        )
        mock_items(router, {31: item})
        result = await run_fetch()

    assert result.status == "partial"
    assert [record["id"] for record in result.records] == ["hn-31"]
    assert "secret upstream response" not in result.error_summary


@pytest.mark.asyncio
async def test_both_list_failures_return_sanitized_failure():
    with respx.mock() as router:
        router.get(f"{BASE}/topstories.json").mock(
            return_value=httpx.Response(503, text="secret upstream response")
        )
        router.get(f"{BASE}/beststories.json").mock(
            return_value=httpx.Response(503, text="secret upstream response")
        )
        result = await run_fetch()

    assert result.status == "failed"
    assert result.records == []
    assert "secret upstream response" not in result.error_summary
    assert "withheld" in result.error_summary


@pytest.mark.asyncio
async def test_empty_successful_lists_return_no_data():
    with respx.mock() as router:
        mock_lists(router, [], [])
        result = await run_fetch()

    assert result.status == "no_data"
    assert result.records == []
    assert result.error_summary is None


@pytest.mark.asyncio
async def test_unexpected_list_errors_are_caught_and_sanitized():
    with respx.mock() as router:
        for endpoint in ("topstories", "beststories"):
            router.get(f"{BASE}/{endpoint}.json").mock(
                side_effect=RuntimeError("private secret upstream detail")
            )
        result = await run_fetch()

    assert result.status == "failed"
    assert result.records == []
    assert "private secret upstream detail" not in result.error_summary
    assert "withheld" in result.error_summary


@pytest.mark.asyncio
async def test_large_retry_after_is_rate_limited_without_sleeping():
    async def unexpected_sleep(delay):
        raise AssertionError(f"Should not sleep for {delay} seconds.")

    with respx.mock() as router:
        for endpoint in ("topstories", "beststories"):
            router.get(f"{BASE}/{endpoint}.json").mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "1000000"}
                )
            )
        result = await run_fetch(sleep=unexpected_sleep)

    assert result.status == "rate_limited"
    assert result.records == []
    assert all(route.call_count == 1 for route in router.routes)


@pytest.mark.asyncio
async def test_null_and_404_items_are_skipped_and_count_as_partial_errors(caplog):
    caplog.set_level(logging.INFO, logger="devnexus.hackernews")
    with respx.mock() as router:
        mock_lists(router, [41, 42, 43], [])
        mock_items(router, {
            41: httpx.Response(200, content=b"null"),
            42: httpx.Response(404, text="private response"),
            43: story(43),
        })
        result = await run_fetch()

    assert result.status == "partial"
    assert [record["id"] for record in result.records] == ["hn-43"]
    assert "'null_or_non_dict': 1" in caplog.text
    assert "'item_request_error': 1" in caplog.text
    assert "private response" not in result.error_summary


@pytest.mark.asyncio
async def test_request_cap_and_all_limits_are_overridable():
    ids = list(range(51, 56))
    with respx.mock(assert_all_called=False) as router:
        mock_lists(router, ids, ids[::-1])
        mock_items(router, {story_id: story(story_id) for story_id in ids})
        result = await run_fetch(
            top_limit=4,
            best_limit=4,
            item_concurrency=1,
            max_item_requests=2,
        )
        assert sum(route.called for route in router.routes) == 4

    assert result.status == "success"
    assert len(result.records) == 2


@pytest.mark.asyncio
async def test_item_concurrency_is_bounded_and_output_is_deterministic():
    stories = {
        61: story(61, time=1767225600),
        62: story(62, time=1767225602),
        63: story(63, time=1767225602),
        64: story(64, time=1767225590),
    }
    active = 0
    highest_active = 0

    async def response(request):
        nonlocal active, highest_active
        active += 1
        highest_active = max(highest_active, active)
        await asyncio.sleep(0.005)
        story_id = int(request.url.path.rsplit("/", 1)[1].removesuffix(".json"))
        active -= 1
        return httpx.Response(200, json=stories[story_id])

    async def run_with_ids(top, best):
        with respx.mock() as router:
            mock_lists(router, top, best)
            router.get(url__regex=rf"{BASE}/item/\d+\.json").mock(
                side_effect=response
            )
            result = await run_fetch(item_concurrency=2)
        return result

    first = await run_with_ids([64, 62, 61], [63, 62])
    second = await run_with_ids([61, 63], [64, 62])
    expected_ids = ["hn-62", "hn-63", "hn-61", "hn-64"]
    assert highest_active == 2
    assert [record["id"] for record in first.records] == expected_ids
    assert [record["id"] for record in second.records] == expected_ids
