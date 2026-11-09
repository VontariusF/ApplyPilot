"""Contract tests for the --json output modes on `status` and `doctor`.

The --json contract (agent protocol spec, option b): in JSON mode ALL
contractual output is one pure-JSON document on stdout alone — no Rich
markup, nothing contractual on stderr — and errors use the
{"error": {"code", "message"}} envelope with a nonzero exit code.
"""

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from applypilot.cli import app

runner = CliRunner()

# A realistic get_stats() return: by_site / score_distribution are lists of
# tuples from SQLite, which JSON must serialize as arrays.
STATS = {
    "total": 5,
    "by_site": [("hn", 3), ("w3c", 2)],
    "pending_detail": 1,
    "with_description": 4,
    "detail_errors": 0,
    "scored": 2,
    "scored_fresh": 1,
    "score_failed": 1,
    "unscored": 2,
    "pending_score": 3,
    "tailored": 1,
    "tailor_exhausted": 0,
    "untailored_eligible": 1,
    "with_cover_letter": 1,
    "cover_exhausted": 0,
    "ready_to_apply": 1,
    "applied": 0,
    "captcha": 1,
    "apply_errors": 0,
    "manual_marked": 0,
    "expired": 0,
    "score_distribution": [(7, 1), (5, 1)],
    "score_provenance": {"host": 0, "provider": 2, "unattributed": 0},
}

STATS_AS_JSON = {
    **{k: v for k, v in STATS.items() if k not in ("by_site", "score_distribution")},
    "by_site": [["hn", 3], ["w3c", 2]],
    "score_distribution": [[7, 1], [5, 1]],
}


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_PROVIDER", "LLM_URL", "LLM_API_KEY", "LLM_MODEL",
        "GEMINI_API_KEY", "OPENAI_API_KEY",
        "COMPOSIO_API_KEY", "CAPSOLVER_API_KEY", "FACTORY_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# status --json
# ---------------------------------------------------------------------------

def test_status_json_emits_get_stats_dict(monkeypatch):
    import applypilot.database as database

    monkeypatch.setattr(database, "get_stats", lambda: dict(STATS))

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == STATS_AS_JSON


def test_status_json_is_pure_json_on_stdout_alone(monkeypatch):
    import applypilot.database as database

    monkeypatch.setattr(database, "get_stats", lambda: dict(STATS))

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    # No Rich markup leaks into the document.
    assert "[green]" not in result.stdout and "[/" not in result.stdout
    assert result.stderr == ""


def test_status_json_error_path(monkeypatch):
    import applypilot.database as database

    def boom():
        raise sqlite3.OperationalError("no such table: jobs")

    monkeypatch.setattr(database, "get_stats", boom)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "database_error"
    assert "no such table: jobs" in payload["error"]["message"]
    # Nothing contractual on stderr; the envelope is the whole stdout.
    assert result.stderr == ""


def test_status_rich_mode_unaffected(monkeypatch):
    import applypilot.database as database

    monkeypatch.setattr(database, "get_stats", lambda: dict(STATS))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Pipeline Overview" in result.output
    assert "Total jobs discovered" in result.output


# ---------------------------------------------------------------------------
# doctor --json
# ---------------------------------------------------------------------------

def test_doctor_json_contract(app_dir, monkeypatch):
    monkeypatch.chdir(app_dir)  # keep load_dotenv away from any real .env
    _clear_provider_env(monkeypatch)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert set(payload) == {"ok", "tier", "tier_label", "checks"}
    assert isinstance(payload["tier"], int)
    names = [c["name"] for c in payload["checks"]]
    assert "profile.json" in names
    assert "resume.txt" in names
    assert "LLM provider" in names
    assert "Chrome/Chromium" in names

    allowed = {"ok", "warn", "fail", "optional"}
    for check in payload["checks"]:
        assert set(check) == {"name", "status", "note", "fix"}
        assert check["status"] in allowed
        if check["status"] == "fail":
            assert isinstance(check["fix"], dict) and check["fix"]["code"]

    # A fresh app dir has no profile or resume: both fail with run_init hints.
    profile = next(c for c in payload["checks"] if c["name"] == "profile.json")
    assert profile["status"] == "fail"
    assert profile["fix"] == {"code": "run_init", "command": "applypilot init"}
    resume = next(c for c in payload["checks"] if c["name"] == "resume.txt")
    assert resume["status"] == "fail"
    assert resume["fix"]["code"] == "run_init"

    assert payload["ok"] is False
    assert result.stderr == ""


def test_doctor_json_ok_when_everything_available(app_dir, monkeypatch):
    """A fully provisioned setup reads ok: true with no fail checks."""
    from applypilot import config, engine_auth

    monkeypatch.chdir(app_dir)
    _clear_provider_env(monkeypatch)
    (app_dir / "profile.json").write_text("{}")
    (app_dir / "resume.txt").write_text("resume")
    (app_dir / "searches.yaml").write_text("searches: []")
    # Provider env via setenv, NOT a .env file: doctor's load_env() loads
    # .env into os.environ with no undo, and a leaked LLM_URL pointing at a
    # dead port poisons later tests (ConnectError in the portal suites).
    monkeypatch.setenv("LLM_URL", "http://localhost:9000/v1")
    monkeypatch.setenv("LLM_API_KEY", "test")

    status = engine_auth.EngineStatus
    monkeypatch.setattr(
        engine_auth, "claude_status",
        lambda: status("claude", engine_auth.SIGNED_IN, "api_key", "/usr/bin/claude", None))
    monkeypatch.setattr(
        engine_auth, "droid_status",
        lambda: status("droid", engine_auth.SIGNED_IN, "api_key", "/usr/bin/droid", None))
    monkeypatch.setattr(config, "get_chrome_path", lambda: "/usr/bin/chromium")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx" if name == "npx" else None)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert not any(c["status"] == "fail" for c in payload["checks"])


def test_doctor_json_error_path(app_dir, monkeypatch):
    import applypilot.discovery.composio as composio

    def boom():
        raise RuntimeError("composio exploded")

    monkeypatch.setattr(composio, "composio_state", boom)
    monkeypatch.chdir(app_dir)
    _clear_provider_env(monkeypatch)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "doctor_error"
    assert "composio exploded" in payload["error"]["message"]
    assert result.stderr == ""


def test_doctor_rich_mode_survives_refactor(app_dir, monkeypatch):
    monkeypatch.chdir(app_dir)
    _clear_provider_env(monkeypatch)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "ApplyPilot Doctor" in result.output
    assert "MISSING" in result.output
    assert "Current tier" in result.output
