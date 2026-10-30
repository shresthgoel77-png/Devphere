"""Fetch configured YouTube channel feeds as DevNexus news records."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import re

import feedparser
import httpx

from fetching import (
    FetchConfig,
    FetchError,
    bounded_gather,
    finalize_source_result,
    request,
)
from models import SourceResult
from normalize import (
    canonicalize_url,
    derive_topics,
    normalize_tags,
    stable_id,
    strip_html,
    to_iso_utc,
    truncate,
)

SOURCE_ID = "youtube"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHANNELS = (
    ("Fireship", "UCsBjURrPoezykLs9EqgamOA"),
    ("AI Explained", "UCNJ1Ymd5yFuUPtn21xtRbbw"),
    ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg"),
    ("Andrej Karpathy", "UCXUPKJO5MZQN11PqgIvyuvQ"),
    ("Traversy Media", "UC29ju8bIPH5as8OGnQzwJyA"),
    ("Hitesh Choudhary", "UCXgGY0wkgOzynnHvSEVmE3A"),
)
CHANNEL_CONCURRENCY = 3
MAX_ENTRIES_PER_CHANNEL = 10
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

Sleep = Callable[[float], Awaitable[object]]


def _thumbnail(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    thumbnails = entry.get("media_thumbnail")
    if not isinstance(thumbnails, list):
        return None
    for thumbnail in thumbnails:
        if not isinstance(thumbnail, dict):
            continue
        url = thumbnail.get("url")
        safe_url = canonicalize_url(url) if isinstance(url, str) else None
        if safe_url is not None:
            return safe_url
    return None


def _description(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    description = entry.get("media_description")
    if isinstance(description, str):
        return description
    media_group = entry.get("media_group")
    if isinstance(media_group, dict):
        description = media_group.get("media_description")
        if isinstance(description, str):
            return description
    return entry.get("summary", "")


def _record(entry: object, channel_name: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    video_id = entry.get("yt_videoid")
    if not isinstance(video_id, str) or _VIDEO_ID.fullmatch(video_id) is None:
        return None

    title = strip_html(entry.get("title"))
    published_at = to_iso_utc(entry.get("published"))
    author = strip_html(entry.get("author")) or channel_name
    if not title or not published_at or not author:
        return None

    summary_text = strip_html(_description(entry))
    summary = truncate(summary_text, 600) or None
    url = canonicalize_url(f"https://www.youtube.com/watch?v={video_id}")
    if url is None:
        return None
    record = {
        "id": stable_id("yt", video_id),
        "title": title,
        "summary": summary,
        "url": url,
        "source": "YouTube",
        "communityOrAuthor": author,
        "publishedAtIso": published_at,
        # Feed view totals are not reliable likes/comments and have no schema field.
        "upvotes": None,
        "commentsCount": None,
        "tags": normalize_tags([
            *derive_topics(title, summary, []),
            "Video",
        ]),
    }
    thumbnail = _thumbnail(entry)
    if thumbnail is not None:
        record["thumbnailUrl"] = thumbnail
    return record


def _channel_error(channel_name: str, channel_id: str, error: Exception) -> str:
    if isinstance(error, FetchError):
        detail = error.kind
        if error.status_code is not None:
            detail += f" HTTP {error.status_code}"
    else:
        detail = "malformed_payload"
    return f"{channel_name} ({channel_id}): {detail}"


async def fetch(
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
    *,
    sleep: Sleep = asyncio.sleep,
    channel_concurrency: int = CHANNEL_CONCURRENCY,
) -> SourceResult:
    """Fetch each configured feed independently, with bounded concurrency."""
    try:
        if (
            isinstance(channel_concurrency, bool)
            or not isinstance(channel_concurrency, int)
            or channel_concurrency < 1
        ):
            raise ValueError("YouTube channel concurrency must be a positive integer.")

        async def fetch_channel(
            channel_name: str,
            channel_id: str,
        ) -> tuple[str, str, list[dict], Exception | None]:
            try:
                response = await request(
                    client,
                    "GET",
                    FEED_URL.format(channel_id=channel_id),
                    config=config,
                    headers={"User-Agent": config.user_agent},
                    sleep=sleep,
                )
                parsed = feedparser.parse(response.content)
                if parsed.bozo or parsed.version != "atom10":
                    raise FetchError("malformed_payload", status_code=response.status_code)
                feed_author = strip_html(parsed.feed.get("author"))
                if not feed_author:
                    feed_author = strip_html(parsed.feed.get("title"))
                if not feed_author:
                    feed_author = channel_name
                records = []
                for entry in parsed.entries[:MAX_ENTRIES_PER_CHANNEL]:
                    record = _record(entry, feed_author)
                    if record is not None:
                        records.append(record)
                return channel_name, channel_id, records, None
            except Exception as error:
                return channel_name, channel_id, [], error

        results = await bounded_gather(
            (
                lambda channel=channel: fetch_channel(*channel)
                for channel in CHANNELS
            ),
            min(channel_concurrency, CHANNEL_CONCURRENCY),
        )
        successful_channels = 0
        errors: list[str] = []
        records_by_id: dict[str, dict] = {}
        for result in results:
            if isinstance(result, Exception):
                errors.append(f"channel task: {type(result).__name__}")
                continue
            channel_name, channel_id, records, error = result
            if error is not None:
                errors.append(_channel_error(channel_name, channel_id, error))
                continue
            successful_channels += 1
            for record in records:
                records_by_id.setdefault(record["id"], record)

        records = list(records_by_id.values())
        records.sort(key=lambda record: record["id"])
        records.sort(key=lambda record: record["publishedAtIso"], reverse=True)
        if errors:
            status = "partial" if successful_channels else "failed"
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
        result.error_summary = "; ".join(errors) or None
        return result
    except Exception as error:
        return SourceResult.failure(SOURCE_ID, error)
