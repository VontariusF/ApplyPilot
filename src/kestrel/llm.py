# SPDX-License-Identifier: AGPL-3.0-only
"""
Unified LLM client for Kestrel.

Provider resolution order:
  1. LLM_PROVIDER env var, when set — an explicit, validated choice:
       gemini -> Google Gemini compat endpoint (requires GEMINI_API_KEY)
       openai -> OpenAI (requires OPENAI_API_KEY)
       custom -> any OpenAI-compatible endpoint (requires LLM_URL + LLM_MODEL,
                 optional LLM_API_KEY for endpoints that need no bearer auth)
       local  -> local endpoint like Ollama/llama.cpp (requires LLM_URL)
  2. Legacy env sniffing, when LLM_PROVIDER is unset (unchanged behavior):
       GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
       OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
       LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)


class LLMParseError(RuntimeError):
    """Raised when an LLM response cannot be parsed into assistant text (B8/B9).

    Carries a readable description of the response shape instead of letting a
    raw KeyError/IndexError/AttributeError escape from nested dict walks.
    """

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

# Explicit LLM_PROVIDER values, in wizard order.
_VALID_PROVIDERS = ("gemini", "openai", "custom", "local")

# Well-known base URLs used to label providers that were resolved by
# legacy env sniffing (LLM_PROVIDER unset).
_OPENAI_BASE = "https://api.openai.com/v1"


def _detect_provider() -> tuple[str, str, str | None]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.

    An explicit LLM_PROVIDER value is a contract: missing required variables
    raise a RuntimeError naming the missing variable — never a silent
    fallthrough to another provider. When LLM_PROVIDER is unset, the legacy
    env-sniffing order applies unchanged.
    """
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()

    if explicit:
        if explicit not in _VALID_PROVIDERS:
            raise RuntimeError(
                f"LLM_PROVIDER='{explicit}' is not recognized. "
                f"Valid values: {', '.join(_VALID_PROVIDERS)}."
            )

        model_override = os.environ.get("LLM_MODEL", "")

        if explicit == "custom":
            base = os.environ.get("LLM_URL", "")
            missing = [
                name
                for name, value in (("LLM_URL", base), ("LLM_MODEL", model_override))
                if not value
            ]
            if missing:
                raise RuntimeError(
                    f"LLM_PROVIDER=custom requires {', '.join(missing)} to be set."
                )
            # Bearer auth is optional: llama.cpp-class endpoints need no key.
            return base.rstrip("/"), model_override, os.environ.get("LLM_API_KEY", "") or None

        if explicit == "local":
            base = os.environ.get("LLM_URL", "")
            if not base:
                raise RuntimeError(
                    "LLM_PROVIDER=local requires LLM_URL to be set "
                    "(e.g. http://localhost:11434/v1)."
                )
            return base.rstrip("/"), model_override or "local-model", os.environ.get("LLM_API_KEY", "") or None

        # explicit in ("gemini", "openai")
        key_var = "GEMINI_API_KEY" if explicit == "gemini" else "OPENAI_API_KEY"
        key = os.environ.get(key_var, "")
        if not key:
            raise RuntimeError(f"LLM_PROVIDER={explicit} requires {key_var} to be set.")
        base = _GEMINI_COMPAT_BASE if explicit == "gemini" else _OPENAI_BASE
        model = model_override or ("gemini-2.0-flash" if explicit == "gemini" else "gpt-4o-mini")
        return base, model, key

    # Legacy fallback: env sniffing, unchanged.
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            _GEMINI_COMPAT_BASE,
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            _OPENAI_BASE,
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


def mask_key(api_key: str | None) -> str:
    """Mask an API key for display: never reveal more than the last 4 characters."""
    if not api_key:
        return "(not set)"
    if len(api_key) <= 4:
        return "(set)"
    return f"(ending {api_key[-4:]})"


