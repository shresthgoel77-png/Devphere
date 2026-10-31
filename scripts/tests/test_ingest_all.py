"""Temporary-directory-only tests for the ingestion orchestration."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

import ingest_all
from fetching import FetchConfig
from models import PublishOutcome, SourceResult
import storage
from validation import validate_dataset_items, validate_metadata

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
FAST_CONFIG = FetchConfig(
    total_budget_s=1,
    max_attempts=1,
    base_delay=0,
    max_delay=0,
)

NEWS_SOURCE_NAMES = {
    "hackernews": "HackerNews",
    "devto": "Dev.to",
    "huggingface": "HuggingFace",
    "youtube": "YouTube",
    "reddit": "Reddit",
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        pytest.fail("Ingestion tests must not use the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    yield
    assert list(tmp_path.rglob("*.tmp")) == []


def news_record(source_id="hackernews", record_id="hn-new", published=None):
    return {
        "id": record_id,
        "title": f"Synthetic item {record_id}",
        "summary": None,
        "url": "https://example.invalid/story",
        "source": NEWS_SOURCE_NAMES[source_id],
        "communityOrAuthor": "Synthetic author",
        "publishedAtIso": published or "2026-10-30T10:00:00Z",
        "upvotes": None,
        "commentsCount": None,
        "tags": [],
    }


def model_record(model_id="provider/model", arena_elo=None, coding_elo=None):
    return {
        "id": model_id,
        "name": "Synthetic model",
        "provider": model_id.split("/", 1)[0],
        "arenaElo": arena_elo,
        "codingElo": coding_elo,
        "contextLength": 8192,
        "inputCostPer1M": None,
        "outputCostPer1M": None,
        "modality": "Text",
        "isOpenWeights": False,
        "isFreeTierAvailable": False,
        "categoryTags": [],
        "releaseDateIso": "2026-01-01T00:00:00Z",
        "openRouterUrl": f"https://openrouter.ai/{model_id}",
    }


def source_result(source_id, records=(), status="success", error=None):
    return SourceResult(
        source_id=source_id,
        status=status,
        records=list(records),
        fetched=len(records),
        error_summary=error,
        latency_ms=1.5,
    )


def result_fetcher(result):
    async def fetch(client, config, now):
        del client, config, now
        return deepcopy(result)

    return fetch


def raising_fetcher(error):
    async def fetch(client, config, now):
        del client, config, now
        raise error

    return fetch


def seed_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ("news", "models", "builds", "hackathons", "metadata"):
        source = ROOT / "public" / "data" / f"{name}.json"
        (path / source.name).write_bytes(source.read_bytes())
    return path


def write_ingested_dataset(path, dataset, items, *, generated="2026-01-01T00:00:00Z"):
    result = storage.publish_dataset(
        dataset,
        items,
        "ingested",
        generated,
        path / f"{dataset}.json",
        max_items=1000,
        max_bytes=2_000_000,
        allow_empty=False,
    )
    assert result.published


def update_metadata(path, updates):
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sources"].update(updates)
    result = storage.publish_metadata(metadata, metadata_path)
    assert result.published


def file_snapshot(path):
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


@pytest.mark.asyncio
async def test_all_sources_succeed_purges_seeds_and_preserves_unregistered_files(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    untouched = {
        name: (data_dir / f"{name}.json").read_bytes()
        for name in ("builds", "hackathons")
    }
    registry = {
        source_id: result_fetcher(source_result(
            source_id,
            [news_record(source_id, f"{source_id}-new")],
        ))
        for source_id in ingest_all.NEWS_SOURCE_IDS
    }
    registry["openrouter"] = result_fetcher(source_result(
        "openrouter",
        [model_record("provider/new-model")],
    ))

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    assert summary["data_changed"] is True
    assert set(summary["datasets"]) == {"news", "models", "builds", "hackathons"}
    for dataset in ("news", "models"):
        envelope = json.loads((data_dir / f"{dataset}.json").read_text(encoding="utf-8"))
        assert envelope["dataOrigin"] == "ingested"
        assert all(not item["id"].startswith("seed-") for item in envelope["items"])
    assert {
        name: (data_dir / f"{name}.json").read_bytes()
        for name in untouched
    } == untouched
    metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["dataOrigin"] == "seed"
    assert validate_metadata(metadata) == (True, None)


@pytest.mark.asyncio
async def test_one_news_source_failure_does_not_block_others_or_drop_old_records(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    old_item = news_record("hackernews", "hn-prior")
    write_ingested_dataset(data_dir, "news", [old_item])
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews", status="failed", error="token=must-not-appear",
        )),
        "devto": result_fetcher(source_result(
            "devto", [news_record("devto", "devto-fresh")],
        )),
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=list(registry),
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    items = json.loads((data_dir / "news.json").read_text(encoding="utf-8"))["items"]
    assert {item["id"] for item in items} == {"hn-prior", "devto-fresh"}
    assert summary["sources"]["hackernews"]["status"] == "failed"
    assert summary["sources"]["devto"]["status"] == "success"
    metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["sources"]["hackernews"]["status"] == "failed"
    assert "must-not-appear" not in json.dumps(summary)


@pytest.mark.asyncio
async def test_all_news_sources_failing_leaves_seed_file_byte_identical(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    before = (data_dir / "news.json").read_bytes()
    registry = {
        source_id: result_fetcher(source_result(source_id, status="failed"))
        for source_id in ingest_all.NEWS_SOURCE_IDS
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=list(registry),
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    assert (data_dir / "news.json").read_bytes() == before
    assert summary["datasets"]["news"]["status"] == "skipped"
    assert all(
        entry["status"] == "failed"
        for entry in summary["sources"].values()
    )


@pytest.mark.asyncio
async def test_unexpected_source_exception_and_hang_are_isolated_and_strict(tmp_path):
    data_dir = seed_directory(tmp_path / "data")

    async def hanging_fetch(client, config, now):
        del client, config, now
        await asyncio.sleep(10)

    registry = {
        "hackernews": raising_fetcher(RuntimeError("api_key=never-report")),
        "devto": hanging_fetch,
    }
    config = FetchConfig(total_budget_s=0.02, max_attempts=1, base_delay=0, max_delay=0)

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=list(registry),
        source_registry=registry,
        config=config,
        strict=True,
        now=NOW,
    )

    assert code == 3
    assert summary["sources"]["hackernews"]["status"] == "failed"
    assert summary["sources"]["devto"]["status"] == "failed"
    assert "never-report" not in json.dumps(summary)
    assert "timeout" in summary["sources"]["devto"]["error_summary"]


@pytest.mark.asyncio
async def test_empty_valid_result_is_no_data_but_failure_is_failed(tmp_path):
    for source_status, expected in (("no_data", "no_data"), ("failed", "failed")):
        data_dir = seed_directory(tmp_path / source_status)
        registry = {
            "hackernews": result_fetcher(source_result(
                "hackernews", status=source_status,
            ))
        }
        code, summary = await ingest_all.run_ingestion(
            data_dir,
            sources=["hackernews"],
            source_registry=registry,
            config=FAST_CONFIG,
            now=NOW,
        )
        assert code == 0
        assert summary["sources"]["hackernews"]["status"] == expected
        assert summary["datasets"]["news"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_every_invalid_record_is_reported_and_does_not_publish(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    before = (data_dir / "news.json").read_bytes()
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews", [{"id": "bad", "title": "<script>unsafe</script>"}],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["hackernews"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    assert (data_dir / "news.json").read_bytes() == before
    entry = summary["sources"]["hackernews"]
    assert entry["records_fetched"] == 1
    assert entry["records_accepted"] == 0
    assert entry["records_rejected"] == 1
    assert entry["status"] == "failed"
    assert summary["datasets"]["news"]["status"] == "skipped"


@pytest.mark.parametrize("corrupt_json", ["{", '{"schemaVersion":NaN}'])
@pytest.mark.asyncio
async def test_corrupt_dataset_without_valid_recovery_is_preserved_and_reported(
    tmp_path, corrupt_json,
):
    data_dir = seed_directory(tmp_path / "data")
    models_path = data_dir / "models.json"
    models_path.write_text(corrupt_json, encoding="utf-8")
    corrupt_bytes = models_path.read_bytes()
    metadata_path = data_dir / "metadata.json"
    metadata_before = metadata_path.read_bytes()
    registry = {
        "openrouter": result_fetcher(
            source_result("openrouter", status="failed"),
        )
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["openrouter"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 1
    assert models_path.read_bytes() == corrupt_bytes
    assert metadata_path.read_bytes() == metadata_before
    assert summary["datasets"]["models"]["status"] == "refused"
    assert "models.json is invalid" in summary["datasets"]["models"]["reason"]
    assert summary["corrupted_datasets"] == [
        {"dataset": "models", "status": "unrecovered"}
    ]
    assert summary["metadata"]["status"] == "refused"
    assert "models" in summary["metadata"]["reason"]


@pytest.mark.asyncio
async def test_corrupt_dataset_is_recovered_from_validated_incoming_records(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    models_path = data_dir / "models.json"
    models_path.write_bytes(b"")
    registry = {
        "openrouter": result_fetcher(source_result(
            "openrouter", [model_record("provider/recovered")],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["openrouter"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    recovered, status = storage.load_published(models_path)
    assert status == "ok"
    assert recovered["items"] == [model_record("provider/recovered")]
    assert summary["datasets"]["models"]["status"] == "published"
    assert "Recovered previously invalid models.json" in summary["datasets"]["models"]["reason"]
    assert summary["corrupted_datasets"] == [
        {"dataset": "models", "status": "recovered"}
    ]
    metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["counts"]["models"] == 1
    assert validate_metadata(metadata) == (True, None)


@pytest.mark.asyncio
async def test_expired_candidate_cannot_empty_replace_valid_news(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    expired = (NOW - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous_item = news_record("hackernews", "hn-old", expired)
    write_ingested_dataset(data_dir, "news", [previous_item])
    before = (data_dir / "news.json").read_bytes()
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews", [news_record("hackernews", "hn-also-old", expired)],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["hackernews"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 1
    assert (data_dir / "news.json").read_bytes() == before
    assert summary["datasets"]["news"]["status"] == "refused"
    assert summary["datasets"]["news"]["pruned"]["older_than_retention"] == 2


@pytest.mark.asyncio
async def test_publisher_refusal_sets_exit_one_and_preserves_existing_file(tmp_path, monkeypatch):
    data_dir = seed_directory(tmp_path / "data")
    before = (data_dir / "news.json").read_bytes()

    def refuse(*args, **kwargs):
        del kwargs
        return PublishOutcome("news", False, "Synthetic refusal.", str(args[4]))

    monkeypatch.setattr(ingest_all.storage, "publish_dataset", refuse)
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews", [news_record("hackernews", "hn-new")],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["hackernews"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 1
    assert (data_dir / "news.json").read_bytes() == before
    assert summary["datasets"]["news"]["status"] == "refused"


@pytest.mark.asyncio
async def test_idempotent_run_keeps_dataset_bytes_and_only_updates_attempt_metadata(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    record = news_record("hackernews", "hn-stable")
    registry = {"hackernews": result_fetcher(source_result("hackernews", [record]))}

    first_code, first_summary = await ingest_all.run_ingestion(
        data_dir, sources=["hackernews"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )
    news_before = (data_dir / "news.json").read_bytes()
    generated_at = json.loads(news_before)["generatedAtIso"]
    first_metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    second_code, second_summary = await ingest_all.run_ingestion(
        data_dir, sources=["hackernews"], source_registry=registry,
        config=FAST_CONFIG, now=NOW + timedelta(hours=1),
    )
    second_metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))

    assert first_code == second_code == 0
    assert first_summary["data_changed"] is True
    assert second_summary["data_changed"] is False
    assert second_summary["datasets"]["news"]["status"] == "unchanged"
    assert (data_dir / "news.json").read_bytes() == news_before
    assert generated_at == json.loads(news_before)["generatedAtIso"]
    assert second_metadata["lastUpdatedIso"] == first_metadata["lastUpdatedIso"]
    assert second_metadata["lastAttemptIso"] != first_metadata["lastAttemptIso"]


@pytest.mark.asyncio
async def test_old_last_success_turns_source_failure_into_stale(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    old_success = (NOW - timedelta(hours=STALE_HOURS_FOR_TEST)).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_metadata(data_dir, {
        "hackernews": {
            "status": "success",
            "lastSuccessIso": old_success,
            "lastAttemptIso": old_success,
            "recordsFetched": 1,
            "recordsAccepted": 1,
            "errorSummary": None,
        }
    })
    registry = {
        "hackernews": result_fetcher(source_result("hackernews", status="failed"))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=["hackernews"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    assert code == 0
    assert summary["sources"]["hackernews"]["status"] == "stale"
    metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["sources"]["hackernews"]["status"] == "stale"
    assert metadata["sources"]["hackernews"]["lastSuccessIso"] == old_success


STALE_HOURS_FOR_TEST = 19


@pytest.mark.asyncio
async def test_arena_rejection_count_is_reported_once_and_partial_rows_keep_last_good(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    previous_model = model_record("provider/existing", 1700, 1600)
    write_ingested_dataset(data_dir, "models", [previous_model])
    registry = {
        "openrouter": result_fetcher(source_result(
            "openrouter", [model_record("provider/existing")],
        )),
        "arena": result_fetcher(source_result("arena", [
            {
                "name": "provider/existing",
                "arena_elo": 1500,
                "coding_elo": 1400,
                "source_url": "https://example.invalid/benchmark",
                "snapshot_iso": "2026-10-29T00:00:00Z",
            },
            {
                "name": "provider/invalid",
                "arena_elo": 0,
                "coding_elo": None,
                "source_url": "https://example.invalid/benchmark",
                "snapshot_iso": "2026-10-29T00:00:00Z",
            },
        ])),
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["openrouter", "arena"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    assert summary["sources"]["arena"]["records_fetched"] == 2
    assert summary["sources"]["arena"]["records_accepted"] == 1
    assert summary["sources"]["arena"]["records_rejected"] == 1
    assert summary["sources"]["arena"]["status"] == "partial"
    assert summary["sources"]["arena"]["error_summary"] == "Validation rejected 1 source record(s)."
    models = json.loads((data_dir / "models.json").read_text(encoding="utf-8"))["items"]
    assert models == [previous_model]


@pytest.mark.asyncio
async def test_arena_failure_retains_previous_model_elos_while_openrouter_succeeds(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    old_model = model_record("provider/existing", 1720, 1630)
    write_ingested_dataset(data_dir, "models", [old_model])
    registry = {
        "openrouter": result_fetcher(source_result(
            "openrouter", [model_record("provider/existing")],
        )),
        "arena": result_fetcher(source_result("arena", status="failed")),
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["openrouter", "arena"],
        source_registry=registry,
        config=FAST_CONFIG,
        now=NOW,
    )

    assert code == 0
    models = json.loads((data_dir / "models.json").read_text(encoding="utf-8"))["items"]
    assert models == [{**model_record("provider/existing", 1720, 1630)}]
    assert summary["sources"]["arena"]["status"] == "failed"


@pytest.mark.asyncio
async def test_openrouter_failure_keeps_previous_model_file_byte_identical(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    previous = [model_record("provider/existing", 1720, 1630)]
    write_ingested_dataset(data_dir, "models", previous)
    before = (data_dir / "models.json").read_bytes()
    registry = {
        "openrouter": result_fetcher(source_result("openrouter", status="failed"))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=["openrouter"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    assert code == 0
    assert (data_dir / "models.json").read_bytes() == before
    assert summary["datasets"]["models"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_news_retention_by_age_and_cap_reports_pruned_counts(tmp_path, monkeypatch):
    data_dir = seed_directory(tmp_path / "data")
    expired = (NOW - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = [
        news_record("hackernews", "hn-expired", expired),
        news_record("hackernews", "hn-recent-a", "2026-10-20T00:00:00Z"),
        news_record("hackernews", "hn-recent-b", "2026-10-21T00:00:00Z"),
        news_record("hackernews", "hn-recent-c", "2026-10-22T00:00:00Z"),
    ]
    write_ingested_dataset(data_dir, "news", previous)
    monkeypatch.setattr(ingest_all, "NEWS_MAX_ITEMS", 2)
    registry = {
        "devto": result_fetcher(source_result(
            "devto", [news_record("devto", "devto-new", "2026-10-29T00:00:00Z")],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=["devto"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    assert code == 0
    assert summary["pruned"]["news"] == {"older_than_retention": 1, "over_limit": 2}
    items = json.loads((data_dir / "news.json").read_text(encoding="utf-8"))["items"]
    assert [item["id"] for item in items] == ["devto-new", "hn-recent-c"]


@pytest.mark.asyncio
async def test_model_limit_prunes_deterministically_and_reports_count(tmp_path, monkeypatch):
    data_dir = seed_directory(tmp_path / "data")
    monkeypatch.setattr(ingest_all, "MODELS_MAX_ITEMS", 1)
    registry = {
        "openrouter": result_fetcher(source_result(
            "openrouter",
            [model_record("provider/z"), model_record("provider/a")],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=["openrouter"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    assert code == 0
    assert summary["pruned"]["models"] == {"older_than_retention": 0, "over_limit": 1}
    models = json.loads((data_dir / "models.json").read_text(encoding="utf-8"))["items"]
    assert [item["id"] for item in models] == ["provider/a"]


@pytest.mark.asyncio
async def test_sources_subset_leaves_other_source_metadata_unchanged(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    untouched_entry = {
        "status": "no_data",
        "lastSuccessIso": None,
        "lastAttemptIso": "2026-10-29T00:00:00Z",
        "recordsFetched": 0,
        "recordsAccepted": 0,
        "errorSummary": None,
    }
    update_metadata(data_dir, {"reddit": untouched_entry})
    registry = {
        "hackernews": result_fetcher(source_result("hackernews", status="no_data"))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=["hackernews"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    assert code == 0
    assert "reddit" not in summary["sources"]
    assert metadata["sources"]["reddit"] == untouched_entry
    assert metadata["sources"]["hackernews"]["lastAttemptIso"] == "2026-10-30T12:00:00Z"


@pytest.mark.asyncio
async def test_dry_run_leaves_entire_data_directory_byte_identical(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    before = file_snapshot(data_dir)
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews", [news_record("hackernews", "hn-dry-run")],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir,
        sources=["hackernews"],
        source_registry=registry,
        config=FAST_CONFIG,
        dry_run=True,
        summary_json=tmp_path / "summary.json",
        now=NOW,
    )

    assert code == 0
    assert summary["dry_run"] is True
    assert summary["datasets"]["news"]["status"] == "skipped"
    assert summary["data_changed"] is False
    assert file_snapshot(data_dir) == before
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == summary


@pytest.mark.asyncio
async def test_corrupt_previous_dataset_is_recovered_after_valid_source_data(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    news_path = data_dir / "news.json"
    news_path.write_text("{", encoding="utf-8")
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews", [news_record("hackernews", "hn-recovered")],
        ))
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=["hackernews"], source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    recovered, status = storage.load_published(news_path)
    assert code == 0
    assert summary["datasets"]["news"]["status"] == "published"
    assert status == "ok"
    assert [item["id"] for item in recovered["items"]] == ["hn-recovered"]


@pytest.mark.asyncio
async def test_deterministic_news_and_model_ordering(tmp_path):
    data_dir = seed_directory(tmp_path / "data")
    registry = {
        "hackernews": result_fetcher(source_result(
            "hackernews",
            [
                news_record("hackernews", "hn-b", "2026-10-20T00:00:00Z"),
                news_record("hackernews", "hn-z", "2026-10-21T00:00:00Z"),
                news_record("hackernews", "hn-a", "2026-10-21T00:00:00Z"),
            ],
        )),
        "openrouter": result_fetcher(source_result(
            "openrouter",
            [model_record("provider/z"), model_record("provider/a")],
        )),
    }

    code, summary = await ingest_all.run_ingestion(
        data_dir, sources=list(registry), source_registry=registry,
        config=FAST_CONFIG, now=NOW,
    )

    assert code == 0
    news = json.loads((data_dir / "news.json").read_text(encoding="utf-8"))["items"]
    models = json.loads((data_dir / "models.json").read_text(encoding="utf-8"))["items"]
    assert [item["id"] for item in news] == ["hn-a", "hn-z", "hn-b"]
    assert [item["id"] for item in models] == ["provider/a", "provider/z"]
    assert summary["data_changed"] is True


def test_usage_error_returns_two_and_unexpected_cli_exception_returns_one(tmp_path, monkeypatch):
    assert ingest_all.main(["--data-dir", str(tmp_path), "--sources", "unknown"]) == 2

    async def fail_runtime(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("credential=not-for-output")

    monkeypatch.setattr(ingest_all, "run_ingestion", fail_runtime)
    assert ingest_all.main(["--data-dir", str(tmp_path)]) == 1

    async def fail_value(*args, **kwargs):
        del args, kwargs
        raise ValueError("unexpected operational value error")

    monkeypatch.setattr(ingest_all, "run_ingestion", fail_value)
    assert ingest_all.main(["--data-dir", str(tmp_path)]) == 1


def test_selected_fetcher_results_and_validation_helpers_are_non_mutating():
    model = model_record()
    before = deepcopy(model)
    assert validate_dataset_items("models", [model]).accepted == [model]
    assert model == before


def test_optional_arena_registry_is_available_for_injected_tests_only():
    assert "arena" not in ingest_all.SOURCE_REGISTRY
    assert ingest_all._resolve_sources(
        ["openrouter", "arena"],
        {"openrouter": result_fetcher(source_result("openrouter")), "arena": result_fetcher(source_result("arena"))},
    ) == ["openrouter", "arena"]
