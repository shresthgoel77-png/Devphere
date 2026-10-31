"""Shared, bounded HTTP fetching primitives for source modules.

Every source module defines ``SOURCE_ID`` and an async ``fetch(client, config,
now) -> SourceResult`` function. Fetchers return schema-shaped records and never
raise; the orchestrator performs the single validation pass over their results.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import random
import time
from typing import Literal, TypeVar, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from models import SourceResult, sanitize_error
from validation import is_safe_external_url

FetchErrorKind = Literal[
    "timeout", "rate_limited", "server_error", "client_error", "blocked",
    "network", "malformed_payload", "too_large",
]
T = TypeVar("T")
ResultT = TypeVar("ResultT")
AsyncFactory = Callable[[], Awaitable[T]]
SourceFetcher = Callable[[httpx.AsyncClient, "FetchConfig", datetime], Awaitable[SourceResult]]

_ERROR_MESSAGES = {
    "timeout": "The source request timed out.",
    "rate_limited": "The source rate-limited the request.",
    "server_error": "The source returned a server error.",
    "client_error": "The source rejected the request.",
    "blocked": "The source blocked the request.",
    "network": "A network error interrupted the source request.",
    "malformed_payload": "The source returned a malformed payload.",
    "too_large": "The source response exceeded the configured size limit.",
}
_REDACTED = "[REDACTED]"
_SENSITIVE_QUERY_WORDS = ("token", "key", "secret", "auth", "password", "signature")


@dataclass(frozen=True)
class FetchConfig:
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    total_budget_s: float = 45.0
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    max_retry_after: float = 30.0
    max_response_bytes: int = 5_000_000
    user_agent: str = "DevNexusBot/1.0 (+https://github.com/shresthgoel77-png/Devphere)"

    def __post_init__(self) -> None:
        numeric = (
            self.connect_timeout, self.read_timeout, self.total_budget_s,
            self.base_delay, self.max_delay, self.max_retry_after,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in numeric
        ):
            raise ValueError("Fetch timeouts and delays must be finite and non-negative.")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one.")
        if isinstance(self.max_response_bytes, bool) or not isinstance(self.max_response_bytes, int) or self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one.")


class FetchError(Exception):
    """A classified fetch failure whose message never includes upstream text."""

    def __init__(
        self,
        kind: FetchErrorKind,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(_ERROR_MESSAGES[kind][:300])


def new_client(config: FetchConfig) -> httpx.AsyncClient:
    """Create a pooled async client; callers own it with ``async with``."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    timeout = httpx.Timeout(
        connect=config.connect_timeout,
        read=config.read_timeout,
        write=config.read_timeout,
        pool=config.connect_timeout,
    )
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=3,
        limits=httpx.Limits(max_connections=10),
        timeout=timeout,
        headers={"User-Agent": config.user_agent},
    )


