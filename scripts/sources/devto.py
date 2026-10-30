"""Fetch Dev.to articles as DevNexus news records."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from fetching import (
    FetchConfig,
    bounded_gather,
    finalize_source_result,
    get_json,
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

SOURCE_ID = "devto"
BASE = "https://dev.to/api/articles"
TAGS = ("ai", "programming", "webdev", "javascript", "python", "opensource", "devops")
TAG_CONCURRENCY = 3

Sleep = Callable[[float], Awaitable[object]]


def _article_tags(value: object) -> list[object]:
    if isinstance(value, str):
        return value.split(",")
    if isinstance(value, list):
        return value
    return []


def _record(article: object) -> dict | None:
    if not isinstance(article, dict):
        return None

    native_id = article.get("id")
    if isinstance(native_id, bool) or not isinstance(native_id, (int, str)) or not str(native_id):
        return None

    title = strip_html(article.get("title"))
    user = article.get("user")
    author = None
    if isinstance(user, dict):
        author = strip_html(user.get("name")) or strip_html(user.get("username"))
    published_at = to_iso_utc(article.get("published_timestamp"))
    if published_at is None:
        published_at = to_iso_utc(article.get("published_at"))

    raw_url = article.get("canonical_url")
    url = canonicalize_url(raw_url) if isinstance(raw_url, str) else None
    if url is None:
        raw_url = article.get("url")
        url = canonicalize_url(raw_url) if isinstance(raw_url, str) else None
    if not title or not author or not published_at or not url:
        return None

    summary_text = strip_html(article.get("description"))
    summary = truncate(summary_text, 600) or None
    source_tags = _article_tags(article.get("tag_list", article.get("tags")))
    tags = normalize_tags([
        *source_tags,
        *derive_topics(title, summary, source_tags),
    ])
    record = {
        "id": stable_id("devto", native_id),
        "title": title,
        "summary": summary,
        "url": url,
        "source": "Dev.to",
        "communityOrAuthor": author,
        "publishedAtIso": published_at,
        "upvotes": clean_count(article.get("positive_reactions_count")),
        "commentsCount": clean_count(article.get("comments_count")),
        "tags": tags,
    }

    for field in ("cover_image", "social_image"):
        candidate = article.get(field)
        thumbnail = canonicalize_url(candidate) if isinstance(candidate, str) else None
        if thumbnail is not None:
            record["thumbnailUrl"] = thumbnail
            break
    return record


async def fetch(
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
    *,
    sleep: Sleep = asyncio.sleep,
    tag_concurrency: int = TAG_CONCURRENCY,
) -> SourceResult:
    """Fetch each configured tag independently and merge duplicate articles."""
    try:
        if isinstance(tag_concurrency, bool) or not isinstance(tag_concurrency, int) or tag_concurrency < 1:
            raise ValueError("Dev.to tag concurrency must be a positive integer.")

        async def fetch_tag(tag: str) -> object:
            try:
                return await get_json(
                    client,
                    f"{BASE}?tag={tag}&top=2&per_page=30",
                    config=config,
                    expected_type=list,
                    sleep=sleep,
                )
            except Exception as error:
                return error

        results = await bounded_gather(
            (lambda tag=tag: fetch_tag(tag) for tag in TAGS),
            tag_concurrency,
        )
        errors: list[object] = []
        records_by_id: dict[str, dict] = {}
        for result in results:
            if isinstance(result, Exception):
                errors.append(result)
                continue
            if not isinstance(result, list):
                continue
            for article in result:
                record = _record(article)
                if record is None:
                    continue
                existing = records_by_id.get(record["id"])
                if existing is None:
                    records_by_id[record["id"]] = record
                else:
                    existing["tags"] = normalize_tags(
                        [*existing["tags"], *record["tags"]]
                    )

        records = list(records_by_id.values())
        records.sort(key=lambda record: record["id"])
        records.sort(key=lambda record: record["publishedAtIso"], reverse=True)
        source_result = finalize_source_result(
            SOURCE_ID,
            records,
            fetched=len(records),
            errors=errors,
            started=now,
            finished=datetime.now(timezone.utc),
        )
        if errors and len(errors) < len(TAGS):
            source_result.status = "partial"
        return source_result
    except Exception as error:
        return SourceResult.failure(SOURCE_ID, error)
