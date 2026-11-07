"""Scripted end-to-end protocol journey (spec art_A71wyYS4, Verification 1).

The full arc the spec draws for a host coding agent, on one store, through
the real CLI surfaces:

    agent start → next → resume intake → start (kickoff) → answer ×N
    → review --set → agent confirm → run discover enrich score
    → apply --plan → apply --i-confirm

Every external boundary is the deterministic stub of the live-verification
rig (art_M7Xt2rnH), in-process:

- onboarding turns ride the scripted fake `claude` binary (the rig's
  fake-session shim — the same FAKE_CLAUDE rig as tests/test_onboarding_agent.py);
- the LLM provider boundary (the rig's localhost OpenAI-compatible stub,
  in-process) returns a scripted SCORE reply through conftest's fake_llm;
- the discovery and enrichment network seams write deterministic job data;
- the apply engine boundary records the pinned run instead of launching
  Chrome (the same seam the guardrail suite pins).

Zero network, zero real keys. Green on Python 3.11, 3.12, 3.13.
"""

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

import applypilot.scoring.scorer as scorer
from applypilot import config, database, engine_auth, onboarding
from applypilot.apply import launcher, planning
from applypilot.cli import app

# The live rig's scripted `claude -p` shim: kickoff greeting on turn 1, the
# fenced profile draft on the resumed session (fake-session-1).
from tests.test_onboarding_agent import FAKE_CLAUDE, SIGNED_IN

runner = CliRunner()

SCORE_REPLY = ("SCORE: 9\nKEYWORDS: python, sql, airflow\n"
               "REASONING: Direct match on every pipeline skill in the description.")

