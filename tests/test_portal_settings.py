"""Portal settings tests: 0600 writes, masked read-back, merged saves.

Spec D acceptance: settings write produces a 0600 file; no unmasked key
appears in any settings response; unknown .env lines survive a save.
Live-refresh acceptance (S2-2 / sweep F7): a save reaches the running
process — os.environ, the LLM singleton, and spawned-run envs — with no
restart.
"""

import os
import stat
import sys
import time
from types import SimpleNamespace

from applypilot import config, llm
from applypilot.portal import runner
from applypilot.portal import app as portal_app_module
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


# ---------------------------------------------------------------------------
# .env quoting (sweep F11): special values round-trip through both readers
# ---------------------------------------------------------------------------


def test_quote_env_value_uses_dotenv_canonical_escaping():
    assert config.quote_env_value("plain") == "'plain'"
    assert config.quote_env_value("it's") == "'it\\'s'"
    assert config.quote_env_value("back\\slash") == "'back\\\\slash'"
    assert config.quote_env_value("") == "''"


def test_env_roundtrip_special_chars(portal_env, portal_client, monkeypatch):
    """F11 acceptance: values holding #, quotes, =, and spaces survive a
    settings save and read back identically through BOTH readers — the
    portal's read_env_file and the runtime config.load_env — with no
    corruption of the stored value."""
    values = {
        "LLM_URL": "https://proxy.example/api?team=x #not-a-comment",
        "LLM_MODEL": "model with 'single' \"double\" quotes",
        "GEMINI_API_KEY": "key#hash=equals and spaces, ends with backslash\\",
    }
    for key in values:
        monkeypatch.delenv(key, raising=False)

    client, _ = portal_client
    r = client.post("/settings", data=values, follow_redirects=False)
    assert r.status_code == 303

    # The file itself stores quoted values, not the raw special chars.
    raw = config.ENV_PATH.read_text(encoding="utf-8")
    assert "LLM_URL='https://proxy.example/api?team=x #not-a-comment'" in raw

    # Reader 1: the portal's own parser (now python-dotenv).
    read_back = settings_mod.read_env_file()
    for key, expected in values.items():
        assert read_back[key] == expected, f"read_env_file corrupted {key}"

    # Reader 2: the runtime loader (load_dotenv into os.environ).
    config.load_env()
    for key, expected in values.items():
        assert os.environ[key] == expected, f"load_env corrupted {key}"

    # And a second save (blank secret = keep existing) does not corrupt.
    client.post("/settings", data={"LLM_PROVIDER": "gemini"})
    read_again = settings_mod.read_env_file()
    for key, expected in values.items():
        assert read_again[key] == expected, f"second save corrupted {key}"


# ---------------------------------------------------------------------------
# Live refresh (S2-2 / sweep F7): a save reaches the running process
# ---------------------------------------------------------------------------


def _wait_for_status(run_id: str, timeout: float = 5.0) -> dict:
    """Local copy of test_portal_runner.wait_for_status (self-contained modules)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = runner.get_run(run_id)
        if row and row["status"] != "running":
            return row
        time.sleep(0.05)
    row = runner.get_run(run_id)
    assert row is not None and row["status"] != "running", f"run {run_id} never finished"
    return row


def test_settings_save_refreshes_process_env(portal_client, monkeypatch):
    """Sweep B F7 acceptance: os.environ reflects saved keys after a save."""
    client, _ = portal_client
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    # A var the file never held must survive a save untouched: the refresh
    # is a selective override, not a wholesale file-to-env stomp.
    monkeypatch.setenv("GEMINI_API_KEY", "exported-gemini-key")

    r = client.post("/settings", data={"LLM_PROVIDER": "openai",
                                       "OPENAI_API_KEY": "sk-live-1234"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert os.environ["LLM_PROVIDER"] == "openai"
    assert os.environ["OPENAI_API_KEY"] == "sk-live-1234"
    assert os.environ["GEMINI_API_KEY"] == "exported-gemini-key"

    # Clearing a text/choice key removes it from the process env too.
    client.post("/settings", data={"LLM_PROVIDER": ""})
    assert "LLM_PROVIDER" not in os.environ

    # The saved banner states the live behavior (F7's missing-hint gap).
    page = client.get("/settings?saved=1").text
    assert "applied to the running portal" in page


def test_single_job_rescore_uses_saved_provider(portal_client, monkeypatch):
    """Sweep A S2-2 acceptance: a save resets the LLM singleton, so the next
    in-process rescore rebuilds from the refreshed env — no restart."""
    client, _ = portal_client
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Singleton as if built at portal startup under the old config.
    monkeypatch.setattr(llm, "_instance",
                        SimpleNamespace(provider_name="stale", api_key="stale-key"))

    r = client.post("/settings", data={"LLM_PROVIDER": "openai",
                                       "OPENAI_API_KEY": "sk-live-1234"})
    assert r.status_code == 200

    captured: dict = {}

    def fake_run_scoring(url: str):
        fresh = llm.get_client()
        captured["provider"] = fresh.provider_name
        captured["api_key"] = fresh.api_key
        captured["http"] = fresh._client

    monkeypatch.setattr(portal_app_module, "run_scoring", fake_run_scoring)
    r = client.post("/jobs/rescore", data={"url": "https://jobs.example.com/123"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert captured["provider"] == "openai"
    assert captured["api_key"] == "sk-live-1234"
    captured["http"].close()


def test_settings_save_visible_to_spawned_run_env(portal_env, portal_client, monkeypatch):
    """Sweep A S2-2 acceptance: spawned runs inherit saved values.

    runner.start_run copies os.environ at spawn; a save must reach the
    next run's environment without a portal restart.
    """
    client, _ = portal_client
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    r = client.post("/settings", data={"LLM_PROVIDER": "openai"})
    assert r.status_code == 200

    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c",
                 "import os; print('PROBE:' + os.environ.get('LLM_PROVIDER', '<unset>'))"],
        emit_jsonl=False,
    )
    final = _wait_for_status(row["id"])
    assert final["status"] == "completed"
    log_text = (portal_env / "runs" / f"{row['id']}.log").read_text(encoding="utf-8")
    assert "PROBE:openai" in log_text


def test_llm_reset_client_drops_singleton(monkeypatch):
    """reset_client drops the singleton; get_client rebuilds from the env."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ok-after-reset")
    stale = SimpleNamespace(provider_name="stale")
    monkeypatch.setattr(llm, "_instance", stale)

    llm.reset_client()
    fresh = llm.get_client()
    assert fresh is not stale
    assert fresh.provider_name == "openai"
    assert fresh.api_key == "ok-after-reset"
    fresh._client.close()
