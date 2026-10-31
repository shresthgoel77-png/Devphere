import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
import respx

from fetching import (
    FetchConfig,
    FetchError,
    bounded_gather,
    finalize_source_result,
    get_json,
    get_logger,
    get_text,
    new_client,
    redact_url,
    request,
)


BASE = "https://example.test/data"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.asyncio
async def test_server_errors_retry(status):
    config = FetchConfig(base_delay=0, max_delay=0)
    sleeps = []
    with respx.mock() as router:
        route = router.get(BASE).mock(
            side_effect=[httpx.Response(status), httpx.Response(200, json={"ok": True})]
        )
        async with new_client(config) as client:
            response = await request(
                client, "GET", BASE, config=config,
                sleep=lambda delay: _record_sleep(sleeps, delay),
                rng=lambda: 0.5,
            )
    assert response.status_code == 200
    assert route.call_count == 2
    assert sleeps == [0]


async def _record_sleep(values, delay):
    values.append(delay)


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "client_error"), (404, "client_error"), (403, "blocked")],
)
@pytest.mark.asyncio
async def test_client_errors_are_not_retried(status, kind):
    config = FetchConfig()
    with respx.mock() as router:
        route = router.get(BASE).mock(return_value=httpx.Response(status))
        async with new_client(config) as client:
            with pytest.raises(FetchError) as raised:
                await request(client, "GET", BASE, config=config)
    assert raised.value.kind == kind
    assert raised.value.status_code == status
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_sensitive_request_and_response_headers_never_enter_logs(caplog):
    secrets = (
        "COOKIE_VALUE_SENTINEL",
        "AUTHORIZATION_VALUE_SENTINEL",
        "API_KEY_VALUE_SENTINEL",
        "TOKEN_VALUE_SENTINEL",
    )
    caplog.set_level("DEBUG")
    config = FetchConfig(max_attempts=1)
    with respx.mock() as router:
        router.get(BASE).mock(return_value=httpx.Response(
            403,
            headers={
                "Set-Cookie": f"session={secrets[0]}",
                "Authorization": secrets[1],
                "X-Api-Key": secrets[2],
                "X-Access-Token": secrets[3],
            },
        ))
        async with new_client(config) as client:
            with pytest.raises(FetchError) as raised:
                await request(
                    client,
                    "GET",
                    BASE,
                    config=config,
                    headers={
                        "Authorization": f"Bearer {secrets[1]}",
                        "X-Api-Key": secrets[2],
                    },
                )

    captured = caplog.text
    assert "example.test" in captured
    assert "403" in captured
    assert "blocked" in captured
    assert raised.value.kind == "blocked"
    for secret in secrets:
        assert secret not in captured
        assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_small_retry_after_is_honored():
    sleeps = []
    config = FetchConfig()
    with respx.mock() as router:
        route = router.get(BASE).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200),
            ]
        )
        async with new_client(config) as client:
            response = await request(
                client, "GET", BASE, config=config,
                sleep=lambda delay: _record_sleep(sleeps, delay),
            )
    assert response.status_code == 200
    assert route.call_count == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_excessive_retry_after_fails_without_sleeping():
    sleeps = []
    config = FetchConfig(max_retry_after=3)
    with respx.mock() as router:
        router.get(BASE).mock(return_value=httpx.Response(429, headers={"Retry-After": "31"}))
        async with new_client(config) as client:
            with pytest.raises(FetchError) as raised:
                await request(
                    client, "GET", BASE, config=config,
                    sleep=lambda delay: _record_sleep(sleeps, delay),
                )
    assert raised.value.kind == "rate_limited"
    assert raised.value.retry_after == 31
    assert sleeps == []


@pytest.mark.asyncio
async def test_http_date_retry_after_is_honored():
    sleeps = []
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    header = format_datetime(retry_at, usegmt=True)
    config = FetchConfig(max_retry_after=10)
    with respx.mock() as router:
        router.get(BASE).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": header}),
                httpx.Response(200),
            ]
        )
        async with new_client(config) as client:
            await request(
                client, "GET", BASE, config=config,
                sleep=lambda delay: _record_sleep(sleeps, delay),
            )
    assert len(sleeps) == 1
    assert 0 <= sleeps[0] <= 5


@pytest.mark.asyncio
async def test_full_jitter_is_bounded_and_budget_stops_retries():
    sleeps = []
    clock_values = iter([0.0, 0.0, 0.0])
    config = FetchConfig(total_budget_s=0.3, base_delay=0.8, max_delay=1)
    with respx.mock() as router:
        route = router.get(BASE).mock(return_value=httpx.Response(503))
        async with new_client(config) as client:
            with pytest.raises(FetchError) as raised:
                await request(
                    client, "GET", BASE, config=config,
                    sleep=lambda delay: _record_sleep(sleeps, delay),
                    rng=lambda: 0.5,
                    clock=lambda: next(clock_values, 2.0),
                )
    assert raised.value.kind == "timeout"
    assert route.call_count == 1
    assert sleeps == []

    sleeps.clear()
    config = FetchConfig(base_delay=0.8, max_delay=1)
    with respx.mock() as router:
        router.get(BASE).mock(
            side_effect=[httpx.Response(503), httpx.Response(200)]
        )
        async with new_client(config) as client:
            await request(
                client, "GET", BASE, config=config,
                sleep=lambda delay: _record_sleep(sleeps, delay),
                rng=lambda: 0.5,
            )
    assert sleeps == [0.4]


