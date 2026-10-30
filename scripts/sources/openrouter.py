"""Fetch OpenRouter's catalogue into schema-shaped model records.

Modality is derived from declared input modalities: exactly text is Text,
image-only non-text input is Vision, audio-only non-text input is Audio, and
other non-text combinations are Multimodal. Provider is the ID's slug prefix,
not a provider display name. ``isOpenWeights`` is true only when OpenRouter
provides a non-empty Hugging Face ID; false means "not verified as open
weights", not "closed".
"""

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP
import math
from urllib.parse import quote

import httpx

from fetching import (
    FetchConfig,
    FetchError,
    finalize_source_result,
    get_json,
    get_logger,
)
from models import SourceResult, sanitize_error
from normalize import to_iso_utc
from validation import is_safe_external_url

SOURCE_ID = "openrouter"
URL = "https://openrouter.ai/api/v1/models"
ULTRA_LOW_COST_BASIS = "input and output each < $0.20 per 1M tokens"

_USD_PER_MILLION = Decimal("1000000")
_PRICE_QUANTUM = Decimal("0.000001")
_ULTRA_LOW_COST_LIMIT = Decimal("0.20")
_LOGGER = get_logger(SOURCE_ID)


def _price_usd_per_1m(value: object) -> Decimal | None:
    """Return exact USD per million tokens, retaining unknown and invalid prices."""
    if not isinstance(value, str):
        return None
    try:
        price = Decimal(value)
        if not price.is_finite() or price < 0:
            return None
        return price * _USD_PER_MILLION
    except (DecimalException, ValueError):
        return None


def _rounded_price(price: Decimal | None) -> float | None:
    if price is None:
        return None
    try:
        result = float(price.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP))
        return result if math.isfinite(result) else None
    except (DecimalException, OverflowError, ValueError):
        return None


def _price_per_1m(value: object) -> float | None:
    """Convert a decimal USD-per-token string to rounded USD per 1M tokens."""
    return _rounded_price(_price_usd_per_1m(value))


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _input_modalities(architecture: object) -> set[str]:
    if not isinstance(architecture, dict):
        return set()
    declared = architecture.get("input_modalities")
    if isinstance(declared, list):
        modalities = {
            value.casefold()
            for value in declared
            if isinstance(value, str) and value
        }
        if modalities:
            return modalities

    modality = architecture.get("modality")
    if isinstance(modality, str):
        inputs = modality.split("->", 1)[0]
        return {
            value.strip().casefold()
            for value in inputs.split("+")
            if value.strip()
        }
    return set()


def _modality(architecture: object) -> str:
    modalities = _input_modalities(architecture)
    non_text = modalities - {"text"}
    if modalities == {"text"}:
        return "Text"
    if non_text == {"image"}:
        return "Vision"
    if non_text == {"audio"}:
        return "Audio"
    return "Multimodal"


def _has_text_output(architecture: object) -> bool:
    if not isinstance(architecture, dict) or "output_modalities" not in architecture:
        return True
    outputs = architecture["output_modalities"]
    return isinstance(outputs, list) and any(
        isinstance(value, str) and value.casefold() == "text"
        for value in outputs
    )


def _record(entry: object) -> tuple[dict | None, str | None]:
    if not isinstance(entry, dict):
        return None, "invalid_model"

    model_id = entry.get("id")
    if (not isinstance(model_id, str) or not model_id.strip()
            or "/" not in model_id or not model_id.split("/", 1)[0].strip()):
        return None, "invalid_id"

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "missing_name"

    context_length = _positive_integer(entry.get("context_length"))
    if context_length is None:
        top_provider = entry.get("top_provider")
        if isinstance(top_provider, dict):
            context_length = _positive_integer(top_provider.get("context_length"))
    if context_length is None:
        return None, "missing_context_length"

    release_date = to_iso_utc(entry.get("created"))
    if release_date is None:
        return None, "missing_release_date"

    openrouter_url = "https://openrouter.ai/" + quote(model_id, safe="/:")
    if not is_safe_external_url(openrouter_url):
        return None, "invalid_openrouter_url"

    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    input_price = _price_usd_per_1m(pricing.get("prompt"))
    output_price = _price_usd_per_1m(pricing.get("completion"))
    input_cost = _rounded_price(input_price)
    output_cost = _rounded_price(output_price)

    architecture = entry.get("architecture")
    hugging_face_id = entry.get("hugging_face_id")
    modality = _modality(architecture)
    category_tags = []
    if modality in ("Vision", "Multimodal"):
        category_tags.append("Vision / Multimodal")
    if isinstance(hugging_face_id, str) and hugging_face_id.strip():
        category_tags.append("Open-Weights")
    if input_price == 0 and output_price == 0:
        category_tags.append("Free API Available")
    if (input_price is not None and output_price is not None
            and input_price < _ULTRA_LOW_COST_LIMIT
            and output_price < _ULTRA_LOW_COST_LIMIT):
        category_tags.append("Ultra-Low Cost")

    return {
        "id": model_id,
        "name": name,
        "provider": model_id.split("/", 1)[0],
        "arenaElo": None,
        "codingElo": None,
        "contextLength": context_length,
        "inputCostPer1M": input_cost,
        "outputCostPer1M": output_cost,
        "modality": modality,
        "isOpenWeights": isinstance(hugging_face_id, str) and bool(hugging_face_id.strip()),
        "isFreeTierAvailable": input_price == 0 and output_price == 0,
        "categoryTags": category_tags,
        "releaseDateIso": release_date,
        "openRouterUrl": openrouter_url,
    }, None


async def fetch(
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
) -> SourceResult:
    """Fetch once via the shared helper; return a result for every failure."""
    try:
        payload = await get_json(
            client,
            URL,
            config=config,
            expected_type=dict,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise FetchError("malformed_payload")

        data = payload["data"]
        records = []
        seen_ids: set[str] = set()
        skipped: Counter[str] = Counter()
        excluded_non_text_output = 0
        for entry in data:
            architecture = entry.get("architecture") if isinstance(entry, dict) else None
            if not _has_text_output(architecture):
                excluded_non_text_output += 1
                continue

            record, reason = _record(entry)
            if reason is not None:
                skipped[reason] += 1
                continue
            if record is None:
                skipped["invalid_model"] += 1
                continue
            if record["id"] in seen_ids:
                skipped["duplicate_id"] += 1
                continue
            seen_ids.add(record["id"])
            records.append(record)

        records.sort(key=lambda record: record["id"])
        _LOGGER.info(
            "OpenRouter catalogue: fetched=%d accepted=%d skipped=%s "
            "excluded_non_text_output=%d",
            len(data),
            len(records),
            dict(sorted(skipped.items())),
            excluded_non_text_output,
        )
        return finalize_source_result(
            SOURCE_ID,
            records,
            fetched=len(records),
            errors=[],
            started=now,
            finished=datetime.now(timezone.utc),
        )
    except Exception as error:
        _LOGGER.warning("OpenRouter fetch failed: %s", sanitize_error(error))
        return finalize_source_result(
            SOURCE_ID,
            [],
            fetched=0,
            errors=[error],
            started=now,
            finished=datetime.now(timezone.utc),
        )
