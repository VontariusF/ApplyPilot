"""Scoring robustness + rescore (spec PR E).

Covers the verifying tests named by the spec:
- rescore flag/round-trip (run_scoring and the CLI command),
- missing-resume error state (score_error, not a silent skip or crash),
- malformed-response retry-then-fail with a mocked LLM,
- portal routes for the new Rescore actions,
- portal rendering that distinguishes "not scored yet" from "scoring failed".
"""

from datetime import datetime, timezone
from urllib.parse import unquote

import pytest
from typer.testing import CliRunner

from applypilot import database
from applypilot.scoring import scorer

GOOD = "SCORE: 8\nKEYWORDS: python, llm\nREASONING: Solid match on core skills."
MALFORMED = "I think this candidate looks like a great fit for the role overall."


class FakeClient:
    """Stand-in for the LLM client; hands out scripted responses in order."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def seed_job(url="https://jobs.example/posting-1", **overrides) -> str:
    conn = database.init_db()
    row = {
        "url": url,
        "title": "Pwidget Engineer",
        "site": "ExampleBoard",
        "location": "Remote",
        "full_description": "full description text",
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    return url


def job_row(url) -> dict:
    row = database.get_connection().execute(
        "SELECT * FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    return dict(row) if row else None


@pytest.fixture
def resume(app_dir, monkeypatch):
    """An existing resume.txt, with scorer's imported path repointed to it."""
    path = app_dir / "resume.txt"
    path.write_text("Experienced pwidget engineer.\n", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", path)
    return path


# ---------------------------------------------------------------------------
# Rescore flag / round-trip
# ---------------------------------------------------------------------------


def test_rescore_round_trip(app_dir, resume, monkeypatch):
    url = seed_job(fit_score=5, score_reasoning="old score", scored_at="t0")
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    # A normal pass skips already-scored jobs.
    result = scorer.run_scoring()
    assert result["scored"] == 0
    assert job_row(url)["fit_score"] == 5

    # rescore=True re-scores it and updates the stored state.
    result = scorer.run_scoring(rescore=True)
    assert result["scored"] == 1
    assert result["errors"] == 0
    row = job_row(url)
    assert row["fit_score"] == 8
    assert "Solid match" in row["score_reasoning"]
    assert row["scored_at"] != "t0"
    assert row["score_error"] is None


def test_rescore_targeted_url_leaves_other_jobs_alone(app_dir, resume, monkeypatch):
    url = seed_job(fit_score=5)
    seed_job(url="https://jobs.example/other", fit_score=6)
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    result = scorer.run_scoring(url=url)
    assert result["scored"] == 1
    assert result["errors"] == 0
    assert job_row(url)["fit_score"] == 8
    assert job_row("https://jobs.example/other")["fit_score"] == 6
    assert fake.calls == 1  # exactly one LLM call


def test_rescore_targeted_url_without_description_fails_visibly(app_dir, resume, monkeypatch):
    url = seed_job(full_description=None)
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    result = scorer.run_scoring(url=url)
    assert result["errors"] == 1
    assert result["scored"] == 0
    assert fake.calls == 0  # nothing was sent to the LLM
    assert "not scoreable" in job_row(url)["score_error"]


# ---------------------------------------------------------------------------
# Missing resume -> clear failed state
# ---------------------------------------------------------------------------


def test_missing_resume_marks_score_error(app_dir, monkeypatch):
    url = seed_job()
    missing = app_dir / "resume.txt"  # never created
    monkeypatch.setattr(scorer, "RESUME_PATH", missing)

    result = scorer.run_scoring()
    assert result["errors"] == 1
    assert result["scored"] == 0
    row = job_row(url)
    assert row["fit_score"] is None
    assert "Resume file not found" in row["score_error"]


def test_score_error_cleared_on_later_success(app_dir, resume, monkeypatch):
    url = seed_job(score_error="old failure")
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    scorer.run_scoring()
    row = job_row(url)
    assert row["fit_score"] == 8
    assert row["score_error"] is None


# ---------------------------------------------------------------------------
# Malformed LLM response -> retry once, then fail visibly
# ---------------------------------------------------------------------------


def test_malformed_response_retries_once_then_fails(app_dir, resume, monkeypatch):
    url = seed_job()
    fake = FakeClient([MALFORMED, MALFORMED])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    result = scorer.run_scoring()
    assert fake.calls == 2  # exactly one retry after the malformed response
    assert result["errors"] == 1
    row = job_row(url)
    assert row["fit_score"] is None  # malformed response must not store score 0
    assert "Malformed LLM response" in row["score_error"]


def test_malformed_then_valid_recovers_on_retry(app_dir, resume, monkeypatch):
    url = seed_job()
    fake = FakeClient([MALFORMED, GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    result = scorer.run_scoring()
    assert result["errors"] == 0
    assert result["scored"] == 1
    row = job_row(url)
    assert row["fit_score"] == 8
    assert row["score_error"] is None


def test_llm_exception_records_score_error(app_dir, resume, monkeypatch):
    url = seed_job()

    class ExplodingClient:
        def chat(self, messages, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr(scorer, "get_client", lambda: ExplodingClient())

    result = scorer.run_scoring()
    assert result["errors"] == 1
    row = job_row(url)
    assert row["fit_score"] is None
    assert "provider down" in row["score_error"]


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def test_cli_rescore_requires_target():
    from applypilot.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["rescore"])
    assert result.exit_code == 1
    assert "Nothing to re-score" in result.output


def test_cli_rescore_rejects_url_with_all():
    from applypilot.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["rescore", "https://jobs.example/x", "--all"])
    assert result.exit_code == 1
    assert "not both" in result.output


def test_cli_rescore_all_round_trip(app_dir, resume, monkeypatch):
    from applypilot.cli import app as cli_app

    url = seed_job(fit_score=5, score_reasoning="old score")
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)
    monkeypatch.setattr("applypilot.config.check_tier", lambda *a, **k: None)

    result = CliRunner().invoke(cli_app, ["rescore", "--all"])
    assert result.exit_code == 0, result.output
    assert "Scored 1 job(s)" in result.output
    assert job_row(url)["fit_score"] == 8


def test_cli_rescore_single_url(app_dir, resume, monkeypatch):
    from applypilot.cli import app as cli_app

    url = seed_job()
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)
    monkeypatch.setattr("applypilot.config.check_tier", lambda *a, **k: None)

    result = CliRunner().invoke(cli_app, ["rescore", url])
    assert result.exit_code == 0, result.output
    assert job_row(url)["fit_score"] == 8


