from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from fetching import FetchConfig, new_client
from sources import openrouter
from validation import is_safe_external_url, validate_dataset_items

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "openrouter" / "models.json"
CONFIG = FetchConfig(max_attempts=1, base_delay=0, max_delay=0)


def model(model_id="test/model", **overrides):
    record = {
        "id": model_id,
        "name": "Test Model",
        "created": 1767225600,
        "context_length": 32768,
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "top_provider": {},
        "hugging_face_id": None,
    }
    record.update(overrides)
    return record


async def run_fetch():
    async with new_client(CONFIG) as client:
        return await openrouter.fetch(client, CONFIG, START)


@pytest.mark.parametrize(
    ("value", "expected", "exact_string"),
    [
        ("0.0000025", 2.5, "2.5"),
        ("0.00001", 10.0, "10.0"),
        ("0.00000015", 0.15, "0.15"),
        ("1e-7", 0.1, "0.1"),
        ("0", 0.0, "0.0"),
        ("-1", None, None),
        (None, None, None),
        ("abc", None, None),
        ("", None, None),
    ],
)
def test_price_conversion_uses_exact_decimal_values(value, expected, exact_string):
    result = openrouter._price_per_1m(value)
    assert result == expected
    if exact_string is not None:
        assert str(result) == exact_string


def test_live_fixture_maps_unknown_and_verified_zero_prices_correctly():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    free = payload["data"][0]
    dynamic = payload["data"][2]

    free_record, reason = openrouter._record(free)
    dynamic_record, dynamic_reason = openrouter._record(dynamic)

    assert reason is None and dynamic_reason is None
    assert free_record["inputCostPer1M"] == 0.0
    assert free_record["outputCostPer1M"] == 0.0
    assert free_record["isFreeTierAvailable"] is True
    assert dynamic_record["inputCostPer1M"] is None
    assert dynamic_record["outputCostPer1M"] is None
    assert dynamic_record["isFreeTierAvailable"] is False


@pytest.mark.parametrize(
    ("prompt", "completion", "is_free"),
    [
        ("0", "0", True),
        ("0", "0.0000001", False),
        ("0.0000001", "0", False),
        (None, "0", False),
    ],
)
def test_free_detection_requires_both_verified_zero_prices(prompt, completion, is_free):
    record, reason = openrouter._record(
        model(pricing={"prompt": prompt, "completion": completion})
    )
    assert reason is None
    assert record["isFreeTierAvailable"] is is_free


@pytest.mark.parametrize(
    ("modalities", "expected"),
    [
        (["text"], "Text"),
        (["text", "image"], "Vision"),
        (["image"], "Vision"),
        (["text", "audio"], "Audio"),
        (["audio"], "Audio"),
        (["text", "image", "audio"], "Multimodal"),
        (["text", "video"], "Multimodal"),
        (["text", "file"], "Multimodal"),
    ],
)
def test_modality_from_declared_input_modalities(modalities, expected):
    assert openrouter._modality({"input_modalities": modalities}) == expected


def test_modality_falls_back_to_architecture_string():
    assert openrouter._modality({"modality": "text+image->text"}) == "Vision"


@pytest.mark.asyncio
async def test_non_text_output_is_excluded_and_missing_output_is_included(caplog):
    caplog.set_level(logging.INFO, logger="devnexus.openrouter")
    with respx.mock() as router:
        router.get(openrouter.URL).mock(
            return_value=httpx.Response(200, json={"data": [
                model("test/image-output", architecture={
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["image"],
                }),
                model("test/missing-output", architecture={"input_modalities": ["text"]}),
            ]})
        )
        result = await run_fetch()

    assert [record["id"] for record in result.records] == ["test/missing-output"]
    assert "excluded_non_text_output=1" in caplog.text


@pytest.mark.asyncio
async def test_missing_context_and_created_are_skipped_with_counted_reasons(caplog):
    caplog.set_level(logging.INFO, logger="devnexus.openrouter")
    with respx.mock() as router:
        router.get(openrouter.URL).mock(
            return_value=httpx.Response(200, json={"data": [
                model("test/no-context", context_length=0, top_provider={}),
                model("test/no-created", created=None),
            ]})
        )
        result = await run_fetch()

    assert result.records == []
    assert result.status == "no_data"
    assert "'missing_context_length': 1" in caplog.text
    assert "'missing_release_date': 1" in caplog.text


def test_context_length_falls_back_to_top_provider():
    record, reason = openrouter._record(
        model(context_length=None, top_provider={"context_length": 4096})
    )
    assert reason is None
    assert record["contextLength"] == 4096


