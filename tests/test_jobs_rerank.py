"""Integration tests for deep-filtering Layer 3 (spec art_we4AUxEy).

Resume-aware staleness, `jobs rerank`, the score_history sidecar, and
--rescored-since delta reporting: --dry-run commits nothing; the marked
set is what `agent job-next --stage score` serves first; re-scoring
through the host verbs records the prior entry and clears the mark; and
a plan staged before reranking still confirms against its original
token semantics.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from applypilot import database
from applypilot.apply import planning
from applypilot.cli import app
from applypilot.database import parse_score_history
from applypilot.scoring.scorer import resume_sha256

runner = CliRunner()

RESUME_V1 = "platform engineer with nine years of experience\n"
RESUME_V2 = "staff platform engineer with ten years of experience\n"


def run(*args):
    """Invoke `applypilot ...` and return the CliRunner result."""
    return runner.invoke(app, list(args))


def payload_of(result):
    """The one JSON document off stdout — the contract under test."""
    return json.loads(result.stdout)


def _hash(text: str) -> str:
    return resume_sha256(text)


def _seed(conn, url, *, title="Engineer", fit_score=None, site=None,
          scored_resume_hash=None, scored_at=None, full_description="d" * 300,
          application_url=None, tailored_resume_path=None):
    conn.execute(
        "INSERT INTO jobs (url, title, fit_score, site, scored_resume_hash, scored_at,"
        " full_description, application_url, tailored_resume_path)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, title, fit_score, site, scored_resume_hash, scored_at,
         full_description, application_url, tailored_resume_path),
    )


def _row(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


@pytest.fixture()
def store(app_dir, isolated_db):
    """A store with a resume on disk (V1) and two described jobs."""
    (app_dir / "resume.txt").write_text(RESUME_V1, encoding="utf-8")
    conn = database.init_db()
    _seed(conn, "https://jobs.example/j1")
    _seed(conn, "https://jobs.example/j2", title="Analytics Engineer")
    return conn


def _score_via_verb(url, score, *extra):
    return run("agent", "score", "--url", url, "--score", str(score),
               "--reasoning", "judged against the payload", *extra)


# ---------------------------------------------------------------------------
# jobs rerank — dry run, marking, job-next service order
# ---------------------------------------------------------------------------


def test_rerank_dry_run_commits_nothing(store):
    _seed(store, "https://jobs.example/j0", title="Scored Already", fit_score=4,
          scored_at="2026-11-01T00:00:00+00:00")
    store.commit()

    result = run("jobs", "rerank", "--min-score", "1", "--dry-run", "--json")

    assert result.exit_code == 0
    payload = payload_of(result)
    assert payload["dry_run"] is True
    assert payload["count"] == 1
    assert payload["marked"] == 0
    # Nothing stamped: the scored job is still invisible to the score
    # queue (pending_score requires fit_score IS NULL) and unmarked.
    assert _row(store, "https://jobs.example/j0")["rerank_marked_at"] is None
    # The unmarked scored job stays invisible to the score queue — what
    # serves is an ordinary pending (unscored) row, never j0.
    job_next = payload_of(run("agent", "job-next", "--stage", "score"))
    assert job_next["job"]["url"] != "https://jobs.example/j0"


def test_rerank_marks_filtered_set_and_job_next_serves_first(store):
    store.execute(
        "UPDATE jobs SET fit_score = 4, scored_at = ?, scored_by = 'host',"
        " scored_resume_hash = ? WHERE url = ?",
        ("2026-11-01T00:00:00+00:00", _hash(RESUME_V1), "https://jobs.example/j1"))
    store.commit()

    result = run("jobs", "rerank", "--min-score", "1", "--json")

    assert result.exit_code == 0
    payload = payload_of(result)
    assert payload["dry_run"] is False
    assert payload["count"] == 1
    assert payload["marked"] == 1
    # --min-score excludes the unscored j2 (NULL fit never matches >= 1).
    assert _row(store, "https://jobs.example/j2")["rerank_marked_at"] is None

    # The marked — already-scored — job is served FIRST, ahead of the
    # ordinary pending_score queue, and the payload says --rescore.
    job_next = payload_of(run("agent", "job-next", "--stage", "score"))
    assert job_next["job"]["url"] == "https://jobs.example/j1"
    assert job_next["rescore_required"] is True

    scored = _score_via_verb("https://jobs.example/j1", 8, "--rescore", "--prev-score", "4")
    assert scored.exit_code == 0
    row = _row(store, "https://jobs.example/j1")
    assert row["fit_score"] == 8
    # Re-judged: the row left the rerank set.
    assert row["rerank_marked_at"] is None
    # The queue falls back to the unscored job.
    job_next = payload_of(run("agent", "job-next", "--stage", "score"))
    assert job_next["job"]["url"] == "https://jobs.example/j2"


def test_rerank_respects_predicates_and_stale_only(store):
    conn = store
    conn.execute("UPDATE jobs SET fit_score = 4 WHERE url = ?", ("https://jobs.example/j1",))
    conn.execute(
        "UPDATE jobs SET fit_score = 9, scored_resume_hash = ? WHERE url = ?",
        (_hash(RESUME_V1), "https://jobs.example/j2"))
    conn.commit()

    result = run("jobs", "rerank", "--min-score", "5", "--json")
    assert payload_of(result)["marked"] == 1
    assert _row(conn, "https://jobs.example/j2")["rerank_marked_at"] is not None
    assert _row(conn, "https://jobs.example/j1")["rerank_marked_at"] is None

    # A fresh mark must not leak into the next selection: clear, then
    # target the stale set only — j1 never bound a hash (NULL = stale),
    # j2's is current (fresh).
    conn.execute("UPDATE jobs SET rerank_marked_at = NULL")
    conn.commit()
    result = run("jobs", "rerank", "--stale-only", "--json")
    assert payload_of(result)["count"] == 1
    assert _row(conn, "https://jobs.example/j1")["rerank_marked_at"] is not None
    assert _row(conn, "https://jobs.example/j2")["rerank_marked_at"] is None


def test_rerank_skips_undescribed_jobs_visibly(store):
    conn = store
    conn.execute("UPDATE jobs SET fit_score = 4, full_description = NULL"
                 " WHERE url = ?", ("https://jobs.example/j1",))
    conn.commit()

    result = run("jobs", "rerank", "--min-score", "1", "--json")

    payload = payload_of(result)
    # j1 matches the filter but cannot be re-judged without a captured
    # description: selected 0, skipped 1, marked 0 — visible, not silent.
    assert payload["count"] == 0
    assert payload["marked"] == 0
    assert payload["skipped"] == 1
    assert _row(conn, "https://jobs.example/j1")["rerank_marked_at"] is None


# ---------------------------------------------------------------------------
# Staleness — a resume change flags previously-scored jobs
# ---------------------------------------------------------------------------


def test_resume_change_flags_scores_stale(store, app_dir):
    # Both jobs scored against resume V1 — nothing is stale.
    assert _score_via_verb("https://jobs.example/j1", 7).exit_code == 0
    assert _score_via_verb("https://jobs.example/j2", 6).exit_code == 0
    fresh = payload_of(run("jobs", "filter", "--stale-only", "--json"))
    assert [job["url"] for job in fresh["jobs"]] == []

    (app_dir / "resume.txt").write_text(RESUME_V2, encoding="utf-8")
    stale = payload_of(run("jobs", "filter", "--stale-only", "--json"))
    assert sorted(job["url"] for job in stale["jobs"]) == [
        "https://jobs.example/j1", "https://jobs.example/j2"]
    assert stale["jobs"][0]["stale"] is True


# ---------------------------------------------------------------------------
# agent score --prev-score and the score_history sidecar
# ---------------------------------------------------------------------------


def test_prev_score_mismatch_refuses_and_persists_nothing(store):
    conn = store
    conn.execute(
        "UPDATE jobs SET fit_score = 4, scored_at = ?, scored_by = 'host' WHERE url = ?",
        ("2026-11-01T00:00:00+00:00", "https://jobs.example/j1"))
    conn.commit()

    result = _score_via_verb("https://jobs.example/j1", 8, "--rescore", "--prev-score", "5")

    assert result.exit_code == 1
    error = payload_of(result)["error"]
    assert error["code"] == "prev_score_mismatch"
    row = _row(conn, "https://jobs.example/j1")
    assert row["fit_score"] == 4  # nothing persisted
    assert row["score_history"] is None


def test_rescore_records_history_and_rescored_since_delta(store, app_dir):
    conn = store
    assert _score_via_verb("https://jobs.example/j1", 4).exit_code == 0
    _seed(conn, "https://jobs.example/j3", title="Never Rescored")
    conn.execute(
        "UPDATE jobs SET fit_score = 6, scored_at = ?, scored_by = 'host',"
        " score_history = ? WHERE url = ?",
        ("2026-11-01T00:00:00+00:00",
         json.dumps([{"prev_score": 5, "prev_score_hash": "old",
                      "prev_scored_at": "2026-10-31T00:00:00+00:00",
                      "rescored_at": (datetime.now(timezone.utc)
                                      - timedelta(days=2)).isoformat()}]),
         "https://jobs.example/j3"))
    conn.commit()

    # Resume changes → the score goes stale → rerank → re-judge.
    (app_dir / "resume.txt").write_text(RESUME_V2, encoding="utf-8")
    assert run("jobs", "rerank", "--stale-only").exit_code == 0
    assert _score_via_verb("https://jobs.example/j1", 8, "--rescore",
                           "--prev-score", "4").exit_code == 0

    row = _row(conn, "https://jobs.example/j1")
    history = parse_score_history(row["score_history"])
    assert len(history) == 1
    entry = history[0]
    assert entry["prev_score"] == 4
    assert entry["prev_score_hash"] == _hash(RESUME_V1)
    assert entry["prev_scored_at"] is not None
    assert entry["rescored_at"] is not None

    # --rescored-since reports old → new movement per job, and excludes
    # rows rescored outside the window (j3, two days ago) or never
    # rescored (j2).
    report = payload_of(run("jobs", "filter", "--rescored-since", "1h", "--json"))
    urls = [job["url"] for job in report["jobs"]]
    assert urls == ["https://jobs.example/j1"]
    delta = report["jobs"][0]
    assert delta["prev_score"] == 4
    assert delta["fit_score"] == 8
    assert delta["score_delta"] == 4


def test_score_history_caps_at_five_entries(app_dir, isolated_db):
    conn = database.init_db()
    _seed(conn, "https://jobs.example/cap")
    from applypilot.scoring.scorer import persist_score

    when = datetime.now(timezone.utc).isoformat()
    persist_score(conn, "https://jobs.example/cap", 1, "", "r", "host", when)
    for score in range(2, 9):  # seven rescores after the first persist
        persist_score(conn, "https://jobs.example/cap", score, "", "r",
                      "host", when)
    conn.commit()

    history = parse_score_history(_row(conn, "https://jobs.example/cap")["score_history"])
    assert len(history) == 5
    assert [entry["prev_score"] for entry in history] == [3, 4, 5, 6, 7]


def test_parse_score_history_tolerates_malformed_sidecars():
    assert parse_score_history(None) == []
    assert parse_score_history("") == []
    assert parse_score_history("not json") == []
    assert parse_score_history('{"a": 1}') == []  # non-list payload
    assert parse_score_history('[{"prev_score": 4}, "junk"]') == [{"prev_score": 4}]


# ---------------------------------------------------------------------------
# Plan interaction — staleness is advisory, tokens are untouched
# ---------------------------------------------------------------------------


def test_plan_staged_before_rerank_still_confirms(store):
    conn = store
    _seed(conn, "https://jobs.example/eligible", title="Eligible", fit_score=9,
          full_description="d" * 300, site="acme",
          application_url="https://ats.example/apply",
          tailored_resume_path="/tmp/fake/tailored.pdf")
    conn.commit()

    staged = payload_of(run("apply", "--plan"))
    assert staged["commits_nothing"] is True
    plan_hash = staged["plan_hash"]

    assert run("jobs", "rerank", "--min-score", "7").exit_code == 0
    assert _row(conn, "https://jobs.example/eligible")["rerank_marked_at"] is not None

    # The batch is unchanged, so the token still verifies — rerank
    # marking never disturbs plan semantics (spec locked decision).
    replanned = payload_of(run("apply", "--plan"))
    assert replanned["plan_hash"] == plan_hash
    confirmed = planning.verify_confirm(plan_hash, None, None, None)
    assert confirmed["urls"] == ["https://jobs.example/eligible"]
