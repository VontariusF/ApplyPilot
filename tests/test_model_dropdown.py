"""Deliverable A tests: model selection from GET {base}/models.

Covers the spec's named verifying tests:
  - Provider-shape tests via mocked HTTP for OpenAI, Gemini-compat,
    Ollama, and LM Studio response shapes.
  - Fallback-to-free-text when the models list is unreachable (wizard).
  - Settings merge-write still honored after the field changes.
  - Endpoint auth: /settings/models rejects unauthenticated requests.
"""

import httpx
import pytest

from kestrel.llm import ModelListError, _parse_model_ids, list_models

GEMINI_COMPAT = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENAI_BASE = "https://api.openai.com/v1"


def clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LLM_PROVIDER", "LLM_URL", "LLM_API_KEY", "LLM_MODEL",
                "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class _Capture:
    """Records the URL and headers of the (mocked) /models request."""

    def __init__(self):
        self.url = None
        self.headers = None


def _mock_http(monkeypatch, payload=None, status_code=200, exc=None, capture=None):
    """Patch llm.httpx.get with a canned response (or raised exception)."""
    def handler(url, **kwargs):
        if capture is not None:
            capture.url = url
            capture.headers = dict(kwargs.get("headers") or {})
        if exc is not None:
            raise exc
        return httpx.Response(
            status_code, json=payload, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("kestrel.llm.httpx.get", handler)


# ---------------------------------------------------------------------------
# Provider-shape tests (mocked HTTP, one per provider from the spec)
# ---------------------------------------------------------------------------

def test_openai_shape_lists_ids(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "ok123")
    capture = _Capture()
    _mock_http(monkeypatch, {
        "object": "list",
        "data": [
            {"id": "gpt-4o-mini", "object": "model", "owned_by": "system"},
            {"id": "gpt-4.1", "object": "model", "owned_by": "organization-owner"},
        ],
    }, capture=capture)

    assert list_models() == ["gpt-4o-mini", "gpt-4.1"]
    assert capture.url == f"{OPENAI_BASE}/models"
    assert capture.headers.get("Authorization") == "Bearer ok123"


def test_gemini_compat_shape_lists_bare_ids(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    capture = _Capture()
    _mock_http(monkeypatch, {
        "object": "list",
        # Compat route: bare ids, no models/ prefix.
        "data": [{"id": "gemini-2.0-flash"}, {"id": "gemini-2.5-pro"}],
    }, capture=capture)

    assert list_models() == ["gemini-2.0-flash", "gemini-2.5-pro"]
    assert capture.url == f"{GEMINI_COMPAT}/models"
    assert capture.headers.get("Authorization") == "Bearer gk123"


def test_ollama_shape_is_keyless(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_URL", "http://localhost:11434/v1")
    capture = _Capture()
    _mock_http(monkeypatch, {
        "object": "list",
        "data": [{"id": "llama3.2:latest", "owned_by": "library"}],
    }, capture=capture)

    assert list_models() == ["llama3.2:latest"]
    assert capture.url == "http://localhost:11434/v1/models"
    assert "Authorization" not in capture.headers  # local servers get no header


def test_lm_studio_shape_is_keyless(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-7b-instruct")
    capture = _Capture()
    _mock_http(monkeypatch, {
        "data": [{"id": "qwen2.5-7b-instruct"}, {"id": "mistral-7b-instruct-v0.3"}],
    }, capture=capture)

    assert list_models() == ["qwen2.5-7b-instruct", "mistral-7b-instruct-v0.3"]
    assert capture.url == "http://localhost:1234/v1/models"
    assert "Authorization" not in capture.headers


def test_custom_endpoint_with_key_sends_bearer(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_MODEL", "google/gemini-2.0-flash")
    monkeypatch.setenv("LLM_API_KEY", "ck123")
    capture = _Capture()
    _mock_http(monkeypatch, {"data": [{"id": "google/gemini-2.0-flash"}]}, capture=capture)

    assert list_models() == ["google/gemini-2.0-flash"]
    assert capture.headers.get("Authorization") == "Bearer ck123"


# ---------------------------------------------------------------------------
# Response-shape tolerance
# ---------------------------------------------------------------------------

def test_plain_list_shape_tolerated(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "ok123")
    _mock_http(monkeypatch, [{"id": "m1"}, {"id": "m2"}])

    assert list_models() == ["m1", "m2"]


def test_bare_string_entries_and_mixed_shapes(monkeypatch):
    assert _parse_model_ids([{"id": "a"}, "b", {"id": "c"}, 42, {}]) == ["a", "b", "c"]


def test_ids_are_deduplicated_and_blank_stripped():
    assert _parse_model_ids({"data": ["m1", " m1 ", "", {"id": "m2"}, {"id": "m1"}]}) == ["m1", "m2"]


def test_empty_data_list_is_valid_not_an_error():
    assert _parse_model_ids({"object": "list", "data": []}) == []


def test_unparseable_shape_raises_model_list_error():
    with pytest.raises(ModelListError, match="unexpected /models response shape"):
        _parse_model_ids({"foo": "bar"})


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_unreachable_endpoint_raises_model_list_error(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_URL", "http://localhost:11434/v1")
    _mock_http(monkeypatch, exc=httpx.ConnectError("connection refused"))

    with pytest.raises(ModelListError, match="model list request"):
        list_models()


def test_http_error_raises_model_list_error(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
    _mock_http(monkeypatch, {"error": {"message": "invalid key"}}, status_code=401)

    with pytest.raises(ModelListError, match="model list request"):
        list_models()


def test_non_json_body_raises_model_list_error(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "ok123")

    def handler(url, **kwargs):
        return httpx.Response(200, text="<html>not json</html>",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("kestrel.llm.httpx.get", handler)
    with pytest.raises(ModelListError, match="non-JSON"):
        list_models()


def test_unconfigured_provider_raises_readably(monkeypatch):
    clear_llm_env(monkeypatch)
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        list_models()


# ---------------------------------------------------------------------------
# Wizard: select from the list, fall back to free text
# ---------------------------------------------------------------------------

def _wizard_prompt_answers(monkeypatch, answers):
    """Script wizard Prompt/Confirm like the existing provider tests, and
    capture the kwargs of every Prompt.ask call."""
    from kestrel.wizard import init as wizard

    scripted = list(answers)
    calls: list[dict] = []

    class _FakePrompt:
        @classmethod
        def ask(cls, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            value = scripted.pop(0)
            if value == "" and kwargs.get("default") is not None:
                return kwargs["default"]
            return value

    class _FakeConfirm:
        @staticmethod
        def ask(*args, **kwargs):
            return True

    monkeypatch.setattr(wizard, "Prompt", _FakePrompt)
    monkeypatch.setattr(wizard, "Confirm", _FakeConfirm)
    return wizard, calls


def _patch_wizard_list(monkeypatch, wizard, result):
    """Patch the wizard's list_models import to succeed or fail on cue."""
    if isinstance(result, Exception):
        def _raise(*args, **kwargs):
            raise result
        monkeypatch.setattr(wizard, "list_models", _raise)
    else:
        monkeypatch.setattr(wizard, "list_models", lambda *a, **kw: list(result))


def test_wizard_selects_from_fetched_list(monkeypatch, tmp_path):
    wizard, calls = _wizard_prompt_answers(
        monkeypatch,
        ["custom", "http://localhost:8080/v1", "", "llama3"],
    )
    _patch_wizard_list(monkeypatch, wizard, ["llama3", "qwen2.5:7b"])
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    model_call = calls[-1]
    assert model_call["kwargs"].get("choices") == ["llama3", "qwen2.5:7b"]
    assert model_call["kwargs"].get("default") == "llama3"  # first choice when no default
    assert "LLM_MODEL='llama3'" in env_path.read_text(encoding="utf-8")


def test_wizard_falls_back_to_free_text_when_list_fails(monkeypatch, tmp_path, capsys):
    wizard, calls = _wizard_prompt_answers(
        monkeypatch,
        ["custom", "http://localhost:8080/v1", "", "my-typed-model"],
    )
    _patch_wizard_list(monkeypatch, wizard, ModelListError("connection refused"))
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    model_call = calls[-1]
    assert "choices" not in model_call["kwargs"]  # free text, not a select
    assert "LLM_MODEL='my-typed-model'" in env_path.read_text(encoding="utf-8")
    # One-line warning (whitespace-normalized: rich may wrap the console line).
    assert "enter the model id manually" in " ".join(capsys.readouterr().out.split())


def test_wizard_falls_back_when_list_is_empty(monkeypatch, tmp_path, capsys):
    wizard, _ = _wizard_prompt_answers(
        monkeypatch,
        ["local", "", "typed-anyway"],
    )
    _patch_wizard_list(monkeypatch, wizard, [])
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    assert "LLM_MODEL='typed-anyway'" in env_path.read_text(encoding="utf-8")
    assert "returned no models" in " ".join(capsys.readouterr().out.split())


def test_wizard_keeps_default_when_default_in_choices(monkeypatch, tmp_path):
    wizard, _ = _wizard_prompt_answers(
        monkeypatch,
        ["gemini", "gk999", ""],  # empty answer -> default
    )
    _patch_wizard_list(monkeypatch, wizard, ["gemini-2.0-flash", "gemini-2.5-pro"])
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    assert "LLM_MODEL='gemini-2.0-flash'" in env_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Portal: /settings/models endpoint, datalist render, merge-write
# ---------------------------------------------------------------------------

def test_settings_models_requires_auth(portal_client):
    from fastapi.testclient import TestClient

    client, app = portal_client
    anonymous = TestClient(app, base_url="http://127.0.0.1")
    r = anonymous.get("/settings/models")
    assert r.status_code == 401


def test_settings_models_returns_ids(portal_client, monkeypatch):
    from kestrel.portal import app as portal_app

    client, _ = portal_client
    monkeypatch.setattr(portal_app, "list_models", lambda **kw: ["m1", "m2"])
    r = client.get("/settings/models")
    assert r.status_code == 200
    assert r.json() == {"models": ["m1", "m2"]}


def test_settings_models_error_returns_502_with_empty_list(portal_client, monkeypatch):
    from kestrel.portal import app as portal_app

    client, _ = portal_client
    monkeypatch.setattr(
        portal_app, "list_models",
        lambda **kw: (_ for _ in ()).throw(ModelListError("down")),
    )
    r = client.get("/settings/models")
    assert r.status_code == 502
    assert r.json()["models"] == []
    assert "down" in r.json()["error"]


def test_settings_page_renders_populated_datalists(portal_client, monkeypatch):
    from kestrel.portal import app as portal_app

    client, _ = portal_client
    monkeypatch.setattr(portal_app, "list_models", lambda **kw: ["qwen2.5:7b", "llama3"])
    page = client.get("/settings").text
    assert page.count('<datalist id="dl-') == 2  # LLM_MODEL + AUTOAPPLY_MODEL
    assert 'list="dl-LLM_MODEL"' in page
    assert 'list="dl-AUTOAPPLY_MODEL"' in page
    assert '<option value="qwen2.5:7b"></option>' in page
    assert "Refresh model list" in page


def test_settings_page_degrades_to_free_text_when_unreachable(portal_client, monkeypatch):
    from kestrel.portal import app as portal_app

    client, _ = portal_client
    monkeypatch.setattr(
        portal_app, "list_models",
        lambda **kw: (_ for _ in ()).throw(ModelListError("unreachable")),
    )
    page = client.get("/settings").text
    assert 'list="dl-LLM_MODEL"' in page  # input stays typable
    assert '<option value="qwen2.5:7b"></option>' not in page  # but no bogus options


def test_settings_merge_write_still_honored_with_model_fields(portal_client):
    from kestrel import config
    from kestrel.portal import settings as settings_mod

    client, _ = portal_client
    config.ENV_PATH.write_text(
        "# my comment\nUNKNOWN_VAR=keepme\nAUTOAPPLY_ENGINE=droid\n",
        encoding="utf-8",
    )
    client.post("/settings", data={
        "LLM_MODEL": "qwen2.5:7b",
        "AUTOAPPLY_MODEL": "claude-sonnet-4-5",
        "AUTOAPPLY_ENGINE": "claude",
    })
    values = settings_mod.read_env_file()
    assert values["LLM_MODEL"] == "qwen2.5:7b"
    assert values["AUTOAPPLY_MODEL"] == "claude-sonnet-4-5"
    assert values["AUTOAPPLY_ENGINE"] == "claude"
    raw = config.ENV_PATH.read_text(encoding="utf-8")
    assert "# my comment" in raw
    assert "UNKNOWN_VAR=keepme" in raw


# ---------------------------------------------------------------------------
# P1-2: the saved default apply model governs runs that name no model
# ---------------------------------------------------------------------------

def test_default_apply_model_reads_env(monkeypatch):
    from kestrel import config

    monkeypatch.setenv("AUTOAPPLY_MODEL", "gpt-4o-mini")
    assert config.default_apply_model() == "gpt-4o-mini"


def test_default_apply_model_unset_or_empty_falls_back(monkeypatch):
    """Both an unset setting and a cleared one fall back to 'haiku' — a
    cleared field must not put an empty --model on the apply argv."""
    from kestrel import config

    monkeypatch.delenv("AUTOAPPLY_MODEL", raising=False)
    assert config.default_apply_model() == "haiku"
    monkeypatch.setenv("AUTOAPPLY_MODEL", "")
    assert config.default_apply_model() == "haiku"

