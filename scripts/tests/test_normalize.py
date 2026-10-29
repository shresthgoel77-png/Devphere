import hashlib
import math
import time

import pytest

from normalize import (
    canonicalize_url,
    clean_count,
    derive_topics,
    normalize_tags,
    stable_id,
    strip_html,
    to_iso_utc,
    truncate,
    url_hash_id,
)
from validation import parse_iso_datetime


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "HTTPS://Example.TEST/Path?utm_source=news&topic=rust&fbclid=click#section",
            "https://example.test/Path?topic=rust",
        ),
        (
            "https://example.test/?ref=old&mc_cid=abc&keep=&keep=two",
            "https://example.test/?keep=&keep=two",
        ),
        (
            "https://example.test/?UTM_campaign=x&gclid=y&source=kept",
            "https://example.test/?source=kept",
        ),
        ("javascript:alert(1)", None),
        ("http://example.test/path", None),
    ],
)
def test_canonicalize_url(url, expected):
    assert canonicalize_url(url) == expected


def test_ids_are_stable_and_sanitized():
    assert stable_id("reddit", "post /#42") == "reddit-post42"
    assert stable_id("src!*", "a_b:c.d-1") == "src-a_b:c.d-1"
    canonical = "https://example.test/post"
    assert url_hash_id("news", canonical) == (
        "news-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
    )


def test_strip_html_removes_script_style_markup_and_controls():
    source = (
        "<div>Hello <b>nested &amp; readable</b>"
        "<script>alert('unsafe')</script>"
        "<style>body { display: none }</style>"
        "<img src=x onerror=alert(1)> e\u0301\u0001</div>"
    )
    assert strip_html(source) == "Hello nested & readable é"
    assert strip_html("<p>one</p><p>two</p>") == "one two"
    assert strip_html("<script>hidden</script>safe") == "safe"
    assert strip_html("lone\ud800 surrogate") == "lone surrogate"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 10, "short"),
        ("one two three", 9, "one two…"),
        ("unbrokenword", 6, "unbro…"),
        ("anything", 1, "…"),
        ("anything", 0, ""),
    ],
)
def test_truncate(text, limit, expected):
    assert truncate(text, limit) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "1970-01-01T00:00:00Z"),
        (-1, "1969-12-31T23:59:59Z"),
        (1_767_225_600, "2026-01-01T00:00:00Z"),
        ("2026-01-01T03:30:00+03:30", "2026-01-01T00:00:00Z"),
        ("Thu, 01 Jan 2026 00:00:00 GMT", "2026-01-01T00:00:00Z"),
        (time.gmtime(1_767_225_600), "2026-01-01T00:00:00Z"),
    ],
)
def test_to_iso_utc(value, expected):
    result = to_iso_utc(value)
    assert result == expected
    assert parse_iso_datetime(result) is not None


@pytest.mark.parametrize(
    "value",
    [
        True,
        float("nan"),
        float("inf"),
        float("-inf"),
        "2026-01-01",
        "2026-01-01T00:00:00",
        "invalid",
        object(),
    ],
)
def test_to_iso_utc_rejects_invalid_values(value):
    assert to_iso_utc(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (3, 3),
        (3.0, 3),
        (True, None),
        (-1, None),
        (1.5, None),
        (float("nan"), None),
        (float("inf"), None),
        ("4", None),
        (None, None),
    ],
)
def test_clean_count(value, expected):
    assert clean_count(value) == expected


def test_normalize_tags_trims_sanitizes_deduplicates_and_limits():
    assert normalize_tags(
        [" Rust ", "rust", "<b>WebDev</b>", "", None, "Long tag here"],
        max_tags=3,
        max_len=10,
    ) == ["Rust", "WebDev", "Long tag…"]
    assert normalize_tags("not an iterable of labels") == []
    assert normalize_tags(["a", "b"], max_tags=0) == []


@pytest.mark.parametrize(
    ("title", "summary", "tags", "expected"),
    [
        (
            "LLMs and React",
            "Open source developer interviews",
            ["Rust"],
            ["LLMs & Agents", "WebDev", "Systems & Rust", "Open Source", "Career"],
        ),
        ("Rust compiler", None, [], ["Systems & Rust"]),
        ("Open-source browser tools", "", [], ["WebDev", "Open Source"]),
        ("A guide for Go developers", None, [], []),
        ("gopher career paths", None, [], ["Career"]),
        ("LLMs", None, [], ["LLMs & Agents"]),
        ("Unrelated announcement", None, ["something else"], []),
    ],
)
def test_derive_topics(title, summary, tags, expected):
    assert derive_topics(title, summary, tags) == expected


def test_no_topic_substring_false_positives():
    assert derive_topics("Rusty tools and careerism", None, []) == []
