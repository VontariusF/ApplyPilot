"""probe_provider: the bounded reachability probe behind the System Check
provider row (spec reliability fix 1).

The probe must send a minimal request through the same client real runs
use, stay bounded (no retry/sleep loop), map failures to plain language,
never echo credential values back into check output — and it must not
change timeout behavior for normal chat() calls.
"""

import httpx
import pytest

from applypilot import llm

SECRET = "sk-super-secret-12345"


def _ok_response(url: str = "https://example.invalid/v1/chat/completions",
                 body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200, request=httpx.Request("POST", url),
        json=body if body is not None
        else {"choices": [{"message": {"content": "ok"}}]},
    )


class ProbeFakeClient:
    """Duck-typed LLMClient surface probe_provider touches.

    `compat` / `native` are: an Exception to raise, an httpx.Response to
    parse through the real `_handle_compat_response`, or None to succeed.
    """

    def __init__(self, *, compat=None, native=None):
        self.api_key = SECRET
        # compat may be a single spec or an ordered list consumed one per
        # probe call (the slow-retry path calls twice: fast bound, slow bound).
        self.compat = list(compat) if isinstance(compat, list) else [compat]
        self.native = native
        self.native_called = False
        self.timeouts = []
        self._compat_calls = 0

    def _chat_compat(self, messages, temperature=0.0, max_tokens=1,
                     timeout=None):
        assert max_tokens == 1, "probe must request a 1-token completion"
        assert timeout in (llm.PROBE_TIMEOUT_SECONDS,
                           llm.PROBE_SLOW_RETRY_SECONDS), "probe must stay bounded"
        self.timeouts.append(timeout)
        spec = (self.compat[self._compat_calls]
                if self._compat_calls < len(self.compat)
                else self.compat[-1])
        self._compat_calls += 1
        return self._run("compat", spec)

    def _chat_native_gemini(self, messages, temperature=0.0, max_tokens=1,
                            timeout=None):
        self.native_called = True
        return self._run("native", self.native)

    def _run(self, which, spec):
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, httpx.Response):
            return llm.LLMClient._handle_compat_response(spec)
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
    state, detail = llm.probe_provider()
    assert state == "reachable"
    assert "answered a test request" in detail


def test_probe_unreachable_endpoint_is_plain_language(probed):
    probed(ProbeFakeClient(compat=httpx.ConnectError(
        "[Errno 8] nodename nor servname provided")))
    state, detail = llm.probe_provider()
    assert state == "unreachable"
    assert "Could not reach the provider endpoint" in detail


def test_probe_double_timeout_is_slow_not_unreachable(probed):
    """A slow-but-working provider is a warning, not the mistyped-URL FAIL
    (reliability sweep P2-5): a timeout at the fast bound is retried once at
    the slow bound before the check gives up."""
    probed_client = probed(ProbeFakeClient(compat=httpx.ReadTimeout("timed out")))
    state, detail = llm.probe_provider()
    assert state == "slow"
    assert f"did not answer within {llm.PROBE_SLOW_RETRY_SECONDS:g} seconds" in detail
    assert f"real runs wait up to {int(llm._TIMEOUT)} seconds" in detail
    assert len(probed_client.timeouts) == 2  # fast bound, then the slow retry


def test_probe_slow_then_answer_is_reachable_with_a_slow_note(probed):
    """First bound times out, second succeeds: reachable, but the detail
    says the endpoint answered slowly so users can tell runs may lag."""
    probed(ProbeFakeClient(
        compat=[httpx.ReadTimeout("timed out"), None]))
    state, detail = llm.probe_provider()
    assert state == "reachable"
    assert "slowly" in detail
    assert "runs can still succeed" in detail


@pytest.mark.parametrize("status,fragment", [
    (401, "API key was refused"),
    (403, "API key was refused"),
    (404, "not found"),
    (429, "rate"),
    (500, "HTTP 500"),
])
def test_probe_http_status_maps_to_plain_language(probed, status, fragment):
    probed(ProbeFakeClient(compat=_http_error(status)))
    state, detail = llm.probe_provider()
    assert state == "unreachable"
    assert fragment in detail


@pytest.mark.parametrize("body", [
    {"choices": []},          # empty choices -> IndexError
    {"choices": None},        # null choices -> TypeError
    {"choices": [{"message": {"content": None}}]},  # non-text -> LLMParseError
])
def test_probe_malformed_200_is_degraded_not_reachable(probed, body):
    probed(ProbeFakeClient(compat=_ok_response(body=body)))
    state, detail = llm.probe_provider()
    assert state == "degraded"
    assert "real runs may still fail" in detail


def test_probe_gemini_compat_403_follows_the_native_path(probed):
    client = probed(ProbeFakeClient(
        compat=llm._GeminiCompatForbidden(
            httpx.Response(403, request=httpx.Request("POST", "https://x"),
                           text="nope"))))
    state, detail = llm.probe_provider()
    assert client.native_called is True
    assert state == "reachable"
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
    state, detail = llm.probe_provider()
    assert state == "unreachable"
    assert "No AI provider is configured" in detail


# ---------------------------------------------------------------------------
# Timeout preservation for normal chat() calls (Codex P1)
# ---------------------------------------------------------------------------


class PostCapture:
    """Replaces LLMClient._client; records post() kwargs."""

    def __init__(self):
        self.kwargs = None

    def post(self, url, **kwargs):
        self.kwargs = kwargs
        return _ok_response(url)


def _client_with_capture() -> tuple[llm.LLMClient, PostCapture]:
    client = llm.LLMClient(base_url="https://example.invalid/v1",
                           model="test-model", api_key=SECRET)
    capture = PostCapture()
    client._client = capture
    return client, capture


def test_chat_omitting_timeout_inherits_the_client_default():
    """chat() must not forward an explicit timeout=None — httpx reads that
    as 'no timeout' and disables the client's configured bound entirely."""
    client, capture = _client_with_capture()
    client.chat([{"role": "user", "content": "hi"}])
    assert "timeout" not in capture.kwargs


def test_probe_request_carries_the_explicit_bound():
    client, capture = _client_with_capture()
    client._chat_compat([{"role": "user", "content": "hi"}],
                        temperature=0.0, max_tokens=1,
                        timeout=llm.PROBE_TIMEOUT_SECONDS)
    assert capture.kwargs["timeout"] == llm.PROBE_TIMEOUT_SECONDS


def test_timeout_kwargs_sentinel():
    assert llm._timeout_kwargs(None) == {}
    assert llm._timeout_kwargs(5.0) == {"timeout": 5.0}
