"""Source-agnostic normalization helpers; no source-specific inference."""

import calendar
from collections.abc import Iterable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import hashlib
import math
import numbers
import re
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from validation import is_safe_external_url, parse_iso_datetime

_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "ref"}
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")
_SPACE = re.compile(r"\s+")
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p",
    "pre", "section", "table", "td", "th", "tr", "ul",
}
_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LLMs & Agents", (
        "llm", "llms", "large language model", "large language models",
        "generative ai", "ai agent", "ai agents", "agentic", "rag",
        "retrieval-augmented generation", "prompt engineering",
    )),
    ("WebDev", (
        "javascript", "typescript", "react", "next.js", "frontend",
        "front-end", "web development", "css", "html", "node.js", "browser",
    )),
    ("Systems & Rust", (
        "rust", "systems programming", "operating system", "linux",
        "kernel", "compiler", "memory safety", "distributed systems",
        "networking", "concurrency",
    )),
    ("Open Source", (
        "open source", "open-source", "foss", "free software",
    )),
    ("Career", (
        "career", "hiring", "job search", "job opening", "interview",
        "resume", "internship", "salary", "interviews",
    )),
)


def canonicalize_url(url: str) -> str | None:
    """Normalize URL case and discard fragments/tracking parameters safely."""
    if not is_safe_external_url(url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        query_items = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not (key.casefold().startswith("utm_") or key.casefold() in _TRACKING_KEYS)
        ]
        return urlunsplit((
            parts.scheme.lower(),
            host.lower() + port,
            parts.path,
            urlencode(query_items),
            "",
        ))
    except (TypeError, ValueError):
        return None


def stable_id(prefix: str, native_id: str | int) -> str:
    """Build a deterministic schema-safe ID, dropping disallowed characters."""
    return _ID_UNSAFE.sub("", f"{prefix}-{native_id}")


def url_hash_id(prefix: str, canonical_url: str) -> str:
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
    return f"{_ID_UNSAFE.sub('', prefix)}-{digest}"


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if self.hidden:
            if tag in ("script", "style"):
                self.hidden.append(tag)
        elif tag in ("script", "style"):
            self.hidden.append(tag)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _BLOCK_TAGS and not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden:
            reverse_index = self.hidden[::-1].index(tag)
            del self.hidden[len(self.hidden) - reverse_index - 1:]
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def strip_html(text: object) -> str:
    """Extract safe plain text, discarding markup and script/style contents."""
    if not isinstance(text, str):
        return ""
    parser = _PlainTextParser()
    parser.feed(text)
    parser.close()
    plain = unicodedata.normalize("NFC", "".join(parser.parts))
    plain = "".join(
        character for character in plain
        if unicodedata.category(character) not in ("Cc", "Cs")
    )
    return _SPACE.sub(" ", plain).strip()


def truncate(text: str, n: int) -> str:
    """Cap text at ``n`` characters, preferring a word boundary and adding …."""
    if n < 1:
        return ""
    if len(text) <= n:
        return text
    if n == 1:
        return "…"
    available = n - 1
    cut = text[:available]
    boundaries = list(re.finditer(r"\s+", cut))
    if boundaries and boundaries[-1].start() > 0:
        cut = cut[:boundaries[-1].start()]
    return cut.rstrip() + "…"


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso_utc(value: object) -> str | None:
    """Convert aware ISO/RFC-822 times, epochs, or UTC ``struct_time`` to ISO."""
    try:
        parsed: datetime
        if isinstance(value, bool):
            return None
        if isinstance(value, numbers.Real):
            if not math.isfinite(value):
                return None
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, time.struct_time):
            parsed = datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
        elif isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            parsed = value
        elif isinstance(value, str):
            parsed = parse_iso_datetime(value)
            if parsed is None:
                parsed = parsedate_to_datetime(value)
            if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
        else:
            return None

        result = _format_utc(parsed)
        return result if parse_iso_datetime(result) is not None else None
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def clean_count(value: object) -> int | None:
    """Return a non-negative integral count; preserve verified zero."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        if not math.isfinite(value) or value < 0 or value != int(value):
            return None
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return None


def normalize_tags(
    tags: object,
    *,
    max_tags: int = 8,
    max_len: int = 40,
) -> list[str]:
    """Clean, bound, and case-insensitively deduplicate upstream tag labels."""
    if max_tags < 1 or max_len < 1 or isinstance(tags, (str, bytes)):
        return []
    if not isinstance(tags, Iterable):
        return []
    values = iter(tags)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = strip_html(value).strip()
        if not label:
            continue
        label = truncate(label, max_len).strip()
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(label)
        if len(result) >= max_tags:
            break
    return result


def _has_keyword(text: str, keyword: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def derive_topics(title: str, summary: str | None, tags: object) -> list[str]:
    """Derive only fixed topics using the documented case-insensitive keywords.

    Keyword table: LLMs & Agents (llm(s), large language model(s), generative
    ai, ai agent(s), agentic, rag, retrieval-augmented generation, prompt
    engineering); WebDev (javascript, typescript, react, next.js, frontend,
    front-end, web development, css, html, node.js, browser); Systems & Rust
    (rust, systems programming, operating system, linux, kernel, compiler,
    memory safety, distributed systems, networking, concurrency); Open Source
    (open source, open-source, foss, free software); Career (career, hiring,
    job search/opening, interview, resume, internship, salary). No default
    topic is added, and ambiguous short terms such as "go" are intentionally
    excluded.
    """
    text_parts = [strip_html(title), strip_html(summary)]
    if isinstance(tags, Iterable) and not isinstance(tags, (str, bytes)):
        text_parts.extend(strip_html(tag) for tag in tags)
    text = " ".join(text_parts)
    return [
        topic for topic, keywords in _TOPIC_KEYWORDS
        if any(_has_keyword(text, keyword) for keyword in keywords)
    ]
