"""Fetch Reddit hot posts, falling back once to each subreddit RSS feed."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import math
import re
from urllib.parse import urlsplit

import feedparser
import httpx

from fetching import (
    FetchConfig,
    FetchError,
    finalize_source_result,
    get_json,
    request,
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

SOURCE_ID = "reddit"
BASE = "https://www.reddit.com"
SUBREDDITS = (
    "LocalLLaMA",
    "MachineLearning",
    "programming",
    "webdev",
    "artificial",
    "opensource",
    "SideProject",
    "developersIndia",
)
POLITE_DELAY = 1.0
JSON_LIMIT = 35
_POST_ID = re.compile(r"(?:t3_)?([A-Za-z0-9]+)")
_PERMALINK = re.compile(
    r"^/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)/[^?#\\]*$",
    re.IGNORECASE,
)
_IGNORED_THUMBNAILS = {"", "self", "default", "nsfw"}
_RATE_LIMIT_KINDS = {"blocked", "rate_limited"}

Sleep = Callable[[float], Awaitable[object]]


def _post_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _POST_ID.fullmatch(value)
    return match.group(1) if match else None


def _thread_url(permalink: object, subreddit: str) -> str | None:
    if not isinstance(permalink, str):
        return None
    try:
        if permalink.startswith("/"):
            if permalink.startswith("//"):
                return None
            path = urlsplit(permalink).path
        else:
            parts = urlsplit(permalink)
            if (
                parts.scheme.lower() != "https"
                or parts.hostname not in ("reddit.com", "www.reddit.com")
                or parts.username
                or parts.password
            ):
                return None
            path = parts.path
    except ValueError:
        return None
    match = _PERMALINK.fullmatch(path)
    if match is None or match.group(1).casefold() != subreddit.casefold():
        return None
    return canonicalize_url(f"{BASE}{path}")


def _thumbnail(value: object) -> str | None:
    if not isinstance(value, str) or value.strip().casefold() in _IGNORED_THUMBNAILS:
        return None
    return canonicalize_url(value)


def _tags(title: str, summary: str | None, subreddit: str) -> list[str]:
    return normalize_tags([
        *derive_topics(title, summary, []),
        subreddit,
    ])


def _json_record(post: object, subreddit: str) -> dict | None:
    if not isinstance(post, dict):
        return None
    # Exclude pinned posts and adult-marked posts from the public feed.
    if post.get("stickied") is True or post.get("over_18") is True:
        return None

    post_id = _post_id(post.get("id"))
    title = strip_html(post.get("title"))
    published_at = to_iso_utc(post.get("created_utc"))
    url = _thread_url(post.get("permalink"), subreddit)
    if not post_id or not title or not published_at or not url:
        return None

    summary_text = strip_html(post.get("selftext"))
    summary = truncate(summary_text, 600) or None
    record = {
        "id": stable_id("reddit", post_id),
        "title": title,
        "summary": summary,
        "url": url,
        "source": "Reddit",
        "communityOrAuthor": f"r/{subreddit}",
        "publishedAtIso": published_at,
        "upvotes": clean_count(post.get("score")),
        "commentsCount": clean_count(post.get("num_comments")),
        "tags": _tags(title, summary, subreddit),
    }
    thumbnail = _thumbnail(post.get("thumbnail"))
    if thumbnail is not None:
        record["thumbnailUrl"] = thumbnail
    return record


def _rss_thumbnail(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    thumbnails = entry.get("media_thumbnail")
    if not isinstance(thumbnails, list):
        return None
    for thumbnail in thumbnails:
        if not isinstance(thumbnail, dict):
            continue
        url = _thumbnail(thumbnail.get("url"))
        if url is not None:
            return url
    return None


def _rss_record(entry: object, subreddit: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    post_id = _post_id(entry.get("id"))
    title = strip_html(entry.get("title"))
    published_at = to_iso_utc(entry.get("published"))
    url = _thread_url(entry.get("link"), subreddit)
    if not post_id or not title or not published_at or not url:
        return None

    content = entry.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        summary_value = content[0].get("value")
    else:
        summary_value = entry.get("summary")
    summary_text = strip_html(summary_value)
    summary = truncate(summary_text, 600) or None
    record = {
        "id": stable_id("reddit", post_id),
        "title": title,
        "summary": summary,
        "url": url,
        "source": "Reddit",
        "communityOrAuthor": f"r/{subreddit}",
        "publishedAtIso": published_at,
        "upvotes": None,
        "commentsCount": None,
        "tags": _tags(title, summary, subreddit),
    }
    thumbnail = _rss_thumbnail(entry)
    if thumbnail is not None:
        record["thumbnailUrl"] = thumbnail
    return record


def _parse_rss(content: bytes) -> list[object]:
    parsed = feedparser.parse(content)
    if parsed.bozo or parsed.version != "atom10":
        raise FetchError("malformed_payload")
    return parsed.entries


def _failure_kind(error: Exception) -> str:
    if isinstance(error, FetchError):
        return error.kind
    return "network"


def _failure_label(subreddit: str, json_error: Exception, rss_error: Exception) -> str:
    return (
        f"r/{subreddit}: JSON {_failure_kind(json_error)}; "
        f"RSS {_failure_kind(rss_error)}"
    )


async def fetch(
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
    *,
    sleep: Sleep = asyncio.sleep,
    delay_s: float = POLITE_DELAY,
) -> SourceResult:
    """Fetch subreddit hot JSON sequentially, with one RSS fallback per failure."""
    try:
        if (
            isinstance(delay_s, bool)
            or not isinstance(delay_s, (int, float))
            or not math.isfinite(delay_s)
            or delay_s < 0
        ):
            raise ValueError("Reddit inter-subreddit delay must be non-negative.")

        errors: list[tuple[str, Exception, Exception]] = []
        successful_subreddits = 0
        records_by_id: dict[str, dict] = {}
        headers = {"User-Agent": config.user_agent}

        for index, subreddit in enumerate(SUBREDDITS):
            json_error: Exception | None = None
            records: list[dict] = []
            try:
                payload = await get_json(
                    client,
                    f"{BASE}/r/{subreddit}/hot.json?limit={JSON_LIMIT}",
                    config=config,
                    expected_type=dict,
                    headers=headers,
                    sleep=sleep,
                )
                data = payload.get("data")
                children = data.get("children") if isinstance(data, dict) else None
                if not isinstance(children, list):
                    raise FetchError("malformed_payload")
                for child in children:
                    post = child.get("data") if isinstance(child, dict) else None
                    record = _json_record(post, subreddit)
                    if record is not None:
                        records.append(record)
            except Exception as error:
                json_error = error

            if json_error is not None:
                rss_error: Exception | None = None
                try:
                    response = await request(
                        client,
                        "GET",
                        f"{BASE}/r/{subreddit}/hot/.rss",
                        config=config,
                        headers=headers,
                        sleep=sleep,
                    )
                    entries = _parse_rss(response.content)
                    for entry in entries[:JSON_LIMIT]:
                        record = _rss_record(entry, subreddit)
                        if record is not None:
                            records.append(record)
                except Exception as error:
                    rss_error = error

                if rss_error is not None:
                    errors.append((subreddit, json_error, rss_error))
                else:
                    successful_subreddits += 1
            else:
                successful_subreddits += 1

            for record in records:
                records_by_id.setdefault(record["id"], record)
            if index + 1 < len(SUBREDDITS) and delay_s:
                await sleep(delay_s)

        records = list(records_by_id.values())
        records.sort(key=lambda record: record["id"])
        records.sort(key=lambda record: record["publishedAtIso"], reverse=True)
        if errors:
            status = "partial" if successful_subreddits else "failed"
            if not successful_subreddits and all(
                _failure_kind(json_error) in _RATE_LIMIT_KINDS
                and _failure_kind(rss_error) in _RATE_LIMIT_KINDS
                for _, json_error, rss_error in errors
            ):
                status = "rate_limited"
        else:
            status = "success" if records else "no_data"

        result = finalize_source_result(
            SOURCE_ID,
            records,
            fetched=len(records),
            errors=[],
            started=now,
            finished=datetime.now(timezone.utc),
        )
        result.status = status
        result.error_summary = "; ".join(
            _failure_label(subreddit, json_error, rss_error)
            for subreddit, json_error, rss_error in errors
        ) or None
        return result
    except Exception as error:
        return SourceResult.failure(SOURCE_ID, error)
