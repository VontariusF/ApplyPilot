"""probe_provider: the bounded reachability probe behind the System Check
provider row (spec reliability fix 1).

The probe must send a minimal request through the same client real runs
use, stay bounded (no retry/sleep loop), map failures to plain language,
and never echo credential values back into check output.
"""

import httpx
import pytest

from applypilot import llm

SECRET = "sk-super-secret-12345"


class ProbeFakeClient:
    """Duck-typed LLMClient surface probe_provider touches."""

    def __init__(self, *, compat=None, native=None):
        self.api_key = SECRET
        self.compat_raises = compat
        self.native_raises = native
        self.native_called = False

    def _chat_compat(self, messages, temperature=0.0, max_tokens=1,
                     timeout=None):
        assert max_tokens == 1, "probe must request a 1-token completion"
        assert timeout == llm.PROBE_TIMEOUT_SECONDS, "probe must stay bounded"
        if self.compat_raises is not None:
            raise self.compat_raises
        return "ok"

    def _chat_native_gemini(self, messages, temperature=0.0, max_tokens=1,
                            timeout=None):
        self.native_called = True
        if self.native_raises is not None:
            raise self.native_raises
        return "ok"


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request,
                                 response=response)


@pytest.fixture()
def probed(monkeypatch):
    def install(client):
        monkeypatch.setattr(llm, "get_client", lambda: client)
        return client

    return install


def test_probe_success_is_reachable(probed):
    probed(ProbeFakeClient())
    reachable, detail = llm.probe_provider()
    assert reachable is True
    assert "answered a test request" in detail


def test_probe_unreachable_endpoint_is_plain_language(probed):
    probed(ProbeFakeClient(compat=httpx.ConnectError(
        "[Errno 8] nodename nor servname provided")))
    reachable, detail = llm.probe_provider()
    assert reachable is False
    assert "Could not reach the provider endpoint" in detail


def test_probe_timeout_names_the_bound(probed):
    probed(ProbeFakeClient(compat=httpx.ReadTimeout("timed out")))
    reachable, detail = llm.probe_provider()
    assert reachable is False
    assert "did not answer within 5 seconds" in detail


@pytest.mark.parametrize("status,fragment", [
    (401, "API key was refused"),
    (403, "API key was refused"),
    (404, "not found"),
    (429, "rate"),
    (500, "HTTP 500"),
])
def test_probe_http_status_maps_to_plain_language(probed, status, fragment):
    probed(ProbeFakeClient(compat=_http_error(status)))
    reachable, detail = llm.probe_provider()
    assert reachable is False
    assert fragment in detail


def test_probe_gemini_compat_403_follows_the_native_path(probed):
    client = probed(ProbeFakeClient(
        compat=llm._GeminiCompatForbidden(
            httpx.Response(403, request=httpx.Request("POST", "https://x"),
                           text="nope"))))
    reachable, detail = llm.probe_provider()
    assert client.native_called is True
    assert reachable is True
    assert "answered a test request" in detail


def test_probe_never_echoes_credentials(probed):
    """A hostile/leaky response body must not reach the check surface."""
    probed(ProbeFakeClient(
        compat=_http_error(401, body='{"error": "bad key ' + SECRET + '"}')))
    _, detail = llm.probe_provider()
    assert SECRET not in detail


def test_probe_without_a_provider_says_so(monkeypatch):
    def no_provider():
        raise RuntimeError(
            "No LLM provider configured. Set GEMINI_API_KEY, OPENAI_API_KEY, "
            "or LLM_URL in your environment.")

    monkeypatch.setattr(llm, "get_client", no_provider)
    reachable, detail = llm.probe_provider()
    assert reachable is False
    assert "No AI provider is configured" in detail