JOBS = [
    {"url": "https://jobs.example/alpha", "title": "Data Engineer",
     "site": "exampleboard"},
    {"url": "https://jobs.example/beta", "title": "Analytics Engineer",
     "site": "exampleboard"},
]


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Install the scripted fake on a temp PATH (the test_onboarding_agent rig)."""
    import stat as stat_module

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat_module.S_IEXEC)
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(fake))
    return fake


@pytest.fixture()
def signed_in(monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: SIGNED_IN)


def run(*args):
    """Invoke `applypilot agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    """The one JSON document off stdout — the protocol's wire contract."""
    return json.loads(result.stdout)


def _drain_agenda():
    """Walk the remaining agenda using only what the next payload shows."""
    for _ in range(len(onboarding.QUESTION_SPECS) + 1):
        question = payload_of(run("next"))["current_question"]
        if question is None:
            return
        if question["choices"]:
            argv = ("answer", "--question", question["id"],
                    "--choice", question["choices"][0]["value"])
        elif question["allow_free_text"]:
            argv = ("answer", "--question", question["id"],
                    "--free-text", "Something reasonable")
        else:
            argv = ("answer", "--question", question["id"], "--skip")
        result = run(*argv)
        assert result.exit_code == 0, result.stdout
    pytest.fail("the agenda never completed")


def _stub_discovery(monkeypatch):
    """Seed the store at the discovery seams (the rig's local job data).

    One source carries listings (as the rig configured one board); the
    others run empty. The seams are the functions pipeline._run_discover
    imports at call time, so the real stage code executes.
    """
    from applypilot.discovery import composio, jobspy, smartextract, workday

    def seed_jobspy(results_per_site=None):
        conn = database.get_connection()
        for job in JOBS:
            conn.execute(
                "INSERT INTO jobs (url, title, site, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                (job["url"], job["title"], job["site"],
                 datetime.now(timezone.utc).isoformat()))
        conn.commit()

    monkeypatch.setattr(jobspy, "run_discovery", seed_jobspy)
    monkeypatch.setattr(workday, "run_workday_discovery", lambda workers=1: None)
    monkeypatch.setattr(smartextract, "run_smart_extract", lambda workers=1: None)
    monkeypatch.setattr(composio, "run_composio_discovery", lambda: None)


def _stub_enrichment(monkeypatch):
    """Fill descriptions at the enrichment seam (no page fetches)."""
    from applypilot.enrichment import detail

    def fill(workers=1, limit=100):
        conn = database.get_connection()
        for job in JOBS:
            conn.execute(
                "UPDATE jobs SET full_description = ?, application_url = ? "
                "WHERE url = ?",
                (f"Full posting for {job['title']}: Python, SQL, Airflow, dbt.",
                 job["url"], job["url"]))
        conn.commit()

    monkeypatch.setattr(detail, "run_enrichment", fill)


def _pin_tailored_outputs():
    """Pin the tailor stage's queue outcome.

    The tailor stage is outside this journey's stage list (its own suites
    cover it); the apply queue's predicate needs its artifact on disk, so
    the rows are left exactly as the stage would leave them.
    """
    conn = database.get_connection()
    for job in JOBS:
        conn.execute(
            "UPDATE jobs SET tailored_resume_path = ? WHERE url = ?",
            (f"/tmp/fake/{job['url'].rsplit('/', 1)[-1]}.pdf", job["url"]))
    conn.commit()


def test_protocol_journey_onboarding_through_confirmed_apply(
        app_dir, fake_claude, signed_in, fake_llm, monkeypatch, tmp_path):
    # The provider/engine gates ask "is a backend configured" — the scripted
    # stubs ARE the backend here, the same substitution the rig made.
    monkeypatch.setattr(config, "check_tier", lambda tier, feature: None)
    # scorer binds RESUME_PATH at import time; repoint it at the store the
    # journey's resume upload writes to (the test_pipeline_bugs pattern).
    monkeypatch.setattr(scorer, "RESUME_PATH", config.RESUME_PATH)
    _stub_discovery(monkeypatch)
    _stub_enrichment(monkeypatch)

    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé. Python, SQL, Airflow.",
                           encoding="utf-8")

    # --- start: intake opens on a fresh store, resume needed. -------------
    result = run("start")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "intake"
    assert payload["needs"] == {"resume": True, "confirm": False,
                                "provider_setup": False}
    assert payload["progress"] == {"answered": 0, "remaining": 18}

    # --- next: a pure read of the same phase. -----------------------------
    payload = payload_of(run("next"))
    assert payload["phase"] == "intake"
    assert payload["current_question"] is None

    # --- resume intake: the document queues; needs.resume flips off. ------
    result = run("resume", "--file", str(resume_file))
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "intake"
    assert payload["resume"]["provided"] is True
    assert payload["resume"]["pending"] is True
    assert payload["needs"]["resume"] is False

    # --- start again: the kickoff turn runs → interview. ------------------
    result = run("start")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "interview"
    assert payload["mode"] == "choices"
    assert payload["last_agent_reply"] == "Hello! Tell me about your background."

    # --- answer ×N: payload-driven, every choice a listed value. ----------
    _drain_agenda()

    # --- review: the form rides the payload; edits land. ------------------
    payload = payload_of(run("next"))
    assert payload["phase"] == "review"
    assert payload["needs"]["confirm"] is True
    result = run("review", "--set", "phone=555-0199")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    fields = {f["id"]: f for f in payload["review_fields"]}
    assert fields["phone"]["value"] == "555-0199"

    # --- confirm: the only path out of REVIEW; the files land. ------------
    result = run("confirm")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "done"
    assert (app_dir / "profile.json").exists()
    assert (app_dir / "searches.yaml").exists()

    # --- run discover enrich score: real stages, stubbed seams, and the
    # real scorer over the scripted provider replies. ----------------------
    fake_llm.respond(SCORE_REPLY, SCORE_REPLY)
    result = runner.invoke(app, ["run", "discover", "enrich", "score"])
    assert result.exit_code == 0, result.stdout + result.stderr

    conn = database.get_connection()
    rows = {row["url"]: dict(row) for row in conn.execute(
        "SELECT url, fit_score, full_description FROM jobs").fetchall()}
    assert sorted(rows) == sorted(job["url"] for job in JOBS)
    for job in JOBS:
        assert rows[job["url"]]["fit_score"] == 9, job["url"]
        assert "Python, SQL, Airflow" in rows[job["url"]]["full_description"]
    # The scripted provider saw exactly the two scoring asks.
    assert len(fake_llm.calls) == 2

    _pin_tailored_outputs()

    # --- apply --plan: ONE pure-JSON plan; commits nothing. ---------------
    result = runner.invoke(app, ["apply", "--plan", "--limit", "2"])
    assert result.exit_code == 0, result.stdout + result.stderr
    plan_payload = json.loads(result.stdout)
    assert plan_payload["commits_nothing"] is True
    assert plan_payload["batch_size"] == 2
    assert plan_payload["cap"] == planning.AGENT_DEFAULT_DAILY_CAP
    assert plan_payload["cap_source"] == "default"
    assert plan_payload["headroom"] == planning.AGENT_DEFAULT_DAILY_CAP
    assert [job["url"] for job in plan_payload["jobs"]] == sorted(
        job["url"] for job in JOBS)
    assert len(plan_payload["plan_hash"]) == 64
    assert (app_dir / planning.STAGED_PLAN_FILE).exists()

    # --- apply --i-confirm: the pinned batch runs; the token burns. -------
    runs: list[dict] = []

    def fake_main(**kwargs):
        runs.append(kwargs)
        return 0, 0

    monkeypatch.setattr(launcher, "main", fake_main)

    result = runner.invoke(app, ["apply", "--i-confirm", plan_payload["plan_hash"]])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(runs) == 1
    call = runs[0]
    assert call["limit"] == 2
    assert call["daily_cap"] == planning.AGENT_DEFAULT_DAILY_CAP
    assert call["allowed_urls"] == {job["url"] for job in JOBS}
    assert call["continuous"] is False
    assert not (app_dir / planning.STAGED_PLAN_FILE).exists()

    # The token was one-shot: a replayed confirm refuses, structurally.
    result = runner.invoke(app, ["apply", "--i-confirm", plan_payload["plan_hash"]])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "plan_required"
