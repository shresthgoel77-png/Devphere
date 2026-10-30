from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest
import respx

from fetching import FetchConfig, new_client
from sources import huggingface
from validation import validate_dataset_items

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hf"
CONFIG = FetchConfig(max_attempts=1, base_delay=0, max_delay=0)


def load_fixture():
    return json.loads((FIXTURES / "daily_papers.json").read_text(encoding="utf-8"))


def paper(paper_id="2601.01234", **overrides):
    record = {
        "id": paper_id,
        "authors": [{"name": "Example Researcher"}, {"name": "Second Author"}],
        "title": "Research on RAG",
        "abstract": "<p>Read about <strong>retrieval-augmented generation</strong>.</p>",
        "upvotes": 0,
        "numComments": 0,
        "publishedAt": "2026-01-02T00:00:00Z",
        "githubRepo": "https://github.com/example/project",
        "thumbnail": None,
    }
    record.update(overrides)
    return {"paper": record, "publishedAt": "2026-01-01T00:00:00Z"}


async def run_fetch():
    async with new_client(CONFIG) as client:
        return await huggingface.fetch(client, CONFIG, START)


@pytest.mark.asyncio
async def test_synthetic_fixture_maps_and_validates_with_no_rejections():
    with respx.mock() as router:
        route = router.get(huggingface.URL).mock(
            return_value=httpx.Response(200, json=load_fixture())
        )
        result = await run_fetch()

    report = validate_dataset_items("news", result.records)
    assert route.call_count == 1
    assert result.status == "success"
    assert report.rejected == []
    assert report.dropped_optional == []
    assert report.accepted == result.records
    first, second = result.records
    assert first["url"] == "https://arxiv.org/abs/2601.01234"
    assert first["title"] == "Synthetic Research on RAG"
    assert first["summary"] == "A sample retrieval-augmented generation paper."
    assert first["communityOrAuthor"] == "Example Researcher et al."
    assert first["upvotes"] == 0 and first["commentsCount"] == 3
    assert "Paper" in first["tags"] and "LLMs & Agents" in first["tags"]
    assert "githubRepo" not in first
    assert second["communityOrAuthor"] == "Hugging Face Papers"
    assert second["summary"] is None
    assert second["upvotes"] is None and second["commentsCount"] is None
    assert second["publishedAtIso"] == "2026-01-01T03:04:05Z"
    assert "githubRepo" not in second


@pytest.mark.asyncio
async def test_unsafe_thumbnail_omitted_and_bad_paper_id_skipped():
    good = paper(
        thumbnail="http://huggingface.co/insecure.png",
        publishedAt=None,
    )
    bad = paper("bad id", publishedAt="2026-01-03T00:00:00Z")
    with respx.mock() as router:
        router.get(huggingface.URL).mock(
            return_value=httpx.Response(
                200,
                json=[good, bad],
            )
        )
        result = await run_fetch()

    assert len(result.records) == 1
    record, = result.records
    assert record["url"] == "https://arxiv.org/abs/2601.01234"
    assert record["publishedAtIso"] == "2026-01-01T00:00:00Z"
    assert "thumbnailUrl" not in record
    assert validate_dataset_items("news", result.records).rejected == []


@pytest.mark.asyncio
async def test_safe_non_arxiv_id_uses_huggingface_paper_url():
    with respx.mock() as router:
        router.get(huggingface.URL).mock(
            return_value=httpx.Response(
                200,
                json=[paper("paper-alpha_2")],
            )
        )
        result = await run_fetch()

    assert result.records[0]["url"] == "https://huggingface.co/papers/paper-alpha_2"


@pytest.mark.asyncio
async def test_records_are_sorted_by_published_date_then_id():
    with respx.mock() as router:
        router.get(huggingface.URL).mock(
            return_value=httpx.Response(200, json=[
                paper("2601.01236", publishedAt="2026-01-02T00:00:00Z"),
                paper("2601.01235", publishedAt="2026-01-03T00:00:00Z"),
                paper("2601.01234", publishedAt="2026-01-03T00:00:00Z"),
            ])
        )
        result = await run_fetch()

    assert [record["id"] for record in result.records] == [
        "hf-2601.01234", "hf-2601.01235", "hf-2601.01236",
    ]


@pytest.mark.asyncio
async def test_malformed_json_fails_and_empty_list_returns_no_data():
    with respx.mock() as router:
        router.get(huggingface.URL).mock(
            return_value=httpx.Response(200, text="not JSON")
        )
        malformed = await run_fetch()
    assert malformed.status == "failed"
    assert malformed.records == []

    with respx.mock() as router:
        router.get(huggingface.URL).mock(
            return_value=httpx.Response(200, json=[])
        )
        empty = await run_fetch()
    assert empty.status == "no_data"
    assert empty.records == []
