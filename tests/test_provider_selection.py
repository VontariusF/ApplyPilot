"""Deliverable A tests: explicit LLM_PROVIDER selection.

Covers the four acceptance tests from the spec:
  1. LLM_PROVIDER resolution matrix — every value × present/missing keys
     either resolves or raises a RuntimeError naming the missing variable
     (no silent fallthrough).
  2. Legacy fallback equivalence — with LLM_PROVIDER unset, today's
     env-sniffing order is behaviorally unchanged.
  3. Wizard custom-branch write — writes LLM_URL/LLM_API_KEY/LLM_MODEL at
     mode 0600.
  4. Doctor masked output — a full API key never appears in doctor output.
"""

import stat

import pytest

from applypilot import config
from applypilot.llm import ModelListError, _detect_provider, describe_provider, mask_key

GEMINI_COMPAT = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENAI_BASE = "https://api.openai.com/v1"

LLM_ENV_VARS = (
    "LLM_PROVIDER",
    "LLM_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
)


def clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 2. Legacy fallback equivalence (LLM_PROVIDER unset)
# ---------------------------------------------------------------------------

LEGACY_CASES = [
    # (env dict, expected tuple or None for "raises RuntimeError")
    ({"GEMINI_API_KEY": "gk123"}, (GEMINI_COMPAT, "gemini-2.0-flash", "gk123")),
    ({"OPENAI_API_KEY": "ok123"}, (OPENAI_BASE, "gpt-4o-mini", "ok123")),
    ({"LLM_URL": "http://localhost:8080/v1"}, ("http://localhost:8080/v1", "local-model", "")),
    # Local URL wins over keys in both orders.
    ({"GEMINI_API_KEY": "gk123", "LLM_URL": "http://x:9000"}, ("http://x:9000", "local-model", "")),
    ({"OPENAI_API_KEY": "ok123", "LLM_URL": "http://x:9000"}, ("http://x:9000", "local-model", "")),
    # Gemini key wins when both keys are present.
    ({"GEMINI_API_KEY": "gk123", "OPENAI_API_KEY": "ok123"}, (GEMINI_COMPAT, "gemini-2.0-flash", "gk123")),
    # Model override applies to every provider.
    ({"GEMINI_API_KEY": "gk123", "LLM_MODEL": "m1"}, (GEMINI_COMPAT, "m1", "gk123")),
    ({"OPENAI_API_KEY": "ok123", "LLM_MODEL": "m1"}, (OPENAI_BASE, "m1", "ok123")),
    ({"LLM_URL": "http://x", "LLM_MODEL": "m1"}, ("http://x", "m1", "")),
    ({}, None),
]


@pytest.mark.parametrize(("env", "expected"), LEGACY_CASES)
def test_legacy_fallback_unchanged(monkeypatch, env, expected):
    clear_llm_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    if expected is None:
        with pytest.raises(RuntimeError, match="No LLM provider configured"):
            _detect_provider()
    else:
        assert _detect_provider() == expected


