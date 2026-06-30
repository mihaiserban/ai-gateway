import httpx
import pytest

from router.usage_events import HttpUsageEventSink, UsageEvent, estimate_cost_usd, extract_usage, fingerprint


def test_fingerprint_never_returns_raw_secret():
    result = fingerprint("Bearer sk-secret-value")
    assert result != "Bearer sk-secret-value"
    assert "sk-secret" not in result
    assert len(result) == 16


def test_extract_usage_handles_openai_usage_shape():
    assert extract_usage({"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}) == (11, 7, 18)


def test_extract_usage_returns_none_tuple_when_missing():
    assert extract_usage({}) == (None, None, None)
    assert extract_usage(None) == (None, None, None)


def test_estimate_cost_usd_uses_per_token_prices():
    assert estimate_cost_usd("coder", 10, 4, {"coder": (0.25, 0.75)}) == 5.5


@pytest.mark.asyncio
async def test_disabled_http_usage_sink_is_noop():
    sink = HttpUsageEventSink(None)
    await sink.record(_event())


@pytest.mark.asyncio
async def test_http_usage_sink_posts_prompt_free_event():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read().decode()
        return httpx.Response(202, json={"status": "accepted"})

    sink = HttpUsageEventSink("http://usage-ledger:4200", transport=httpx.MockTransport(handler))

    await sink.record(_event())

    assert seen["url"] == "http://usage-ledger:4200/usage-events"
    assert "secret prompt" not in seen["json"]
    assert "sk-secret" not in seen["json"]
    assert "raw-session" not in seen["json"]
    assert "keyhash" in seen["json"]
    assert "sessionhash" in seen["json"]


def _event() -> UsageEvent:
    return UsageEvent(
        timestamp=1782820800.0,
        path="/v1/chat/completions",
        method="POST",
        key_hash="keyhash",
        session_hash="sessionhash",
        requested_model="coder",
        selected_model="coder",
        served_model="coder",
        provider_model="ollama_chat/kimi-k2.7-code",
        reason="explicit-model",
        status="200",
        latency_ms=123,
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        estimated_cost_usd=5.5,
        cache_status="hit",
        fallback_count=0,
        fallback_from=None,
        error_class=None,
        stream=False,
    )
