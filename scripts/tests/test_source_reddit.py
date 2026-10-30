from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest
import respx

from fetching import FetchConfig, new_client
from sources import reddit
from validation import validate_dataset_items

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "reddit"
CONFIG = FetchConfig(max_attempts=1, base_delay=0, max_delay=0)


def load_json_fixture():
    return json.loads((FIXTURES / "synthetic_hot.json").read_text(encoding="utf-8"))


def subreddit_url(subreddit):
    return f"{reddit.BASE}/r/{subreddit}/hot.json?limit=35"


def rss_url(subreddit):
    return f"{reddit.BASE}/r/{subreddit}/hot/.rss"


def hot_payload(subreddit, posts):
    return {
        "kind": "Listing",
        "data": {
            "children": [
                {"kind": "t3", "data": post}
                for post in posts
            ],
            "after": None,
            "before": None,
        },
    }


def post(post_id, subreddit="LocalLLaMA", **overrides):
    record = {
        "id": post_id,
        "subreddit": subreddit,
        "title": f"Post {post_id}",
        "permalink": f"/r/{subreddit}/comments/{post_id}/post_title/",
        "selftext": "<p>Example <strong>summary</strong>.</p>",
        "created_utc": 1767225600,
        "score": 5,
        "num_comments": 2,
        "thumbnail": "https://preview.redd.it/thumb.jpg",
        "stickied": False,
        "over_18": False,
    }
    record.update(overrides)
    return record


def mock_json(router, response_by_subreddit):
    for subreddit in reddit.SUBREDDITS:
        response = response_by_subreddit.get(
            subreddit,
            httpx.Response(200, json=hot_payload(subreddit, [])),
        )
        if not isinstance(response, httpx.Response):
            response = httpx.Response(200, json=response)
        router.get(subreddit_url(subreddit)).mock(return_value=response)


async def _no_sleep(delay):
    return None


async def run_fetch(**options):
    async with new_client(CONFIG) as client:
        return await reddit.fetch(
            client,
            CONFIG,
            START,
            sleep=_no_sleep,
            delay_s=0,
            **options,
        )


@pytest.mark.asyncio
async def test_json_hot_happy_path_skips_stickied_nsfw_and_validates():
    sample = load_json_fixture()
    with respx.mock() as router:
        mock_json(router, {"LocalLLaMA": sample})
        result = await run_fetch()

    report = validate_dataset_items("news", result.records)
    assert result.status == "success"
    assert report.rejected == []
    assert report.dropped_optional == []
    assert report.accepted == result.records
    assert len(result.records) == 2
    record = next(record for record in result.records if record["id"] == "reddit-post001")
    assert record["title"] == "A RAG post"
    assert record["summary"] == "Readable summary."
    assert record["communityOrAuthor"] == "r/LocalLLaMA"
    assert record["url"] == (
        "https://www.reddit.com/r/LocalLLaMA/comments/post001/post_title/"
    )
    assert record["upvotes"] == 0 and record["commentsCount"] == 0
    assert record["thumbnailUrl"] == "https://preview.redd.it/thumb.jpg"
    assert "LLMs & Agents" in record["tags"]
    assert "LocalLLaMA" in record["tags"]


@pytest.mark.asyncio
async def test_json_html_stripping_missing_summary_and_bad_thumbnail():
    with respx.mock() as router:
        mock_json(router, {
            "LocalLLaMA": hot_payload("LocalLLaMA", [
                post(
                    "post002",
                    selftext="<p>Useful <em>text</em>.</p><script>hidden()</script>",
                    thumbnail="default",
                ),
                post(
                    "post003",
                    selftext="",
                    thumbnail="http://preview.redd.it/insecure.jpg",
                ),
            ])
        })
        result = await run_fetch()

    assert result.records[0]["summary"] == "Useful text."
    assert result.records[1]["summary"] is None
    assert "thumbnailUrl" not in result.records[0]
    assert "thumbnailUrl" not in result.records[1]


@pytest.mark.asyncio
async def test_deterministic_order_and_dedupe_across_subreddits(monkeypatch):
    subreddits = ("LocalLLaMA", "MachineLearning")
    monkeypatch.setattr(reddit, "SUBREDDITS", subreddits)
    posts = [
        post("post003", subreddit="LocalLLaMA",
             created_utc=1767312000),
        post("post002", subreddit="LocalLLaMA",
             created_utc=1767312000),
        post("post001", subreddit="MachineLearning",
             created_utc=1767225600),
    ]
    with respx.mock() as router:
        router.get(subreddit_url("LocalLLaMA")).mock(
            return_value=httpx.Response(
                200,
                json=hot_payload("LocalLLaMA", posts[:2]),
            )
        )
        router.get(subreddit_url("MachineLearning")).mock(
            return_value=httpx.Response(
                200,
                json=hot_payload("MachineLearning", [
                    posts[2],
                    post("post002", subreddit="MachineLearning",
                         created_utc=1767312000),
                ]),
            )
        )
        result = await run_fetch()

    assert [record["id"] for record in result.records] == [
        "reddit-post002", "reddit-post003", "reddit-post001",
    ]


