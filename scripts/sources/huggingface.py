"""Fetch Hugging Face daily papers as DevNexus news records."""

from datetime import datetime, timezone
import re

import httpx

from fetching import FetchConfig, finalize_source_result, get_json
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

SOURCE_ID = "huggingface"
URL = "https://huggingface.co/api/daily_papers"
_ARXIV_ID = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v[1-9]\d*)?"
)
_SAFE_PAPER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _paper_url(paper_id: str) -> str | None:
    if _ARXIV_ID.fullmatch(paper_id):
        return f"https://arxiv.org/abs/{paper_id}"
    if _SAFE_PAPER_ID.fullmatch(paper_id):
        return f"https://huggingface.co/papers/{paper_id}"
    return None


def _record(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    paper = entry.get("paper")
    if not isinstance(paper, dict):
        return None

    paper_id = paper.get("id")
    if not isinstance(paper_id, str) or not paper_id:
        return None
    url = _paper_url(paper_id)
    if url is None:
        return None

    title = strip_html(paper.get("title"))
    if not title:
        return None
    summary_text = strip_html(paper.get("abstract"))
    summary = truncate(summary_text, 600) or None
    authors = paper.get("authors")
    first_author = None
    author_count = 0
    if isinstance(authors, list):
        author_count = len(authors)
        if authors and isinstance(authors[0], dict):
            first_author = strip_html(authors[0].get("name"))
    author = first_author or "Hugging Face Papers"
    if first_author and author_count > 1:
        author += " et al."

    published_at = to_iso_utc(paper.get("publishedAt"))
    if published_at is None:
        published_at = to_iso_utc(entry.get("publishedAt"))
    if published_at is None:
        return None

    tags = normalize_tags([
        *derive_topics(title, summary, []),
        "Paper",
    ])
    record = {
        "id": stable_id("hf", paper_id),
        "title": title,
        "summary": summary,
        "url": url,
        "source": "HuggingFace",
        "communityOrAuthor": author,
        "publishedAtIso": published_at,
        "upvotes": clean_count(paper.get("upvotes")),
        "commentsCount": clean_count(paper.get("numComments")),
        "tags": tags,
    }

    for thumbnail_candidate in (paper.get("thumbnail"), entry.get("thumbnail")):
        thumbnail = (
            canonicalize_url(thumbnail_candidate)
            if isinstance(thumbnail_candidate, str)
            else None
        )
        if thumbnail is not None:
            record["thumbnailUrl"] = thumbnail
            break

    # The upstream GitHub repo URL has no NewsItem field and is intentionally omitted.
    return record


async def fetch(
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
) -> SourceResult:
    """Fetch the daily papers endpoint once and return schema-shaped records."""
    try:
        payload = await get_json(
            client,
            URL,
            config=config,
            expected_type=list,
        )
        records = []
        for entry in payload:
            record = _record(entry)
            if record is not None:
                records.append(record)
        records.sort(key=lambda record: record["id"])
        records.sort(key=lambda record: record["publishedAtIso"], reverse=True)
        return finalize_source_result(
            SOURCE_ID,
            records,
            fetched=len(records),
            errors=[],
            started=now,
            finished=datetime.now(timezone.utc),
        )
    except Exception as error:
        return SourceResult.failure(SOURCE_ID, error)
