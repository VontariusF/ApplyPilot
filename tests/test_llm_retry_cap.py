"""F6: header-derived rate-limit sleeps are clamped to one ceiling.

`Retry-After` and `x-ratelimit-reset` are server hints, and servers get them
wrong: x-ratelimit-reset is an epoch timestamp on many APIs (parsing to
~1.7e9 seconds) and a misbehaving server can send any value. Before this fix
both the LLM client and the Composio client slept the raw value — a scoring
or discovery worker could freeze for hours with no way to cancel. Every
header-derived sleep is now clamped to `llm.MAX_RATE_LIMIT_WAIT_SECONDS`.
"""

import httpx
import pytest

import applypilot.discovery.composio as composio
from applypilot import llm
from applypilot.discovery.composio import TOOL_REGISTRY, ComposioClient, ComposioError

KEY = "AIzaSy-SECRET-KEY-VALUE"
API_KEY = "test-key-not-a-real-credential"

CEILING = llm.MAX_RATE_LIMIT_WAIT_SECONDS


@pytest.fixture
def clean_composio_env(monkeypatch):
    """Remove all Composio env vars so tests are hermetic."""
    for var in ("COMPOSIO_API_KEY", "COMPOSIO_TOOLKITS", "COMPOSIO_USER_ID",
                "COMPOSIO_CONNECTED_ACCOUNT_ID"):
        monkeypatch.delenv(var, raising=False)


def _llm_client(handler) -> llm.LLMClient:
    client = llm.LLMClient(llm._GEMINI_COMPAT_BASE, "gemini-2.0-flash", KEY)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_f6_llm_retry_after_is_clamped(monkeypatch):
    """429 + `Retry-After: 1000000000` must never sleep past the ceiling."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1000000000"})

    with pytest.raises(httpx.HTTPStatusError):
        _llm_client(handler).chat([{"role": "user", "content": "hi"}])

    assert sleeps, "the 429 retry path must sleep"
    assert all(0 <= s <= CEILING for s in sleeps), sleeps


def test_f6_llm_retry_after_reasonable_value_is_honored(monkeypatch):
    """A sane hint below the ceiling is still honored verbatim."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(httpx.HTTPStatusError):
        _llm_client(handler).chat([{"role": "user", "content": "hi"}])

    assert sleeps and all(s == 3.0 for s in sleeps), sleeps


def test_f6_llm_non_numeric_retry_after_falls_back_to_backoff(monkeypatch):
    """An HTTP-date or garbage hint falls back to bounded backoff, not a crash."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})

    with pytest.raises(httpx.HTTPStatusError):
        _llm_client(handler).chat([{"role": "user", "content": "hi"}])

    assert sleeps and all(0 <= s <= CEILING for s in sleeps), sleeps


def test_f6_composio_epoch_reset_is_clamped(clean_composio_env, monkeypatch):
    """x-ratelimit-reset as an epoch timestamp must clamp to the ceiling."""
    sleeps: list[float] = []
    monkeypatch.setattr(composio.time, "sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"x-ratelimit-reset": "1769999999"})

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler), backoff_base=0)
    with pytest.raises(ComposioError):
        client.execute_tool("WEB3_CAREER_LIST_JOBS", {},
                            TOOL_REGISTRY["web3_career"]["version"])
    client.close()

    assert sleeps, "the 429 retry path must sleep"
    assert all(0 <= s <= CEILING for s in sleeps), sleeps


def test_f6_composio_retry_after_is_clamped(clean_composio_env, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(composio.time, "sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "86400"})

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler), backoff_base=0)
    with pytest.raises(ComposioError):
        client.execute_tool("WEB3_CAREER_LIST_JOBS", {},
                            TOOL_REGISTRY["web3_career"]["version"])
    client.close()

    assert sleeps and all(0 <= s <= CEILING for s in sleeps), sleeps