@pytest.mark.asyncio
async def test_timeout_retries_then_returns_timeout_error():
    config = FetchConfig(base_delay=0, max_delay=0)
    with respx.mock() as router:
        route = router.get(BASE).mock(
            side_effect=[
                httpx.ReadTimeout("private path and secret"),
                httpx.ReadTimeout("private path and secret"),
                httpx.ReadTimeout("private path and secret"),
            ]
        )
        async with new_client(config) as client:
            with pytest.raises(FetchError) as raised:
                await request(client, "GET", BASE, config=config)
    assert raised.value.kind == "timeout"
    assert route.call_count == 3
    assert "secret" not in str(raised.value)
    assert "private" not in str(raised.value)
    assert len(str(raised.value)) <= 300


@pytest.mark.asyncio
async def test_connection_errors_are_retried():
    config = FetchConfig(base_delay=0, max_delay=0)
    with respx.mock() as router:
        route = router.get(BASE).mock(
            side_effect=[
                httpx.ConnectError("private path and secret"),
                httpx.Response(200),
            ]
        )
        async with new_client(config) as client:
            response = await request(client, "GET", BASE, config=config)
    assert response.status_code == 200
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_oversized_response_aborts():
    config = FetchConfig(max_response_bytes=4)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

    with respx.mock() as router:
        router.get(BASE).mock(return_value=httpx.Response(200, stream=Chunks()))
        async with new_client(config) as client:
            with pytest.raises(FetchError) as raised:
                await request(client, "GET", BASE, config=config)
    assert raised.value.kind == "too_large"


@pytest.mark.asyncio
async def test_json_helpers_reject_malformed_or_wrong_top_level():
    config = FetchConfig()
    with respx.mock() as router:
        route = router.get(BASE)
        route.mock(return_value=httpx.Response(200, content=b"{"))
        async with new_client(config) as client:
            with pytest.raises(FetchError) as malformed:
                await get_json(client, BASE, config=config)
            route.mock(return_value=httpx.Response(200, json="not-an-object"))
            with pytest.raises(FetchError) as wrong_type:
                await get_json(client, BASE, config=config)
    assert malformed.value.kind == wrong_type.value.kind == "malformed_payload"


@pytest.mark.asyncio
async def test_get_text_and_json_success():
    config = FetchConfig()
    with respx.mock() as router:
        route = router.get(BASE).mock(return_value=httpx.Response(200, json={"x": 1}))
        async with new_client(config) as client:
            assert await get_json(client, BASE, config=config) == {"x": 1}
            route.mock(return_value=httpx.Response(200, text="plain text"))
            assert await get_text(client, BASE, config=config) == "plain text"


@pytest.mark.asyncio
async def test_bounded_gather_is_ordered_bounded_and_failure_isolated():
    active = 0
    maximum = 0

    async def job(value):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        if value == 2:
            raise RuntimeError("expected")
        return value

    results = await bounded_gather(
        [lambda value=value: job(value) for value in (0, 1, 2, 3, 4)],
        2,
    )
    assert results[:2] == [0, 1]
    assert isinstance(results[2], RuntimeError)
    assert results[3:] == [3, 4]
    assert maximum == 2


@pytest.mark.asyncio
async def test_bounded_gather_propagates_cancellation():
    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await bounded_gather([cancelled], 1)


def test_redact_url_and_logger():
    redacted = redact_url(
        "https://user:pass@example.test/path?access_token=secret&monkey=ok&key_id=private"
    )
    assert redacted == (
        "https://example.test/path?access_token=%5BREDACTED%5D"
        "&monkey=%5BREDACTED%5D"
        "&key_id=%5BREDACTED%5D"
    )
    assert redact_url("https://example.test/path?normal=visible") == (
        "https://example.test/path?normal=visible"
    )
    assert get_logger("reddit").name == "devnexus.reddit"


@pytest.mark.parametrize(
    ("records", "errors", "status"),
    [
        ([], [], "no_data"),
        ([{"id": "one"}], [], "success"),
        ([], [FetchError("client_error")], "failed"),
        ([], [FetchError("rate_limited")], "rate_limited"),
        ([], [FetchError("rate_limited"), FetchError("rate_limited")], "rate_limited"),
        ([{"id": "one"}], [FetchError("client_error")], "partial"),
        ([], [FetchError("rate_limited"), ValueError("private secret")], "failed"),
    ],
)
def test_finalize_source_result_matrix(records, errors, status):
    result = finalize_source_result(
        "reddit",
        records,
        fetched=3,
        errors=errors,
        started=START,
        finished=START + timedelta(seconds=1, milliseconds=250),
    )
    assert result.status == status
    assert result.records is records
    assert result.fetched == 3
    assert result.latency_ms == 1250
    assert result.started_iso == "2026-01-01T00:00:00Z"
    assert result.finished_iso == "2026-01-01T00:00:01Z"
    if errors:
        assert result.error_summary is not None
        assert len(result.error_summary) <= 300
        assert "secret" not in result.error_summary


def test_fetch_error_messages_are_sanitized_and_short():
    error = FetchError("client_error", status_code=401)
    assert error.kind == "client_error"
    assert error.status_code == 401
    assert len(str(error)) <= 300
    assert "secret" not in str(error)
