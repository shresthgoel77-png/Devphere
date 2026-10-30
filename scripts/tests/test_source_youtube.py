import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from fetching import FetchConfig, new_client
from sources import youtube
from validation import validate_dataset_items

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "youtube"
CONFIG = FetchConfig(max_attempts=1, base_delay=0, max_delay=0)


def feed_bytes(name="Fixture Channel", entries=None):
    entries = entries or []
    entry_xml = "".join(
        f"""
        <entry>
          <id>yt:video:{video_id}</id>
          <yt:videoId>{video_id}</yt:videoId>
          <yt:channelId>UCsBjURrPoezykLs9EqgamOA</yt:channelId>
          <title>{title}</title>
          <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}" />
          <author><name>{name}</name></author>
          <published>{published}</published>
          <media:group>
            <media:description type="plain">{description}</media:description>
            <media:thumbnail url="{thumbnail}" />
          </media:group>
        </entry>
        """
        for video_id, title, published, description, thumbnail in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
      <id>yt:channel:UCsBjURrPoezykLs9EqgamOA</id>
      <title>{name}</title>
      <author><name>{name}</name></author>
      {entry_xml}
    </feed>
    """.encode()


def mock_channels(router, responses):
    for channel_name, channel_id in youtube.CHANNELS:
        response = responses.get(channel_id)
        if response is None:
            response = httpx.Response(200, content=feed_bytes(channel_name))
        router.get(
            youtube.FEED_URL.format(channel_id=channel_id)
        ).mock(return_value=response)


async def run_fetch(**options):
    async with new_client(CONFIG) as client:
        return await youtube.fetch(client, CONFIG, START, **options)


@pytest.mark.asyncio
async def test_records_validate_with_null_metrics_and_safe_thumbnails():
    with respx.mock() as router:
        mock_channels(router, {
            youtube.CHANNELS[0][1]: httpx.Response(
                200,
                content=feed_bytes("Fireship", entries=[
                    (
                        "abC_123-xYz",
                        "A <b>RAG</b> video",
                        "2026-01-02T03:04:05Z",
                        "Watch <em>RAG</em> work.",
                        "https://i.ytimg.com/thumbnail.jpg",
                    ),
                    (
                        "abc_123-xY0",
                        "Plain video",
                        "2026-01-01T03:04:05Z",
                        "Description",
                        "http://i.ytimg.com/insecure.jpg",
                    ),
                ]),
            )
        })
        result = await run_fetch()

    report = validate_dataset_items("news", result.records)
    assert result.status == "success"
    assert report.rejected == []
    assert report.dropped_optional == []
    assert report.accepted == result.records
    first, second = result.records
    assert first["id"] == "yt-abC_123-xYz"
    assert first["url"] == "https://www.youtube.com/watch?v=abC_123-xYz"
    assert first["title"] == "A RAG video"
    assert first["summary"] == "Watch RAG work."
    assert first["communityOrAuthor"] == "Fireship"
    assert first["upvotes"] is None and first["commentsCount"] is None
    assert first["thumbnailUrl"] == "https://i.ytimg.com/thumbnail.jpg"
    assert "LLMs & Agents" in first["tags"] and "Video" in first["tags"]
    assert second["summary"] == "Description"
    assert "thumbnailUrl" not in second
    assert all(record["url"].startswith("https://") for record in result.records)


@pytest.mark.asyncio
async def test_synthetic_feed_xml_fields_are_supported():
    fixture = (FIXTURES / "synthetic_feed.xml").read_bytes()
    with respx.mock() as router:
        mock_channels(router, {
            youtube.CHANNELS[0][1]: httpx.Response(200, content=fixture)
        })
        result = await run_fetch()

    assert result.records
    assert result.records[0]["communityOrAuthor"] == "Fireship"
    assert result.records[0]["id"] == "yt-AbC123_XyZ0"


def test_live_derived_feed_fixture_fields_are_supported():
    sample = json.loads(
        (FIXTURES / "feed_sample.json").read_text(encoding="utf-8")
    )
    records = [
        youtube._record(entry, sample["feed"]["author"])
        for entry in sample["entries"]
    ]

    assert [record["id"] for record in records] == [
        "yt-nEaJMdM1djM",
        "yt-G_em1hHPnXY",
    ]
    assert all(record["communityOrAuthor"] == "Fireship" for record in records)


@pytest.mark.asyncio
async def test_bad_video_id_is_skipped():
    with respx.mock() as router:
        mock_channels(router, {
            youtube.CHANNELS[0][1]: httpx.Response(
                200,
                content=feed_bytes(entries=[
                    ("short", "Invalid ID", "2026-01-01T00:00:00Z", "summary",
                     "https://i.ytimg.com/thumbnail.jpg")
                ]),
            )
        })
        result = await run_fetch()

    assert result.records == []
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_malformed_entry_does_not_discard_valid_entries():
    with respx.mock() as router:
        mock_channels(router, {
            youtube.CHANNELS[0][1]: httpx.Response(
                200,
                content=feed_bytes(entries=[
                    ("short", "Invalid ID", "2026-01-01T00:00:00Z", "summary",
                     "https://i.ytimg.com/thumbnail.jpg"),
                    ("abC_123-xYz", "Valid video", "2026-01-02T00:00:00Z",
                     "description", "https://i.ytimg.com/thumbnail.jpg"),
                ]),
            )
        })
        result = await run_fetch()

    assert result.status == "success"
    assert [record["id"] for record in result.records] == ["yt-abC_123-xYz"]


@pytest.mark.asyncio
async def test_channel_failure_isolated_and_reported_as_partial():
    channels = youtube.CHANNELS
    with respx.mock() as router:
        mock_channels(router, {
            channels[0][1]: httpx.Response(404),
            channels[1][1]: httpx.Response(
                200,
                content=feed_bytes("AI Explained", [
                    ("abC_123-xYz", "Valid video", "2026-01-01T00:00:00Z",
                     "description", "https://i.ytimg.com/thumb.jpg")
                ]),
            ),
        })
        result = await run_fetch()

    assert result.status == "partial"
    assert len(result.records) == 1
    assert "Fireship" in result.error_summary
    assert channels[0][1] in result.error_summary
    assert "HTTP 404" in result.error_summary


@pytest.mark.asyncio
async def test_all_channel_failures_return_failed():
    with respx.mock() as router:
        mock_channels(router, {
            channel_id: httpx.Response(404)
            for _, channel_id in youtube.CHANNELS
        })
        result = await run_fetch()

    assert result.status == "failed"
    assert result.records == []
    assert len(result.error_summary) > 300
    for channel_name, channel_id in youtube.CHANNELS:
        assert channel_name in result.error_summary
        assert channel_id in result.error_summary


@pytest.mark.asyncio
async def test_malformed_xml_is_a_controlled_channel_failure():
    with respx.mock() as router:
        mock_channels(router, {
            youtube.CHANNELS[0][1]: httpx.Response(
                200, content=b"<feed><entry>"
            ),
        })
        result = await run_fetch()

    assert result.status == "partial"
    assert "Fireship" in result.error_summary
    assert "malformed_payload" in result.error_summary


@pytest.mark.asyncio
async def test_channel_concurrency_is_capped_at_three():
    active = 0
    peak_active = 0

    async def delayed_response(_request):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, content=feed_bytes())

    with respx.mock() as router:
        for _, channel_id in youtube.CHANNELS:
            router.get(
                youtube.FEED_URL.format(channel_id=channel_id)
            ).mock(side_effect=delayed_response)
        result = await run_fetch(channel_concurrency=99)

    assert result.status == "no_data"
    assert peak_active == youtube.CHANNEL_CONCURRENCY == 3


@pytest.mark.asyncio
async def test_html_instead_of_atom_is_a_channel_failure():
    channels = youtube.CHANNELS
    with respx.mock() as router:
        mock_channels(router, {
            channels[0][1]: httpx.Response(
                200, text="<html><body>Unavailable</body></html>"
            ),
        })
        result = await run_fetch()

    assert result.status == "partial"
    assert "Fireship" in result.error_summary
    assert "malformed_payload" in result.error_summary


@pytest.mark.asyncio
async def test_entry_limit_is_ten_per_channel():
    entries = [
        (
            f"v{index:010d}",
            f"Video {index}",
            f"2026-01-{(index % 28) + 1:02d}T00:00:00Z",
            "description",
            "https://i.ytimg.com/thumb.jpg",
        )
        for index in range(12)
    ]
    with respx.mock() as router:
        mock_channels(router, {
            youtube.CHANNELS[0][1]: httpx.Response(
                200,
                content=feed_bytes(entries=entries),
            ),
        })
        result = await run_fetch()

    assert len(result.records) == 10
