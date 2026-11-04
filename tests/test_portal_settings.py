"""Portal settings tests: 0600 writes, masked read-back, merged saves.

Spec D acceptance: settings write produces a 0600 file; no unmasked key
appears in any settings response; unknown .env lines survive a save.
"""

import os
import stat

from applypilot import config
from applypilot.portal import settings as settings_mod


def test_mask_hides_all_but_last_four():
    assert settings_mod.mask("supersecretkey123") == "••••y123"
    assert settings_mod.mask("ab") == "••"
    assert settings_mod.mask("") == ""
    assert settings_mod.mask(None) == ""


def test_settings_write_0600_and_masked_read_back(portal_client):
    client, _ = portal_client
    r = client.post("/settings", data={"GEMINI_API_KEY": "supersecretkey123"},
                    follow_redirects=False)
    assert r.status_code == 303

    env_file = config.ENV_PATH
    assert env_file.exists()
    if os.name == "posix":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    page = client.get("/settings").text
    assert "supersecretkey123" not in page
    assert "••••y123" in page


def test_post_response_never_echoes_secret(portal_client):
    client, _ = portal_client
    r = client.post(
        "/settings",
        data={"OPENAI_API_KEY": "sk-openai-secret-value", "LLM_PROVIDER": "openai"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "sk-openai-secret-value" not in r.headers.get("location", "")
    assert "sk-openai-secret-value" not in r.text


def test_blank_secret_keeps_existing_value(portal_env, portal_client):
    client, _ = portal_client
    client.post("/settings", data={"CAPSOLVER_API_KEY": "capsolver-key-9999"})
    # Second save with the secret field blank keeps it.
    client.post("/settings", data={"LLM_PROVIDER": "openai", "CAPSOLVER_API_KEY": ""})
    values = settings_mod.read_env_file()
    assert values["CAPSOLVER_API_KEY"] == "capsolver-key-9999"


def test_clear_checkbox_removes_secret(portal_client):
    client, _ = portal_client
    client.post("/settings", data={"CAPSOLVER_API_KEY": "capsolver-key-9999"})
    client.post("/settings", data={"CAPSOLVER_API_KEY": "", "CAPSOLVER_API_KEY__clear": "1"})
    values = settings_mod.read_env_file()
    assert "CAPSOLVER_API_KEY" not in values


def test_unknown_env_lines_survive_save(portal_env, portal_client):
    client, _ = portal_client
    config.ENV_PATH.write_text(
        "# my comment\nUNKNOWN_VAR=keepme\nGEMINI_API_KEY=old-gemini-key\n",
        encoding="utf-8",
    )
    client.post("/settings", data={"GEMINI_API_KEY": "new-gemini-key"})
    raw = config.ENV_PATH.read_text(encoding="utf-8")
    assert "UNKNOWN_VAR=keepme" in raw
    assert "new-gemini-key" in raw
    assert "old-gemini-key" not in raw


def test_provider_choice_persists(portal_client):
    client, _ = portal_client
    client.post("/settings", data={
        "LLM_PROVIDER": "custom",
        "LLM_URL": "http://localhost:11434/v1",
        "LLM_MODEL": "qwen2.5:7b",
    })
    values = settings_mod.read_env_file()
    assert values["LLM_PROVIDER"] == "custom"
    assert values["LLM_URL"] == "http://localhost:11434/v1"
    assert values["LLM_MODEL"] == "qwen2.5:7b"


def test_redact_for_render_strips_configured_keys(portal_env):
    config.ENV_PATH.write_text("GEMINI_API_KEY=gemini-live-key-xyz\n", encoding="utf-8")
    text = settings_mod.redact_for_render(
        "request failed with key gemini-live-key-xyz in URL"
    )
    assert "gemini-live-key-xyz" not in text
    assert "[REDACTED]" in text
