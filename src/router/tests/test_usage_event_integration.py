import json

import httpx
import pytest

from router.main import create_app
from router.usage_events import UsageEvent


class FakeSink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        if self.fail:
            raise RuntimeError("usage sink unavailable")
        self.events.append(event)


@pytest.mark.asyncio
async def test_chat_emits_prompt_free_usage_event():
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
            headers={"x-litellm-cache-hit": "true"},
        )

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        usage_sink=sink,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-test-secret", "X-Session-Id": "session-raw"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "secret prompt text must not be stored"}],
            },
        )

    assert response.status_code == 200
    event = sink.events[0]
    assert event.key_hash != "Bearer sk-test-secret"
    assert event.session_hash != "session-raw"
    assert event.requested_model == "coder"
    assert event.selected_model == "coder"
    assert event.served_model == "coder"
    assert event.status == "200"
    assert event.prompt_tokens == 10
    assert event.completion_tokens == 4
    assert event.total_tokens == 14
    assert event.cache_status == "hit"
    assert event.stream is False
    rendered = json.dumps(event.__dict__, default=str)
    assert "secret prompt text" not in rendered
    assert "sk-test-secret" not in rendered
    assert "session-raw" not in rendered


@pytest.mark.asyncio
async def test_usage_sink_failure_does_not_fail_chat():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        usage_sink=FakeSink(fail=True),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sink-failure"},
            json={"messages": [{"role": "user", "content": "say hello"}]},
        )

    assert response.status_code == 200