# ---------------------------------------------------------------------------
# Portal routes and failed-state rendering
# ---------------------------------------------------------------------------


def test_portal_rescore_route_scores_job(portal_client, resume, monkeypatch):
    client, _ = portal_client
    url = seed_job(fit_score=None)
    fake = FakeClient([GOOD])
    monkeypatch.setattr(scorer, "get_client", lambda: fake)

    r = client.post("/jobs/rescore", data={"url": url}, follow_redirects=False)
    assert r.status_code == 303
    assert unquote(r.headers["location"]) == f"/jobs/detail?url={url}"
    row = job_row(url)
    assert row["fit_score"] == 8
    assert row["score_error"] is None


def test_portal_rescore_route_requires_url(portal_client):
    client, _ = portal_client
    r = client.post("/jobs/rescore", data={})
    assert r.status_code == 400


def test_portal_rescore_all_spawns_tracked_run(portal_client, monkeypatch):
    import sys

    client, _ = portal_client
    from applypilot.portal import runner as runner_mod

    seen = {}
    original = runner_mod.start_run

    def fake_start(kind, params, command=None, emit_jsonl=None):
        seen["kind"] = kind
        return original(kind, params, command=[sys.executable, "-c", "pass"],
                        emit_jsonl=False)

    monkeypatch.setattr(runner_mod, "start_run", fake_start)

    r = client.post("/jobs/rescore-all", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/runs"
    assert seen["kind"] == "rescore"


def test_detail_renders_score_failed_distinctly(portal_client):
    client, _ = portal_client
    url = seed_job(fit_score=None, score_error="LLM unavailable")

    r = client.get("/jobs/detail", params={"url": url})
    assert r.status_code == 200
    assert "Scoring failed" in r.text
    assert "LLM unavailable" in r.text
    assert "Not scored yet." not in r.text
    assert "Rescore" in r.text  # the retry action is offered on the page


def test_detail_renders_not_scored_when_no_error(portal_client):
    client, _ = portal_client
    url = seed_job(fit_score=None)

    r = client.get("/jobs/detail", params={"url": url})
    assert r.status_code == 200
    assert "Not scored yet." in r.text
    assert "Scoring failed" not in r.text


def test_jobs_page_shows_failed_badge_and_rescore_all(portal_client):
    client, _ = portal_client
    seed_job(fit_score=None, score_error="LLM unavailable")
    seed_job(url="https://jobs.example/clean", fit_score=7)

    r = client.get("/jobs", params={"stage": "score_failed"})
    assert r.status_code == 200
    assert "score failed" in r.text
    assert "Rescore all" in r.text

    # The score_failed filter only surfaces the failed job.
    table = client.get("/jobs/table", params={"stage": "score_failed"})
    assert "Pwidget Engineer" in table.text
    assert "clean" not in table.text
