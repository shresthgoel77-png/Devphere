"""Fetch Hacker News top and best stories as DevNexus news records."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from fetching import (
    FetchConfig,
    bounded_gather,
    finalize_source_result,
    get_json,
    get_logger,
)
from models import SourceResult
from normalize import (
    canonicalize_url,
    clean_count,
    derive_topics,
    normalize_tags,
    stable_id,
    strip_html,
    to_iso_utc,
    truncate,
)

SOURCE_ID = "hackernews"
BASE = "https://hacker-news.firebaseio.com/v0"
TOP_LIMIT = 60
BEST_LIMIT = 40
ITEM_CONCURRENCY = 10
MAX_ITEM_REQUESTS = 100

Sleep = Callable[[float], Awaitable[object]]


async def fetch(
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
    *,
    sleep: Sleep = asyncio.sleep,
    top_limit: int = TOP_LIMIT,
    best_limit: int = BEST_LIMIT,
    item_concurrency: int = ITEM_CONCURRENCY,
    max_item_requests: int = MAX_ITEM_REQUESTS,
) -> SourceResult:
    """Fetch independently listed stories, returning schema-shaped records."""
    skipped: Counter[str] = Counter()
    errors: list[Exception] = []
    try:
        limits = (top_limit, best_limit, item_concurrency, max_item_requests)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in limits):
            raise ValueError("Hacker News request limits must be integers.")
        if top_limit < 0 or best_limit < 0 or max_item_requests < 0 or item_concurrency < 1:
            raise ValueError("Hacker News request limits are outside their valid range.")

        async def fetch_ids(name: str, limit: int) -> list[int]:
            try:
                payload = await get_json(
                    client,
                    f"{BASE}/{name}.json",
                    config=config,
                    expected_type=list,
                    sleep=sleep,
                )
            except Exception as error:
                errors.append(error)
                return []
            if not isinstance(payload, list):
                errors.append(ValueError("Hacker News story list is malformed."))
                return []
            return [
                story_id for story_id in payload[:limit]
                if isinstance(story_id, int) and not isinstance(story_id, bool)
            ]

        top_ids = await fetch_ids("topstories", top_limit)
        best_ids = await fetch_ids("beststories", best_limit)

        story_ids: list[int] = []
        seen_ids: set[int] = set()
        for story_id in (*top_ids, *best_ids):
            if len(story_ids) >= max_item_requests:
                break
            if story_id not in seen_ids:
                seen_ids.add(story_id)
                story_ids.append(story_id)

        async def fetch_item(story_id: int) -> object:
            return await get_json(
                client,
                f"{BASE}/item/{story_id}.json",
                config=config,
                expected_type=object,
                sleep=sleep,
            )

        item_results = await bounded_gather(
            (lambda story_id=story_id: fetch_item(story_id) for story_id in story_ids),
            item_concurrency,
        )

        records: list[dict] = []
        fallback_url = "https://news.ycombinator.com/item?id={}"
        for result in item_results:
            if isinstance(result, Exception):
                errors.append(result)
                skipped["item_request_error"] += 1
                continue
            if not isinstance(result, dict):
                skipped["null_or_non_dict"] += 1
                continue
            if result.get("deleted") is True:
                skipped["deleted"] += 1
                continue
            if result.get("dead") is True:
                skipped["dead"] += 1
                continue
            if result.get("type") != "story":
                skipped["not_story"] += 1
                continue

            title = strip_html(result.get("title"))
            author = strip_html(result.get("by"))
            published_at = to_iso_utc(result.get("time"))
            story_id = result.get("id")
            if (
                not title
                or not author
                or published_at is None
                or isinstance(story_id, bool)
                or not isinstance(story_id, int)
            ):
                skipped["missing_required_fields"] += 1
                continue

            summary_text = strip_html(result.get("text"))
            summary = truncate(summary_text, 600) or None
            raw_url = result.get("url")
            url = canonicalize_url(raw_url) if isinstance(raw_url, str) else None
            if url is None:
                url = fallback_url.format(story_id)
                skipped["fallback_discussion_url"] += 1

            extra_tags: list[str] = []
            title_prefix = title.casefold()
            if title_prefix.startswith("show hn:"):
                extra_tags.append("Show HN")
            elif title_prefix.startswith("ask hn:"):
                extra_tags.append("Ask HN")
            tags = normalize_tags(
                [*derive_topics(title, summary, []), *extra_tags]
            )
            records.append({
                "id": stable_id("hn", story_id),
                "title": title,
                "summary": summary,
                "url": url,
                "source": "HackerNews",
                "communityOrAuthor": author,
                "publishedAtIso": published_at,
                "upvotes": clean_count(result.get("score")),
                "commentsCount": clean_count(result.get("descendants")),
                "tags": tags,
            })

        records.sort(key=lambda record: (record["publishedAtIso"], record["id"]))
        records.sort(key=lambda record: record["publishedAtIso"], reverse=True)
        get_logger(SOURCE_ID).info("Hacker News fetch counters: %s", dict(skipped))
        return finalize_source_result(
            SOURCE_ID,
            records,
            fetched=len(records),
            errors=errors,
            started=now,
            finished=datetime.now(timezone.utc),
        )
    except Exception as error:
        get_logger(SOURCE_ID).info("Hacker News fetch counters: %s", dict(skipped))
        return SourceResult.failure(SOURCE_ID, error)