def _retry_after_seconds(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None


def _backoff(config: FetchConfig, attempt: int, rng: Callable[[], float]) -> float:
    if config.base_delay == 0 or config.max_delay == 0:
        return 0.0
    saturation_attempt = math.ceil(
        math.log2(config.max_delay) - math.log2(config.base_delay)
    )
    ceiling = (
        config.max_delay
        if attempt >= max(0, saturation_attempt)
        else config.base_delay * (2 ** attempt)
    )
    sample = min(1.0, max(0.0, rng()))
    return ceiling * sample


def _raise_http_error(response: httpx.Response, retry_after: float | None = None) -> None:
    status = response.status_code
    if status == 403:
        raise FetchError("blocked", status_code=status)
    if status == 429:
        raise FetchError("rate_limited", status_code=status, retry_after=retry_after)
    if 500 <= status <= 599:
        raise FetchError("server_error", status_code=status)
    if 400 <= status <= 499:
        raise FetchError("client_error", status_code=status)


def _log_response(url: str, response: httpx.Response) -> None:
    source = urlsplit(url).hostname or "unknown"
    status = response.status_code
    category = (
        "blocked" if status == 403
        else "rate_limited" if status == 429
        else "server_error" if 500 <= status <= 599
        else "client_error" if 400 <= status <= 499
        else None
    )
    if category is None:
        logging.getLogger("devnexus.fetching").info(
            "Source %s returned HTTP %d", source, status,
        )
    else:
        logging.getLogger("devnexus.fetching").warning(
            "Source %s returned HTTP %d (%s)", source, status, category,
        )


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    config: FetchConfig,
    params: object = None,
    json: object = None,
    headers: object = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
    clock: Callable[[], float] = time.monotonic,
) -> httpx.Response:
    """Issue a bounded request, retrying only timeout/connect, 5xx, and 429."""
    if not is_safe_external_url(url):
        raise FetchError("client_error")

    started = clock()
    deadline = started + config.total_budget_s
    last_error: FetchError | None = None

    for attempt in range(config.max_attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            raise last_error or FetchError("timeout")

        try:
            async with asyncio.timeout(remaining):
                async with client.stream(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                ) as response:
                    _log_response(url, response)
                    retry_after = (
                        _retry_after_seconds(response.headers.get("Retry-After"), time.time())
                        if response.status_code == 429 else None
                    )
                    if response.status_code == 429:
                        if retry_after is not None and retry_after > config.max_retry_after:
                            raise FetchError(
                                "rate_limited",
                                status_code=429,
                                retry_after=retry_after,
                            )
                        _raise_http_error(response, retry_after)
                    if response.status_code == 403 or 400 <= response.status_code <= 499:
                        _raise_http_error(response)
                    if 500 <= response.status_code <= 599:
                        _raise_http_error(response)

                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            if int(content_length) > config.max_response_bytes:
                                raise FetchError("too_large", status_code=response.status_code)
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > config.max_response_bytes:
                            raise FetchError("too_large", status_code=response.status_code)
                        chunks.append(chunk)

                    return httpx.Response(
                        status_code=response.status_code,
                        headers=response.headers,
                        content=b"".join(chunks),
                        request=response.request,
                        extensions=response.extensions,
                    )
        except FetchError as error:
            if error.kind in ("blocked", "client_error", "too_large", "malformed_payload"):
                raise
            last_error = error
            retry_after = error.retry_after if error.kind == "rate_limited" else None
        except (httpx.TimeoutException, TimeoutError):
            last_error = FetchError("timeout")
            retry_after = None
        except httpx.ConnectError:
            last_error = FetchError("network")
            retry_after = None
        except httpx.RequestError:
            raise FetchError("network") from None

        if attempt + 1 >= config.max_attempts:
            raise last_error or FetchError("network")

        delay = retry_after if retry_after is not None else _backoff(config, attempt, rng)
        if retry_after is not None and delay > config.max_retry_after:
            raise FetchError(
                "rate_limited",
                status_code=last_error.status_code if last_error else 429,
                retry_after=delay,
            )
        remaining = deadline - clock()
        if remaining <= 0 or delay >= remaining:
            raise FetchError(
                "rate_limited" if last_error and last_error.kind == "rate_limited" else "timeout",
                status_code=last_error.status_code if last_error else None,
                retry_after=retry_after,
            )
        await sleep(delay)

    raise last_error or FetchError("network")


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    config: FetchConfig,
    expected_type: type | tuple[type, ...] = (dict, list),
    **request_options: object,
) -> object:
    response = await request(client, "GET", url, config=config, **request_options)
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise FetchError("malformed_payload", status_code=response.status_code) from None
    if not isinstance(payload, expected_type):
        raise FetchError("malformed_payload", status_code=response.status_code)
    return payload


async def get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    config: FetchConfig,
    **request_options: object,
) -> str:
    response = await request(client, "GET", url, config=config, **request_options)
    try:
        return response.text
    except (UnicodeDecodeError, LookupError):
        raise FetchError("malformed_payload", status_code=response.status_code) from None


async def bounded_gather(
    factories: Iterable[AsyncFactory[ResultT]],
    limit: int,
) -> list[ResultT | Exception]:
    """Run factories with a concurrency cap; retain failures and input ordering."""
    if limit < 1:
        raise ValueError("limit must be at least one.")
    jobs = list(factories)
    results: list[ResultT | Exception | None] = [None] * len(jobs)
    next_index = 0

    async def worker() -> None:
        nonlocal next_index
        while next_index < len(jobs):
            index = next_index
            next_index += 1
            try:
                results[index] = await jobs[index]()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                results[index] = error

    await asyncio.gather(*(worker() for _ in range(min(limit, len(jobs)))))
    return cast(list[ResultT | Exception], results)


def redact_url(url: str) -> str:
    """Remove URL credentials and redact values of sensitive query parameters."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            return "[invalid-url]"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        query = urlencode([
            (key, _REDACTED if any(word in key.casefold() for word in _SENSITIVE_QUERY_WORDS) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, host + port, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return "[invalid-url]"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("devnexus." + name)


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Source timestamps must include a timezone.")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def finalize_source_result(
    source_id: str,
    records: list[dict],
    *,
    fetched: int,
    errors: Sequence[object],
    started: datetime,
    finished: datetime,
) -> SourceResult:
    """Apply the shared source status matrix and safely summarize errors.

    ``started`` and ``finished`` are timezone-aware datetimes. Their difference
    supplies latency; ISO fields are normalized to UTC.
    """
    error_list = list(errors)
    if records:
        status = "partial" if error_list else "success"
    elif error_list:
        status = (
            "rate_limited"
            if all(isinstance(error, FetchError) and error.kind == "rate_limited" for error in error_list)
            else "failed"
        )
    else:
        status = "no_data"

    latency_ms = max(0.0, (finished - started).total_seconds() * 1000)
    error_summary = (
        "; ".join(sanitize_error(error) for error in error_list)[:300]
        if error_list else None
    )
    return SourceResult(
        source_id=source_id,
        status=status,
        records=records,
        fetched=fetched,
        error_summary=error_summary,
        started_iso=_iso_timestamp(started),
        finished_iso=_iso_timestamp(finished),
        latency_ms=latency_ms,
    )