def test_legacy_fallback_trailing_slash_stripped(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_URL", "http://localhost:11434/v1/")
    base, _, _ = _detect_provider()
    assert base == "http://localhost:11434/v1"


# ---------------------------------------------------------------------------
# 1. LLM_PROVIDER resolution matrix
# ---------------------------------------------------------------------------

def test_explicit_gemini_resolves(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    assert _detect_provider() == (GEMINI_COMPAT, "gemini-2.0-flash", "gk123")


def test_explicit_openai_resolves(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "ok123")
    assert _detect_provider() == (OPENAI_BASE, "gpt-4o-mini", "ok123")


def test_explicit_provider_is_case_insensitive(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", " CUSTOM ")
    monkeypatch.setenv("LLM_URL", "http://ep:8080/v1/")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    base, model, key = _detect_provider()
    assert (base, model) == ("http://ep:8080/v1", "my-model")
    assert key is None


def test_explicit_custom_requires_llm_url(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")  # present but must NOT be used
    with pytest.raises(RuntimeError, match="LLM_PROVIDER=custom requires LLM_URL"):
        _detect_provider()


def test_explicit_custom_requires_llm_model(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "http://ep:8080/v1")
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        _detect_provider()


def test_explicit_custom_reports_all_missing_vars(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    with pytest.raises(RuntimeError, match="LLM_URL, LLM_MODEL"):
        _detect_provider()


def test_explicit_custom_optional_key(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "http://ep:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.setenv("LLM_API_KEY", "ck123")
    assert _detect_provider() == ("http://ep:8080/v1", "my-model", "ck123")


def test_explicit_custom_never_falls_through_to_gemini(monkeypatch):
    """A custom provider with a missing var must error, not use the Gemini key."""
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    with pytest.raises(RuntimeError):
        _detect_provider()


def test_explicit_gemini_never_falls_through_to_openai(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("OPENAI_API_KEY", "ok123")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _detect_provider()


def test_explicit_openai_never_falls_through_to_gemini(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _detect_provider()


def test_explicit_gemini_missing_key_raises_naming_variable(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _detect_provider()


def test_explicit_local_resolves(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_URL", "http://localhost:11434/v1")
    base, model, key = _detect_provider()
    assert (base, model) == ("http://localhost:11434/v1", "local-model")
    assert key is None


def test_explicit_local_with_model_and_key(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3")
    monkeypatch.setenv("LLM_API_KEY", "lk123")
    assert _detect_provider() == ("http://localhost:11434/v1", "llama3", "lk123")


def test_explicit_local_requires_llm_url(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER=local requires LLM_URL"):
        _detect_provider()


def test_explicit_unknown_provider_raises(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(RuntimeError, match="Valid values: gemini, openai, custom, local"):
        _detect_provider()


def test_empty_llm_provider_uses_legacy_fallback(monkeypatch):
    """LLM_PROVIDER='' or whitespace behaves as unset."""
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "   ")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    assert _detect_provider() == (GEMINI_COMPAT, "gemini-2.0-flash", "gk123")


# ---------------------------------------------------------------------------
# Diagnostics: describe_provider / mask_key
# ---------------------------------------------------------------------------

def test_mask_key_never_reveals_full_key():
    long_key = "sk-ant-api03-abcdef1234567890"
    masked = mask_key(long_key)
    assert masked == f"(ending {long_key[-4:]})"
    assert long_key not in masked
    assert "abcdef" not in masked


def test_mask_key_short_and_empty():
    assert mask_key("abc") == "(set)"
    assert mask_key("abcd") == "(set)"
    assert mask_key("") == "(not set)"
    assert mask_key(None) == "(not set)"


def test_describe_provider_masks_key(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_MODEL", "google/gemini-2.0-flash")
    monkeypatch.setenv("LLM_API_KEY", "sk-or-secret-123456")

    info = describe_provider()
    assert info["provider"] == "custom"
    assert info["base_url"] == "https://openrouter.ai/api/v1"
    assert info["model"] == "google/gemini-2.0-flash"
    assert info["key_hint"] == "(ending 3456)"
    assert "sk-or-secret-123456" not in info["key_hint"]


def test_describe_provider_labels_legacy_gemini(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    info = describe_provider()
    assert info["provider"] == "gemini"
    assert info["key_hint"] == "(ending k123)"


def test_describe_provider_raises_when_unconfigured(monkeypatch):
    clear_llm_env(monkeypatch)
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        describe_provider()


# ---------------------------------------------------------------------------
# get_tier / check_tier are provider-aware
# ---------------------------------------------------------------------------

def test_get_tier_custom_provider_with_url(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "http://ep:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # No claude/chrome in the test environment -> tier 2, not 1.
    assert config._llm_configured() is True


def test_get_tier_custom_without_url_ignores_other_keys(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    assert config._llm_configured() is False


def test_get_tier_legacy_keys_still_count(monkeypatch):
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gk123")
    assert config._llm_configured() is True


# ---------------------------------------------------------------------------
# 3. Wizard custom/local branches
# ---------------------------------------------------------------------------

class _FakeConfirm:
    @staticmethod
    def ask(*args, **kwargs):
        return True


def _install_wizard_fakes(monkeypatch, answers):
    from applypilot.wizard import init as wizard

    scripted = list(answers)

    class _FakePrompt:
        @classmethod
        def ask(cls, *args, **kwargs):
            value = scripted.pop(0)
            # Mirror rich.prompt.Prompt: empty input falls back to the default.
            if value == "" and kwargs.get("default") is not None:
                return kwargs["default"]
            return value

    monkeypatch.setattr(wizard, "Prompt", _FakePrompt)
    monkeypatch.setattr(wizard, "Confirm", _FakeConfirm)
    # Hermetic: the model-select step must not touch the network — force the
    # free-text fallback these tests were written against.
    def _no_model_list(*args, **kwargs):
        raise ModelListError("mocked unreachable")

    monkeypatch.setattr(wizard, "list_models", _no_model_list)
    return wizard


def test_wizard_custom_branch_writes_env_at_0600(monkeypatch, tmp_path):
    wizard = _install_wizard_fakes(
        monkeypatch,
        ["custom", "https://openrouter.ai/api/v1/", "sk-or-secret-123456", "google/gemini-2.0-flash"],
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    text = env_path.read_text(encoding="utf-8")
    assert "LLM_PROVIDER='custom'" in text
    assert "LLM_URL='https://openrouter.ai/api/v1/'" in text
    assert "LLM_API_KEY='sk-or-secret-123456'" in text
    assert "LLM_MODEL='google/gemini-2.0-flash'" in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_wizard_custom_branch_without_key_omits_line(monkeypatch, tmp_path):
    wizard = _install_wizard_fakes(
        monkeypatch,
        ["custom", "http://localhost:8080/v1", "", "qwen2.5-7b-instruct"],
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    text = env_path.read_text(encoding="utf-8")
    assert "LLM_PROVIDER='custom'" in text
    assert "LLM_URL='http://localhost:8080/v1'" in text
    assert "LLM_MODEL='qwen2.5-7b-instruct'" in text
    assert "LLM_API_KEY" not in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_wizard_local_branch_writes_env_at_0600(monkeypatch, tmp_path):
    wizard = _install_wizard_fakes(monkeypatch, ["local", "", ""])  # both defaults
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    text = env_path.read_text(encoding="utf-8")
    assert "LLM_PROVIDER='local'" in text
    assert "LLM_URL='http://localhost:11434/v1'" in text
    assert "LLM_MODEL='local-model'" in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_wizard_gemini_branch_still_works(monkeypatch, tmp_path):
    wizard = _install_wizard_fakes(monkeypatch, ["gemini", "gk999", ""])
    env_path = tmp_path / ".env"
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._setup_ai_features()

    text = env_path.read_text(encoding="utf-8")
    assert "LLM_PROVIDER='gemini'" in text
    assert "GEMINI_API_KEY='gk999'" in text
    assert "LLM_MODEL='gemini-2.0-flash'" in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 4. Doctor masked output
# ---------------------------------------------------------------------------

def test_doctor_never_prints_full_key(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from applypilot.cli import app

    secret = "sk-test-secret-key-123456"
    monkeypatch.chdir(tmp_path)  # keep load_dotenv away from any real .env
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "missing.env")
    clear_llm_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", secret)

    result = CliRunner().invoke(app, ["doctor"])
    out = result.output

    assert result.exit_code == 0
    assert secret not in out
    assert "(ending 3456)" in out
    assert "gemini" in out.lower()


def test_doctor_reports_unconfigured_provider_readably(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from applypilot.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "missing.env")
    clear_llm_env(monkeypatch)

    result = CliRunner().invoke(app, ["doctor"])
    out = result.output

    assert result.exit_code == 0
    assert "No LLM provider configured" in out