@pytest.mark.asyncio
async def test_403_json_falls_back_once_to_rss_without_changing_user_agent(monkeypatch):
    subreddit = "LocalLLaMA"
    monkeypatch.setattr(reddit, "SUBREDDITS", (subreddit,))
    headers_seen = []

    def observe(request):
        headers_seen.append(request.headers.get("User-Agent"))

    with respx.mock() as router:
        json_route = router.get(subreddit_url(subreddit)).mock(
            side_effect=lambda request: (
                observe(request),
                httpx.Response(403),
            )[1]
        )
        rss_route = router.get(rss_url(subreddit)).mock(
            side_effect=lambda request: (
                observe(request),
                httpx.Response(
                    200,
                    content=(FIXTURES / "synthetic_hot.rss").read_bytes(),
                    headers={"Content-Type": "application/atom+xml"},
                ),
            )[1]
        )
        result = await run_fetch()

    assert json_route.call_count == 1
    assert rss_route.call_count == 1
    assert result.status == "success"
    assert len(result.records) == 1
    record, = result.records
    assert record["url"] == (
        "https://www.reddit.com/r/LocalLLaMA/comments/rss001/rss_sample/"
    )
    assert record["upvotes"] is None and record["commentsCount"] is None
    assert record["summary"] == "RSS <contents> are cleaned."
    assert headers_seen == [CONFIG.user_agent, CONFIG.user_agent]


def test_sanitized_rss_fixture_normalizes_to_valid_news_records():
    sample = json.loads(
        (FIXTURES / "rss_sample.json").read_text(encoding="utf-8")
    )
    records = [
        reddit._rss_record(entry, "LocalLLaMA")
        for entry in sample["entries"]
    ]
    records = [record for record in records if record is not None]

    report = validate_dataset_items("news", records)
    assert len(records) == 2
    assert report.rejected == []
    assert report.accepted == records
    assert all(
        entry["author"].startswith("synthetic-user-")
        for entry in sample["entries"]
    )
    assert "s=" not in sample["entries"][0]["media_thumbnail"][0]["url"]


@pytest.mark.asyncio
async def test_rss_failure_is_reported_for_the_subreddit(monkeypatch):
    monkeypatch.setattr(reddit, "SUBREDDITS", ("LocalLLaMA",))
    with respx.mock() as router:
        mock_json(router, {"LocalLLaMA": httpx.Response(403)})
        rss_route = router.get(rss_url("LocalLLaMA")).mock(
            return_value=httpx.Response(500)
        )
        result = await run_fetch()

    assert rss_route.call_count == 1
    assert result.status == "failed"
    assert result.records == []
    assert "r/LocalLLaMA" in result.error_summary
    assert "blocked" in result.error_summary
    assert "server_error" in result.error_summary


@pytest.mark.asyncio
async def test_failed_subreddit_with_other_success_is_partial(monkeypatch):
    subreddits = ("LocalLLaMA", "MachineLearning")
    monkeypatch.setattr(reddit, "SUBREDDITS", subreddits)
    with respx.mock() as router:
        mock_json(router, {
            "LocalLLaMA": hot_payload("LocalLLaMA", [
                post("post001"),
            ]),
            "MachineLearning": httpx.Response(403),
        })
        router.get(rss_url("MachineLearning")).mock(
            return_value=httpx.Response(500)
        )
        result = await run_fetch()

    assert result.status == "partial"
    assert [record["id"] for record in result.records] == ["reddit-post001"]
    assert "r/MachineLearning" in result.error_summary


@pytest.mark.asyncio
async def test_all_subreddit_failures_remain_identifiable_and_fallback_once(
    monkeypatch,
):
    subreddits = reddit.SUBREDDITS
    monkeypatch.setattr(reddit, "SUBREDDITS", subreddits)
    with respx.mock() as router:
        json_routes = {
            subreddit: router.get(subreddit_url(subreddit)).mock(
                return_value=httpx.Response(403)
            )
            for subreddit in subreddits
        }
        rss_routes = {
            subreddit: router.get(rss_url(subreddit)).mock(
                return_value=httpx.Response(500)
            )
            for subreddit in subreddits
        }
        result = await run_fetch()

    assert result.status == "failed"
    assert len(result.error_summary) > 300
    assert all(f"r/{subreddit}" in result.error_summary for subreddit in subreddits)
    assert all(route.call_count == 1 for route in json_routes.values())
    assert all(route.call_count == 1 for route in rss_routes.values())
    assert sum(route.call_count for route in json_routes.values()) + sum(
        route.call_count for route in rss_routes.values()
    ) <= 2 * len(subreddits)


@pytest.mark.asyncio
async def test_large_retry_after_is_rate_limited_without_sleeping(monkeypatch):
    monkeypatch.setattr(reddit, "SUBREDDITS", ("LocalLLaMA",))
    async def should_not_sleep(delay):
        raise AssertionError(f"Unexpected sleep: {delay}")

    config = FetchConfig(
        max_attempts=3,
        max_retry_after=1,
        base_delay=0,
        max_delay=0,
    )
    with respx.mock() as router:
        json_route = router.get(subreddit_url("LocalLLaMA")).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "3600"})
        )
        rss_route = router.get(rss_url("LocalLLaMA")).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "3600"})
        )
        async with new_client(config) as client:
            result = await reddit.fetch(
                client,
                config,
                START,
                sleep=should_not_sleep,
                delay_s=0,
            )

    assert json_route.call_count == 1
    assert rss_route.call_count == 1
    assert result.status == "rate_limited"


@pytest.mark.asyncio
async def test_aggregate_request_count_is_bounded_across_subreddits():
    subreddits = reddit.SUBREDDITS
    with respx.mock() as router:
        routes = {
            subreddit: router.get(subreddit_url(subreddit)).mock(
                return_value=httpx.Response(
                    200,
                    json=hot_payload(subreddit, []),
                )
            )
            for subreddit in subreddits
        }
        result = await run_fetch()

    assert result.status == "no_data"
    assert all(route.call_count == 1 for route in routes.values())
    assert sum(route.call_count for route in routes.values()) <= 2 * len(subreddits)
