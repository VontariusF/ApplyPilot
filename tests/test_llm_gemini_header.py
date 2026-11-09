"""S4: the Gemini API key travels in the x-goog-api-key header, never the URL.

httpx exception text embeds the request URL, so a key in the query string
leaks into logs and error strings on every failed call.
"""

import httpx
import pytest

from kestrel import llm

KEY = "AIzaSy-SECRET-KEY-VALUE"
NATIVE_OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


def make_client(handler):
    client = llm.LLMClient(llm._GEMINI_COMPAT_BASE, "gemini-2.0-flash", KEY)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_native_gemini_key_in_header_not_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=NATIVE_OK)

    client = make_client(handler)
    client._use_native_gemini = True  # the production switch after a compat 403

    out = client.chat([{"role": "user", "content": "hi"}])

    assert out == "ok"
    assert captured["headers"].get("x-goog-api-key") == KEY
    assert KEY not in captured["url"]
    assert "key=" not in captured["url"]


def test_compat_403_fallback_sends_header_on_native_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(403, json={"error": {"message": "model unavailable"}})
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=NATIVE_OK)

    out = make_client(handler).chat([{"role": "user", "content": "hi"}])

    assert out == "ok"
    assert captured["headers"].get("x-goog-api-key") == KEY
    assert KEY not in captured["url"]


def test_native_gemini_401_error_leaks_no_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = make_client(handler)
    client._use_native_gemini = True

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])

    # The exception text (which lands in logs) must be key-free.
    assert KEY not in str(exc_info.value)
