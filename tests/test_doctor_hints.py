"""Doctor onboarding hints must present both routes, agent route first.

After PR #81/#82 the agent-native path (kestrel agent start --mode agent)
is the primary onboarding route; the wizard (kestrel init) is the
interactive alternative. Doctor hint sites that name only init are a
bug. Fresh temp KESTREL_DIR comes from the app_dir fixture.
"""

import json

from typer.testing import CliRunner

from kestrel.cli import app

runner = CliRunner()

AGENT_ROUTE = "kestrel agent start --mode agent"
WIZARD_ROUTE = "kestrel init"


def _doctor_json(app_dir, monkeypatch):
    from tests.test_cli_json_output import _clear_provider_env

    monkeypatch.chdir(app_dir)  # keep load_dotenv away from any real .env
    _clear_provider_env(monkeypatch)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    return json.loads(result.stdout)


def _assert_both_routes_ordered(text: str) -> None:
    """Agent route named first, wizard named after it."""
    assert AGENT_ROUTE in text
    assert WIZARD_ROUTE in text
    assert text.index(AGENT_ROUTE) < text.index(WIZARD_ROUTE)


def test_doctor_missing_profile_hint_names_both_routes_agent_first(app_dir, monkeypatch):
    payload = _doctor_json(app_dir, monkeypatch)
    profile = next(c for c in payload["checks"] if c["name"] == "profile.json")

    assert profile["status"] == "fail"
    _assert_both_routes_ordered(profile["note"])
    assert profile["fix"]["command"] == AGENT_ROUTE


def test_doctor_missing_resume_hint_names_both_routes_agent_first(app_dir, monkeypatch):
    (app_dir / "profile.json").write_text("{}")
    payload = _doctor_json(app_dir, monkeypatch)
    resume = next(c for c in payload["checks"] if c["name"] == "resume.txt")

    assert resume["status"] == "fail"
    _assert_both_routes_ordered(resume["note"])
    assert resume["fix"]["command"] == AGENT_ROUTE


def test_doctor_missing_searches_hint_names_both_routes_agent_first(app_dir, monkeypatch):
    payload = _doctor_json(app_dir, monkeypatch)
    searches = next(c for c in payload["checks"] if c["name"] == "searches.yaml")

    assert searches["status"] == "warn"
    _assert_both_routes_ordered(searches["note"])
    assert searches["fix"]["command"] == AGENT_ROUTE


def test_doctor_llm_provider_fix_names_agent_route(app_dir, monkeypatch):
    payload = _doctor_json(app_dir, monkeypatch)
    provider = next(c for c in payload["checks"] if c["name"] == "LLM provider")

    assert provider["status"] == "fail"
    assert provider["fix"]["command"] == AGENT_ROUTE


def test_doctor_rich_tier_summary_names_both_routes_agent_first(app_dir, monkeypatch):
    from tests.test_cli_json_output import _clear_provider_env

    monkeypatch.chdir(app_dir)
    _clear_provider_env(monkeypatch)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Tier 1" in result.output
    # Rich wraps the console at the terminal width; normalize whitespace so
    # the wrapped route names compare as one string.
    normalized = " ".join(result.output.split())
    tier2_start = normalized.index("Tier 2 unlocks")
    tier3_start = normalized.index("Tier 3 unlocks")
    tier2_text = normalized[tier2_start:tier3_start]
    _assert_both_routes_ordered(tier2_text)