class ModelListError(RuntimeError):
    """Raised when GET {base}/models fails or returns an unparseable shape.

    Callers (wizard, portal) fall back to free-text model entry with a
    one-line warning — the id is always typable, the list is an affordance.
    """


def _parse_model_ids(payload: object) -> list[str]:
    """Extract model ids from an OpenAI-style or plain-list response.

    Tolerated shapes: {"data": [{"id": ...}, ...]}, [{"id": ...}, ...], and
    bare string entries. Gemini's OpenAI-compat route returns bare ids in
    data[].id, so no models/ prefix handling is needed.
    """
    if isinstance(payload, dict):
        entries = payload.get("data")
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = None
    if not isinstance(entries, list):
        raise ModelListError(
            f"unexpected /models response shape: {str(payload)[:120]}"
        )

    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            candidate = entry["id"]
        else:
            continue
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            ids.append(candidate)
    return ids


def list_models(
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Fetch the model ids offered at GET {base}/models.

    Resolves the currently configured provider's base when base_url is not
    given (the same resolution chat uses, so the list always matches the
    endpoint that will serve the model). Local servers are keyless: a
    header goes out only when a key is available. api_key=None means "use
    the detected key"; pass "" to force keyless against an explicit base.

    Raises ModelListError on any HTTP, connection, or parse failure —
    never returns a guessed partial list.
    """
    if base_url is None:
        base_url, _model, detected_key = _detect_provider()
        if api_key is None:
            api_key = detected_key

    url = f"{base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except ValueError as exc:
        raise ModelListError(f"{url} returned a non-JSON body: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ModelListError(f"model list request to {url} failed: {exc}") from exc
    return _parse_model_ids(payload)


def _provider_label(base_url: str) -> str:
    """Human-readable provider name for logs and diagnostics."""
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if base_url == _GEMINI_COMPAT_BASE:
        return "gemini"
    if base_url == _OPENAI_BASE:
        return "openai"
    return "custom/local"


def describe_provider() -> dict[str, str]:
    """Resolve the active provider for diagnostics (doctor, portal).

    Raises the same RuntimeError as _detect_provider when nothing is
    configured. The API key is masked — only a last-4 hint is returned.
    """
    base_url, model, api_key = _detect_provider()
    return {
        "provider": _provider_label(base_url),
        "base_url": base_url,
        "model": model,
        "key_hint": mask_key(api_key),
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10

# Ceiling on any single rate-limit sleep, including server-suggested
# Retry-After / x-ratelimit-reset values. A header hint is advisory: many
# APIs send x-ratelimit-reset as an epoch timestamp (which parses to ~1.7e9
# seconds) and a misbehaving server can send anything. Honoring a too-short
# wait costs one extra 429 round-trip on the next retry; honoring a huge one
# stalls the worker for hours with no way to cancel.
MAX_RATE_LIMIT_WAIT_SECONDS: float = 120.0


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        provider_name: str = "",
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.provider_name = provider_name
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                # S4: the key must ride in a header, never the URL query
                # string -- httpx exception text (raise_for_status) embeds the
                # request URL, which then lands in logs and error strings.
                "x-goog-api-key": self.api_key,
            },
            **_timeout_kwargs(timeout),
        )
        resp.raise_for_status()
        data = resp.json()

        # B8: safety-blocked responses have no candidates; tool-call or
        # functionCall parts carry no text. Raise a readable error instead of
        # a raw IndexError/KeyError from the dict walk.
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMParseError(
                "Gemini response contained no candidates (likely safety-blocked): "
                f"{str(data)[:200]}"
            )

        first = candidates[0] if isinstance(candidates[0], dict) else {}
        parts = (first.get("content") or {}).get("parts") or []
        texts = [
            p["text"] for p in parts
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
        if not texts:
            raise LLMParseError(
                "Gemini response contained no text parts (e.g. functionCall or "
                f"empty part): {str(data)[:200]}"
            )
        return "".join(texts)

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
            **_timeout_kwargs(timeout),
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        # B8: tool-call/refusal responses return content: null. Return a
        # readable error rather than None (which crashed downstream .strip()).
        if not isinstance(content, str):
            raise LLMParseError(
                "OpenAI-compatible response had null/non-text message content "
                f"(likely a tool call or refusal): {str(data)[:200]}"
            )
        return content

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        # B9: first message content may be None (structured content) — guard
        # instead of raising AttributeError before any network call.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            first_content = first.get("content") if isinstance(first, dict) else None
            if (
                first.get("role") == "user"
                and isinstance(first_content, str)
                and not first_content.startswith("/no_think")
            ):
                messages = [{"role": first["role"], "content": f"/no_think\n{first_content}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                        wait = min(max(wait, 0.0), MAX_RATE_LIMIT_WAIT_SECONDS)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s) on %s. Waiting %ds before retry %d/%d.",
                        resp.status_code,
                        self.provider_name or "LLM endpoint",
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def is_infra_error(exc: BaseException) -> bool:
    """True when `exc` is a transport-class failure a provider recovery outlives.

    Connection errors, timeouts, and HTTP 5xx are provider-side problems:
    the request never produced a content-level verdict (reliability sweep
    P1-1). Per-job attempt budgets must not burn on them — otherwise one
    outage converts every queued job's full attempt budget into "failed"
    before the provider comes back. Everything else — malformed 200s,
    validation failures, 4xx refusals — is content-level: retrying with a
    fresh budget cannot help, so the normal burn applies.
    """
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, httpx.HTTPError):
        # ConnectError and friends: the request never landed at all.
        return True
    return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None
_instance_lock = threading.Lock()


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton.

    B15: guarded by a module lock so concurrent first use (parallel
    enrichment workers) constructs exactly one client.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:  # re-check inside the lock
                base_url, model, api_key = _detect_provider()
                label = _provider_label(base_url)
                log.info("LLM provider: %s (%s)  model: %s", label, base_url, model)
                _instance = LLMClient(base_url, model, api_key, provider_name=label)
    return _instance


# ---------------------------------------------------------------------------
# Reachability probe (System Check)
# ---------------------------------------------------------------------------

# How long the System Check provider probe waits before declaring the
# endpoint unreachable. Deliberately far below _TIMEOUT: the check page
# renders this inline and must never hang on a dead endpoint.
PROBE_TIMEOUT_SECONDS: float = 5.0

# Second, longer bound for the probe's single retry: a slow-but-working
# endpoint (a cold local model loading, a free-tier queue) answers past
# the check's fast bound but under what real runs allow. Retrying once
# before reporting keeps that case out of the dead-endpoint FAIL.
PROBE_SLOW_RETRY_SECONDS: float = 30.0

# A 1-token completion is the cheapest request that exercises the exact
# path a real apply run takes (same endpoint, same auth, same parsing).
_PROBE_MAX_TOKENS = 1


def _timeout_kwargs(timeout: float | None) -> dict:
    """Per-request httpx kwargs for an optional timeout override.

    Passing timeout=None to httpx DISABLES the client's configured
    timeout entirely — omit the argument instead so normal chat() calls
    inherit the client default; the probe passes an explicit short bound.
    """
    return {"timeout": timeout} if timeout is not None else {}


def probe_provider(timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Send one minimal chat request to the configured provider.

    The probe runs through the same client singleton (get_client) and the
    same request/parsing path a real run uses — env-var presence proves
    nothing on its own; a mispointed custom endpoint must show red here,
    not silently fail every later apply run.

    Returns:
        (state, detail): state is "reachable" (the endpoint answered and
        the response parsed), "degraded" (it answered but the response
        was unusable — real runs may still fail), "slow" (it did not
        answer within the retry bound, but real runs wait longer — a
        warning, not an outage), or "unreachable".
        detail is one plain-language sentence. It may name HTTP status
        codes and endpoint hosts but never echoes credential values —
        request headers are never included in any error text, and
        response bodies are never quoted back.

    Bounded: one request per attempt (at most two) with a per-request
    timeout, no chat()'s retry/sleep loop — the System Check page
    renders this inline.
    """
    try:
        client = get_client()
    except RuntimeError as exc:
        # _detect_provider's error names missing env vars, never values.
        return "unreachable", f"No AI provider is configured — {exc}"

    messages = [{"role": "user", "content": "Reply with the single word: ok"}]

    def _attempt(bound: float) -> None:
        try:
            client._chat_compat(messages, temperature=0.0,
                                max_tokens=_PROBE_MAX_TOKENS, timeout=bound)
        except _GeminiCompatForbidden:
            # Exactly what chat() does on a Gemini compat 403: switch to
            # the native API. Reachability follows whichever path works.
            client._chat_native_gemini(messages, temperature=0.0,
                                       max_tokens=_PROBE_MAX_TOKENS,
                                       timeout=bound)

    # Two bounds: the check's fast bound first, then one longer attempt.
    # A timeout at the fast bound is not evidence the endpoint is down —
    # reporting the mistyped-URL FAIL for a merely slow provider sent
    # users chasing the wrong problem while scheduled runs succeeded.
    for attempt, bound in enumerate((timeout, PROBE_SLOW_RETRY_SECONDS)):
        try:
            _attempt(bound)
        except httpx.TimeoutException:
            if attempt == 0:
                continue
            return "slow", (
                f"The provider did not answer within {bound:g} seconds, but "
                f"real runs wait up to {int(_TIMEOUT)} seconds — the endpoint "
                "may be slow rather than down. If runs also fail, check the "
                "endpoint URL and model in provider settings.")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                return "unreachable", (
                    f"The provider rejected the test request (HTTP {code}) — "
                    "the API key was refused. Check it in provider settings.")
            if code == 404:
                return "unreachable", (
                    "The provider answered, but the endpoint or model was not "
                    "found (HTTP 404) — the URL or model name looks wrong.")
            if code == 429:
                return "unreachable", (
                    "The provider is reachable but refused the test request "
                    "(HTTP 429) — out of quota or rate-limited right now.")
            return "unreachable", (
                f"The provider rejected the test request (HTTP {code}). "
                "Check the endpoint URL and model in provider settings.")
        except httpx.HTTPError as exc:
            # ConnectError / protocol errors: message names the host and OS
            # errno — never headers or credentials.
            detail = str(exc).strip() or exc.__class__.__name__
            return "unreachable", (
                f"Could not reach the provider endpoint — {detail}. "
                "Check the endpoint URL in provider settings.")
        except (LLMParseError, KeyError, ValueError, IndexError, TypeError):
            # An HTTP 200 with a body the parser disliked (missing or empty
            # choices, non-text content): the endpoint is alive and answering
            # — degraded, not dead. Real runs may still fail parsing.
            return "degraded", (
                "The provider answered the test request, but the response was "
                "not usable — real runs may still fail. Check the model name "
                "in provider settings.")
        if attempt == 0:
            return "reachable", "The provider answered a test request."
        return "reachable", (
            f"The provider answered a test request — slowly (over "
            f"{timeout:g} seconds). Real runs wait up to {int(_TIMEOUT)} "
            "seconds, so runs can still succeed.")
    raise AssertionError("unreachable: probe loop must return on every path")


def reset_client() -> None:
    """Drop the singleton so the next get_client() re-reads the environment.

    Settings saves call this after refreshing os.environ (S2-2 / sweep F7):
    a saved provider, model, or key must reach the next scoring/rescore
    call without a portal restart. The dropped client is deliberately not
    close()d — an in-flight chat may still hold it, and closing under its
    feet would turn a save into a spurious request error; the abandoned
    connection pool is reclaimed by GC instead.
    """
    global _instance
    with _instance_lock:
        _instance = None