def test_open_weights_means_hugging_face_id_is_verified():
    present, reason = openrouter._record(model(hugging_face_id="org/model"))
    absent, absent_reason = openrouter._record(model(hugging_face_id=None))

    assert reason is None and absent_reason is None
    assert present["isOpenWeights"] is True
    assert "Open-Weights" in present["categoryTags"]
    assert absent["isOpenWeights"] is False
    assert "Open-Weights" not in absent["categoryTags"]


@pytest.mark.parametrize(
    ("prompt", "completion", "expected"),
    [
        ("0.00000019", "0.00000019", True),
        ("0.0000002", "0.00000019", False),
        ("0", "0", True),
        (None, "0", False),
    ],
)
def test_ultra_low_cost_tag_uses_strict_unrounded_boundary(
    prompt, completion, expected
):
    record, reason = openrouter._record(
        model(pricing={"prompt": prompt, "completion": completion})
    )
    assert reason is None
    assert ("Ultra-Low Cost" in record["categoryTags"]) is expected
    assert openrouter.ULTRA_LOW_COST_BASIS == (
        "input and output each < $0.20 per 1M tokens"
    )


def test_category_tags_are_limited_to_defined_source_rules():
    record, reason = openrouter._record(
        model(
            architecture={"input_modalities": ["text", "image"]},
            hugging_face_id="org/model",
            pricing={"prompt": "0", "completion": "0"},
        )
    )
    assert reason is None
    assert record["categoryTags"] == [
        "Vision / Multimodal",
        "Open-Weights",
        "Free API Available",
        "Ultra-Low Cost",
    ]
    assert "Frontier Reasoning" not in record["categoryTags"]
    assert "Coding Specialist" not in record["categoryTags"]


def test_variant_id_url_keeps_colon_and_is_safe():
    record, reason = openrouter._record(model("vendor/model:free"))
    assert reason is None
    assert record["openRouterUrl"] == "https://openrouter.ai/vendor/model:free"
    assert record["provider"] == "vendor"
    assert is_safe_external_url(record["openRouterUrl"])


def test_ids_without_a_provider_slug_are_rejected():
    record, reason = openrouter._record(model("plain-id"))
    assert record is None
    assert reason == "invalid_id"


@pytest.mark.asyncio
async def test_duplicate_ids_keep_first_and_output_is_sorted():
    with respx.mock() as router:
        router.get(openrouter.URL).mock(
            return_value=httpx.Response(200, json={"data": [
                model("zeta/model", name="First"),
                model("alpha/model"),
                model("zeta/model", name="Second"),
                model("zeta/model:free"),
            ]})
        )
        result = await run_fetch()

    assert [record["id"] for record in result.records] == [
        "alpha/model", "zeta/model", "zeta/model:free",
    ]
    assert result.records[1]["name"] == "First"
    assert result.records[2]["provider"] == "zeta"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"data": {}}, []])
async def test_non_list_catalogue_payload_is_malformed(payload):
    with respx.mock() as router:
        router.get(openrouter.URL).mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await run_fetch()

    assert result.status == "failed"
    assert result.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (httpx.Response(500), "failed"),
        (httpx.Response(429), "rate_limited"),
        (
            httpx.TimeoutException("timeout", request=httpx.Request("GET", openrouter.URL)),
            "failed",
        ),
    ],
)
async def test_upstream_failures_return_classified_source_results(
    response, expected_status
):
    with respx.mock() as router:
        route = router.get(openrouter.URL)
        if isinstance(response, BaseException):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        result = await run_fetch()

    assert result.status == expected_status
    assert result.records == []


@pytest.mark.asyncio
async def test_live_fixture_validates_with_no_rejections_and_is_deterministic():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with respx.mock() as router:
        route = router.get(openrouter.URL).mock(
            return_value=httpx.Response(200, json=payload)
        )
        first = await run_fetch()
        second = await run_fetch()

    report = validate_dataset_items("models", first.records)
    assert route.call_count == 2
    assert first.records == second.records
    assert first.status == "success"
    assert report.rejected == []
    assert report.accepted == first.records
    assert all(record["arenaElo"] is None for record in first.records)
    assert all(record["codingElo"] is None for record in first.records)


@pytest.mark.asyncio
async def test_valid_empty_catalogue_returns_no_data():
    with respx.mock() as router:
        router.get(openrouter.URL).mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        result = await run_fetch()

    assert result.status == "no_data"
    assert result.records == []
