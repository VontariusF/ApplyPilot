"""Integration tests for deep-filtering Layer 3 (spec art_we4AUxEy).

Resume-aware staleness, `jobs rerank`, the score_history sidecar, and
--rescored-since delta reporting: --dry-run commits nothing; the marked
set is what `agent job-next --stage score` serves first; re-scoring
through the host verbs records the prior entry and clears the mark; and
a plan staged before reranking still confirms against its original
token semantics.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from kestrel import database
from kestrel.apply import planning
from kestrel.cli import app
from kestrel.database import parse_score_history
from kestrel.scoring.scorer import resume_sha256

runner = CliRunner()

RESUME_V1 = "platform engineer with nine years of experience\n"
RESUME_V2 = "staff platform engineer with ten years of experience\n"


def run(*args):
    """Invoke `kestrel ...` and return the CliRunner result."""
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


def _committed_row(url):
    """Read one row through a brand-new connection — committed state only.

    The `store` fixture and the in-process CLI share one thread-local
    connection, so reads through it also see uncommitted writes. A
    second CLI process never would: this helper is the durability view
    everyone else gets. It is the fresh-connection assertion style the
    layer-3 audit required after the shared-connection blind spot hid
    the missing `conn.commit()` in `jobs rerank` (audit Finding 2).
    """
    import sqlite3

    conn = sqlite3.connect(str(database.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    finally:
        conn.close()


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
    # Durability view: a fresh connection sees only what a second CLI
    # process would — the mark must be committed, not in-flight.
    assert _committed_row("https://jobs.example/j2")["rerank_marked_at"] is None

    # The marked — already-scored — job is served FIRST, ahead of the
    # ordinary pending_score queue, and the payload says --rescore.
    job_next = payload_of(run("agent", "job-next", "--stage", "score"))
    assert job_next["job"]["url"] == "https://jobs.example/j1"
    assert job_next["rescore_required"] is True

    scored = _score_via_verb("https://jobs.example/j1", 8, "--rescore", "--prev-score", "4")
    assert scored.exit_code == 0
    row = _committed_row("https://jobs.example/j1")
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
    assert _committed_row("https://jobs.example/j2")["rerank_marked_at"] is not None
    assert _committed_row("https://jobs.example/j1")["rerank_marked_at"] is None

    # A fresh mark must not leak into the next selection: clear, then
    # target the stale set only — j1 never bound a hash (NULL = stale),
    # j2's is current (fresh).
    conn.execute("UPDATE jobs SET rerank_marked_at = NULL")
    conn.commit()
    result = run("jobs", "rerank", "--stale-only", "--json")
    assert payload_of(result)["count"] == 1
    assert _committed_row("https://jobs.example/j1")["rerank_marked_at"] is not None
    assert _committed_row("https://jobs.example/j2")["rerank_marked_at"] is None


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
    assert _committed_row("https://jobs.example/j1")["rerank_marked_at"] is None


def test_rerank_mark_persists_across_connections(store):
    """Blocker regression: a rerank mark must survive the CLI process.

    `mark_rerank`'s contract is "callers commit"; when `jobs rerank`
    didn't, the mark rolled back on process exit while the command
    still reported `marked: N` — invisible to this suite because the
    fixture's thread-local connection carried the uncommitted write.
    The assertion below reads through a brand-new connection, so it
    can only pass on committed state.
    """
    store.execute(
        "UPDATE jobs SET fit_score = 4, scored_at = ?, scored_by = 'host',"
        " scored_resume_hash = ? WHERE url = ?",
        ("2026-11-01T00:00:00+00:00", _hash(RESUME_V1), "https://jobs.example/j1"))
    store.commit()

    result = run("jobs", "rerank", "--min-score", "1", "--json")

    assert result.exit_code == 0
    assert payload_of(result)["marked"] == 1
    row = _committed_row("https://jobs.example/j1")
    assert row["rerank_marked_at"] is not None


def test_rerank_excludes_dismissed_jobs(store):
    """A dismissed row never re-enters the scoring loop through rerank.

    Dismissal is board-level curation (set_dismissed semantics): the
    ordinary pending_score queue can never serve a dismissed job, and
    the rerank path must not smuggle it back in — it is skipped
    visibly, in its own count, not silently folded into the targets.
    """
    conn = store
    conn.execute("UPDATE jobs SET fit_score = 4, dismissed_at = ?"
                 " WHERE url = ?",
                 ("2026-11-08T00:00:00+00:00", "https://jobs.example/j1"))
    conn.execute(
        "UPDATE jobs SET fit_score = 5, scored_at = ?, scored_by = 'host'"
        " WHERE url = ?", ("2026-11-08T00:00:00+00:00", "https://jobs.example/j2"))
    conn.commit()

    result = run("jobs", "rerank", "--min-score", "1", "--json")

    assert result.exit_code == 0
    payload = payload_of(result)
    # j1 (dismissed, described, scored) is skipped as dismissed; j2
    # (described, not dismissed) is the only remaining target.
    assert [job["url"] for job in payload["jobs"]] == ["https://jobs.example/j2"]
    assert payload["count"] == 1
    assert payload["marked"] == 1
    assert payload["skipped"] == 1
    assert payload["skipped_dismissed"] == 1
    assert _committed_row("https://jobs.example/j1")["rerank_marked_at"] is None


def test_mark_rerank_refuses_dismissed_rows_at_the_data_layer(app_dir, isolated_db):
    """Belt-and-suspenders: the UPDATE itself cannot mark a dismissed row,
    even when handed its URL directly."""
    conn = database.init_db()
    _seed(conn, "https://jobs.example/d1")
    _seed(conn, "https://jobs.example/d2")
    conn.execute("UPDATE jobs SET dismissed_at = ? WHERE url = ?",
                 ("2026-11-08T00:00:00+00:00", "https://jobs.example/d1"))
    conn.commit()

    marked = database.mark_rerank(
        conn, ["https://jobs.example/d1", "https://jobs.example/d2"],
        "2026-11-09T00:00:00+00:00")
    conn.commit()

    assert marked == 1
    assert _row(conn, "https://jobs.example/d1")["rerank_marked_at"] is None
    assert _row(conn, "https://jobs.example/d2")["rerank_marked_at"] is not None


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


def test_invalid_resume_bytes_hash_identically_on_both_paths(store, app_dir):
    """L3: both resume_sha256 consumers read with one strictness.

    A resume.txt holding bytes that are not valid UTF-8 must not split
    the hash consumers: the host score path stamps the replaced-text
    hash (it once raised into an internal_error envelope instead) and
    `jobs filter` staleness compares against the identical value.
    """
    (app_dir / "resume.txt").write_bytes(b"platform engineer \xff\xfe resume")

    assert _score_via_verb("https://jobs.example/j1", 7).exit_code == 0
    assert _score_via_verb("https://jobs.example/j2", 6).exit_code == 0

    # Both stamps are the hash of the replaced text — what the filter
    # side computes too.
    expected = resume_sha256(
        (app_dir / "resume.txt").read_text(encoding="utf-8", errors="replace"))
    assert _committed_row("https://jobs.example/j1")["scored_resume_hash"] == expected
    assert _committed_row("https://jobs.example/j2")["scored_resume_hash"] == expected

    # ...so neither freshly scored row is stale, on either side.
    assert payload_of(run("jobs", "filter", "--stale-only", "--json"))["jobs"] == []


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
    from kestrel.scoring.scorer import persist_score

    when = datetime.now(timezone.utc).isoformat()
    persist_score(conn, "https://jobs.example/cap", 1, "", "r", "host", when)
    for score in range(2, 9):  # seven rescores after the first persist
        persist_score(conn, "https://jobs.example/cap", score, "", "r",
                      "host", when)
    conn.commit()

    history = parse_score_history(_row(conn, "https://jobs.example/cap")["score_history"])
    assert len(history) == 5
    assert [entry["prev_score"] for entry in history] == [3, 4, 5, 6, 7]


def test_persist_score_history_survives_concurrent_rescores(store, monkeypatch):
    """L2: the sidecar read-modify-write is serialized per row.

    Two concurrent persists for one URL must not both read the same
    prior snapshot — the second waits at BEGIN IMMEDIATE and reads the
    first's committed row, so both history entries survive. The patched
    parse holds each reader inside the read-modify-write window long
    enough to make the losing-update interleaving deterministic instead
    of lucky.
    """
    import threading

    from kestrel.scoring.scorer import persist_score

    when = datetime.now(timezone.utc).isoformat()
    persist_score(store, "https://jobs.example/j1", 4, "", "r", "host", when)
    store.commit()

    real_parse = database.parse_score_history
    first_reader_inside = threading.Event()

    def slow_parse(raw):
        history = real_parse(raw)
        first_reader_inside.set()
        time.sleep(0.5)
        return history

    monkeypatch.setattr(database, "parse_score_history", slow_parse)

    errors = []

    def rescore(score, stamp):
        try:
            c = database.get_connection()
            persist_score(c, "https://jobs.example/j1", score, "", "r", "host", stamp)
            c.commit()
        except BaseException as exc:  # surfaced below — never swallowed
            errors.append(exc)

    t1 = threading.Thread(target=rescore, args=(5, "2026-11-09T00:00:01+00:00"))
    t1.start()
    assert first_reader_inside.wait(timeout=5)
    t2 = threading.Thread(target=rescore, args=(6, "2026-11-09T00:00:02+00:00"))
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()
    assert errors == []

    history = parse_score_history(
        _committed_row("https://jobs.example/j1")["score_history"])
    assert len(history) == 2  # the raced write once lost one of these
    assert history[0]["prev_score"] == 4


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
    assert _committed_row("https://jobs.example/eligible")["rerank_marked_at"] is not None

    # The batch is unchanged, so the token still verifies — rerank
    # marking never disturbs plan semantics (spec locked decision).
    replanned = payload_of(run("apply", "--plan"))
    assert replanned["plan_hash"] == plan_hash
    confirmed = planning.verify_confirm(plan_hash, None, None, None)
    assert confirmed["urls"] == ["https://jobs.example/eligible"]
