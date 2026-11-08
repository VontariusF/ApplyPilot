"""Regression tests for the round-3 audit remediation (findings docs
art_CxX8pt9C / art_GxFCvfLI, merged main 8c8d5ae).

Sections map one-to-one to finding IDs:
  B1+S1  — jobspy str(None) class: _clean_cell at every coercion site, plus
           the idempotent startup repair for already-polluted rows.
  B7+M1  — persist_description preserves an absent apply URL, normalizes
           junk sentinels, refuses non-http(s) values, and stamps a
           visible detail_error when no description was extracted.
  B7     — the host enrich verb refuses a non-URL --application-url with
           a typed envelope instead of writing it.
  B2+S3  — `apply --plan` sizes an omitted --limit to min(headroom,
           eligible) and the payload carries exclusion diagnostics.
  B3     — the natural confirm (no flags) of a plan staged with a
           non-default --min-score runs at the staged threshold.
  B4+B5  — reclaiming a crashed worker's lock consumes one of its
           attempts; consecutive engine-signal skips stop the run; the
           F-3 refund still leaves fresh claims fully retryable.
  B6+M4  — reset_failed preserves terminal (expired, login_issue, captcha)
           and manual rows.
  B8     — derive_batch/count_eligible carry the manual-ATS exclusion.
  B9+M3  — readiness counts treat a non-http(s) application_url as no URL,
           and carry the attempts and live min_score guards.
  H2     — the streaming cover pending count matches the runner's
           threshold.
  H3     — pending_detail (stats, streaming pending, portal stage filter)
           excludes skip-site rows the CLI enrich runner refuses.
"""

import json

import pandas as pd
import pytest
from typer.testing import CliRunner

from applypilot import config, database
from applypilot.apply import launcher, planning
from applypilot.cli import app
from applypilot.discovery import jobspy
from applypilot.enrichment import detail
from applypilot import pipeline

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def queue(app_dir):
    database.init_db()
    return database.get_connection()


@pytest.fixture()
def tier_ok(monkeypatch):
    """The Tier-3 gate passes (the confirm path's run checks)."""
    monkeypatch.setattr(config, "check_tier", lambda tier, feature: None)


def _seed_job(conn, url, title="Engineer", fit_score=9, **extra):
    cols = {
        "url": url,
        "title": title,
        "site": "acme",
        "fit_score": fit_score,
        "full_description": "d" * 300,
        "tailored_resume_path": f"/tmp/fake/{abs(hash(url))}.txt",
    }
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


def _row(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


def _plan(*args):
    return runner.invoke(app, ["apply", "--plan", *args])


def _confirm(plan_hash, *args):
    return runner.invoke(app, ["apply", "--i-confirm", plan_hash, *args])


def _error_of(result) -> dict:
    assert result.exit_code == 1
    return json.loads(result.stdout)["error"]


# ---------------------------------------------------------------------------
# B1+S1 — the str(None) class at the jobspy boundary
# ---------------------------------------------------------------------------


def test_jobspy_none_cells_store_sql_null_not_the_string_none(queue):
    """A Python-None DataFrame cell (object dtype — LinkedIn rows commonly
    carry no job_url_direct) must persist SQL NULL on every column, never
    the literal 'None' string the old str() != "nan" guard produced."""
    df = pd.DataFrame([{
        "job_url": "https://example.com/jobs/1",
        "title": None, "company": None, "location": None,
        "description": None, "site": None,
        "interval": None, "currency": None,
        "job_url_direct": None,
    }])

    new, _existing = jobspy.store_jobspy_results(queue, df, "TestBoard")
    assert new == 1

    row = _row(queue, "https://example.com/jobs/1")
    for col in ("title", "location", "description",
                "full_description", "application_url"):
        assert row[col] is None, col
    assert row["site"] == "TestBoard"  # falls to the source label, not 'None'
    assert row["salary"] is None


def test_jobspy_nan_cells_still_normalize_to_null(queue):
    """The pandas float-NaN leak (the string 'nan') keeps its old
    normalization on every column."""
    nan = float("nan")
    df = pd.DataFrame([{
        "job_url": "https://example.com/jobs/2",
        "title": nan, "company": nan, "location": nan,
        "description": nan, "job_url_direct": nan,
    }])

    jobspy.store_jobspy_results(queue, df, "TestBoard")

    row = _row(queue, "https://example.com/jobs/2")
    for col in ("title", "location", "description",
                "full_description", "application_url"):
        assert row[col] is None, col


def test_jobspy_real_values_pass_through(queue):
    df = pd.DataFrame([{
        "job_url": "https://example.com/jobs/3",
        "title": "Data Engineer",
        "company": "Example Co",
        "location": "Remote",
        "description": "d" * 300,
        "job_url_direct": "https://example.com/apply/3",
        "interval": "year", "currency": "USD",
        "min_amount": 100000, "max_amount": 120000,
    }])

    jobspy.store_jobspy_results(queue, df, "TestBoard")

    row = _row(queue, "https://example.com/jobs/3")
    assert row["title"] == "Data Engineer"
    assert row["application_url"] == "https://example.com/apply/3"
    assert row["full_description"].startswith("d")
    assert row["salary"].startswith("$100,000") or "100,000" in row["salary"]


def test_normalize_polluted_urls_repairs_the_b1_class(queue):
    """The startup repair NULLs every stored sentinel and fabricated
    '<base>/None' URL, preserves real URLs, and is idempotent."""
    cases = {
        "https://x.com/jobs/none-literal": "None",
        "https://x.com/jobs/na": "N/A",
        "https://x.com/jobs/lower-null": "null",
        "https://x.com/jobs/fabricated": "https://careers.example.com/None",
    }
    for url, polluted in cases.items():
        _seed_job(queue, url, application_url=polluted, fit_score=9)
    _seed_job(queue, "https://x.com/jobs/real",
              application_url="https://x.com/apply/real", fit_score=9)

    repaired = database.normalize_polluted_urls(queue)
    assert repaired == 4
    for url in cases:
        assert _row(queue, url)["application_url"] is None, url
    assert (_row(queue, "https://x.com/jobs/real")["application_url"]
            == "https://x.com/apply/real")

    # Idempotent: the second pass is a no-op.
    assert database.normalize_polluted_urls(queue) == 0


def test_init_db_heals_polluted_rows_on_startup(queue):
    """The repair rides init_db — an old store heals on the next start,
    no migration."""
    _seed_job(queue, "https://x.com/jobs/legacy", application_url="None")
    row = _row(queue, "https://x.com/jobs/legacy")
    assert row["application_url"] == "None"

    database.init_db()  # the fixture already ran it once; run "next startup"

    assert _row(queue, "https://x.com/jobs/legacy")["application_url"] is None


# ---------------------------------------------------------------------------
# B7+M1 — persist_description write semantics
# ---------------------------------------------------------------------------


def test_persist_description_without_url_preserves_the_existing_one(queue):
    """B7: the host enrich verb omits --application-url and the partial
    scrape tier never has one — neither may clobber a discovery-time URL
    (the Workday rows are exactly the rows that carry one)."""
    _seed_job(queue, "https://x.com/jobs/1", full_description=None,
              application_url="https://x.com/apply/1")

    detail.persist_description(queue, "https://x.com/jobs/1",
                               "d" * 300, None, "Engineer")

    row = _row(queue, "https://x.com/jobs/1")
    assert row["application_url"] == "https://x.com/apply/1"
    assert row["full_description"].startswith("d")
    assert row["detail_scraped_at"] is not None
    assert row["detail_error"] is None


def test_persist_description_normalizes_junk_urls_instead_of_writing(queue):
    _seed_job(queue, "https://x.com/jobs/2", full_description=None,
              application_url="https://x.com/apply/2")

    for junk in ("None", "none", "null", "n/a", ""):
        detail.persist_description(queue, "https://x.com/jobs/2",
                                   "d" * 300, junk, "Engineer")
        row = _row(queue, "https://x.com/jobs/2")
        assert row["application_url"] == "https://x.com/apply/2", junk


def test_persist_description_with_junk_and_no_existing_url_stores_null(queue):
    _seed_job(queue, "https://x.com/jobs/3", full_description=None,
              application_url=None)

    detail.persist_description(queue, "https://x.com/jobs/3",
                               "d" * 300, "None", "Engineer")

    assert _row(queue, "https://x.com/jobs/3")["application_url"] is None


def test_persist_description_refuses_a_non_http_value(queue):
    _seed_job(queue, "https://x.com/jobs/4", full_description=None,
              application_url=None)

    with pytest.raises(ValueError, match="non-http"):
        detail.persist_description(queue, "https://x.com/jobs/4",
                                   "d" * 300, "javascript:void(0)", "Engineer")
    assert _row(queue, "https://x.com/jobs/4")["full_description"] is None


def test_persist_description_fresh_url_overwrites(queue):
    _seed_job(queue, "https://x.com/jobs/5", full_description=None,
              application_url="https://x.com/apply/old")

    detail.persist_description(queue, "https://x.com/jobs/5",
                               "d" * 300, "https://x.com/apply/new", "Engineer")

    assert (_row(queue, "https://x.com/jobs/5")["application_url"]
            == "https://x.com/apply/new")


def test_persist_description_without_description_stamps_a_visible_error(queue):
    """M1: a NULL description must not settle the row invisibly — it stays
    in detail_errors instead of vanishing from every queue and counter."""
    _seed_job(queue, "https://x.com/jobs/6", full_description=None,
              application_url=None)

    detail.persist_description(queue, "https://x.com/jobs/6",
                               None, None, "Engineer")

    row = _row(queue, "https://x.com/jobs/6")
    assert row["detail_error"] == "no description extracted"
    assert row["detail_scraped_at"] is not None
    assert row["full_description"] is None


# ---------------------------------------------------------------------------
# B7 — the host enrich verb's typed refusal
# ---------------------------------------------------------------------------


def _run_enrich(*args):
    return runner.invoke(app, ["agent", "enrich", *args])


def test_host_enrich_without_application_url_preserves_the_existing_one(
        queue, seeded_profile):
    _seed_job(queue, "https://x.com/jobs/1", full_description=None,
              application_url="https://x.com/apply/1")

    result = _run_enrich("--url", "https://x.com/jobs/1",
                         "--full-description", "d" * 300)

    assert result.exit_code == 0, result.stdout
    row = _row(queue, "https://x.com/jobs/1")
    assert row["application_url"] == "https://x.com/apply/1"
    assert row["full_description"].startswith("d")


def test_host_enrich_refuses_a_non_url_application_url(queue, seeded_profile):
    """B7: junk sentinels are how the B1 pollution class entered the
    store — the verb refuses them with a typed envelope, writing nothing."""
    _seed_job(queue, "https://x.com/jobs/2", full_description=None,
              application_url=None)

    result = _run_enrich("--url", "https://x.com/jobs/2",
                         "--full-description", "d" * 300,
                         "--application-url", "None")

    error = _error_of(result)
    assert error["code"] == "invalid_application_url"
    assert error["details"]["given"] == "None"
    # Nothing was written: the capture never landed.
    row = _row(queue, "https://x.com/jobs/2")
    assert row["full_description"] is None
    assert row["application_url"] is None


# ---------------------------------------------------------------------------
# B2+S3 — plan sizing and exclusion diagnostics
# ---------------------------------------------------------------------------


def test_plan_without_limit_sizes_to_min_headroom_eligible(queue):
    """B2: an omitted --limit plans everything the cap allows — never the
    apply run's silent default of 1."""
    for i in range(1, 4):
        _seed_job(queue, f"https://x.com/jobs/{i}", fit_score=8)

    payload = json.loads(_plan().stdout)

    assert payload["batch_size"] == 3
    assert payload["limit_applied"] == 3
    assert payload["eligible_total"] == 3
    assert payload["headroom"] == planning.AGENT_DEFAULT_DAILY_CAP


def test_plan_without_limit_respects_the_cap_headroom(queue):
    for i in range(1, 4):
        _seed_job(queue, f"https://x.com/jobs/{i}", fit_score=8)
    for i in range(1, 5):
        _seed_job(queue, f"https://x.com/jobs/applied-{i}", fit_score=8)
        launcher.mark_job(f"https://x.com/jobs/applied-{i}", "applied")

    payload = json.loads(_plan().stdout)

    assert payload["headroom"] == 1  # cap 5 - 4 applied today
    assert payload["limit_applied"] == 1
    assert payload["batch_size"] == 1
    assert payload["eligible_total"] == 3


def test_plan_explicit_limit_is_honored(queue):
    for i in range(1, 4):
        _seed_job(queue, f"https://x.com/jobs/{i}", fit_score=8)

    payload = json.loads(_plan("--limit", "2").stdout)

    assert payload["batch_size"] == 2
    assert payload["limit_applied"] == 2
    assert payload["eligible_total"] == 3


def test_plan_payload_carries_exclusion_diagnostics(queue):
    """S3: 'why didn't it batch X?' is answerable from the payload."""
    _seed_job(queue, "https://x.com/jobs/eligible", fit_score=9)
    _seed_job(queue, "https://x.com/jobs/untailored", fit_score=9,
              tailored_resume_path=None)
    _seed_job(queue, "https://x.com/jobs/lowscore", fit_score=4)
    _seed_job(queue, "https://x.com/jobs/captcha", fit_score=9,
              apply_status="captcha")
    _seed_job(queue, "https://x.com/jobs/capped", fit_score=9,
              apply_status="failed", apply_attempts=5)
    _seed_job(queue, "https://x.com/jobs/manual", fit_score=9,
              application_url="https://ibegin.tcsapps.com/careers/123")
    _seed_job(queue, "https://x.com/jobs/blocked", fit_score=9,
              site="glassdoor")

    payload = json.loads(_plan().stdout)

    assert payload["eligible_total"] == 1
    excluded = payload["excluded"]
    assert excluded["not_tailored"] == 1
    assert excluded["score_below_min"] == 1
    assert excluded["captcha"] == 1
    assert excluded["attempt_capped"] == 1
    assert excluded["manual_ats"] == 1
    assert excluded["blocked"] == 1
    assert payload["skipped_manual_ats"] == 1


def test_plan_empty_error_carries_the_diagnostics(queue):
    error = _error_of(_plan())
    assert error["code"] == "plan_empty"
    assert error["details"]["eligible_total"] == 0
    assert "excluded" in error["details"]


# ---------------------------------------------------------------------------
# B3 — the natural confirm of a non-default --min-score plan
# ---------------------------------------------------------------------------


def test_natural_confirm_of_a_non_default_min_score_plan_runs(
        queue, seeded_profile, tier_ok, monkeypatch):
    """B3: `--plan --min-score 6` then a bare `--i-confirm <hash>` must
    run — the CLI default (7) may not ride into the drift check."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=7)
    _seed_job(queue, "https://x.com/jobs/2", fit_score=6)

    payload = json.loads(_plan("--min-score", "6").stdout)
    assert payload["min_score"] == 6
    assert payload["batch_size"] == 2

    calls: list[dict] = []
    monkeypatch.setattr(launcher, "main", lambda **kwargs: calls.append(kwargs))

    confirmed = _confirm(payload["plan_hash"])
    assert confirmed.exit_code == 0, confirmed.stdout
    assert len(calls) == 1
    # The run filters at the STAGED threshold, not the CLI default.
    assert calls[0]["min_score"] == 6
    assert calls[0]["allowed_urls"] == {"https://x.com/jobs/1",
                                        "https://x.com/jobs/2"}
    assert calls[0]["limit"] == 2


def test_explicit_min_score_agreeing_with_the_plan_still_confirms(
        queue, seeded_profile, tier_ok, monkeypatch):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=6)
    payload = json.loads(_plan("--min-score", "6").stdout)

    monkeypatch.setattr(launcher, "main", lambda **kwargs: None)

    confirmed = _confirm(payload["plan_hash"], "--min-score", "6")
    assert confirmed.exit_code == 0, confirmed.stdout


def test_explicit_min_score_disagreeing_with_the_plan_refuses(queue, tier_ok):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=6)
    payload = json.loads(_plan("--min-score", "6").stdout)

    error = _error_of(_confirm(payload["plan_hash"], "--min-score", "7"))
    assert error["code"] == "plan_drift"
    assert error["details"]["staged_min_score"] == 6
    assert error["details"]["given_min_score"] == 7


# ---------------------------------------------------------------------------
# B4+B5 — attempts on the crash/skip/refund paths
# ---------------------------------------------------------------------------


def test_fresh_claim_leaves_attempts_untouched(queue):
    """A fresh claim doesn't consume an attempt: the reported outcome
    counts it (one attempt per failed cycle, not two)."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9)
    before = _row(queue, "https://x.com/jobs/1")["apply_attempts"]

    job = launcher.acquire_job(min_score=7)

    assert job is not None
    assert _row(queue, "https://x.com/jobs/1")["apply_attempts"] == before


def test_stale_reclaim_consumes_an_attempt(queue):
    """B4: a dead worker's silent death becomes a counted attempt at
    reclaim time — the reclaim→crash→reclaim loop is bounded by the
    existing attempt budget."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9,
              apply_status="in_progress",
              last_attempted_at="2020-01-01T00:00:00+00:00",
              apply_attempts=0, apply_task_id="task-dead")

    job = launcher.acquire_job(min_score=7)
    assert job is not None
    row = _row(queue, "https://x.com/jobs/1")
    assert row["apply_attempts"] == 1
    assert row["apply_task_id"] == "reclaimed"


def test_refund_after_a_reclaim_returns_to_the_prior_count(queue):
    """B5: the F-3 refund exactly undoes a reclaim increment."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9,
              apply_status="in_progress",
              last_attempted_at="2020-01-01T00:00:00+00:00",
              apply_attempts=0, apply_task_id="task-dead")
    launcher.acquire_job(min_score=7)
    assert _row(queue, "https://x.com/jobs/1")["apply_attempts"] == 1

    launcher._refund_attempt("https://x.com/jobs/1")

    assert _row(queue, "https://x.com/jobs/1")["apply_attempts"] == 0


def test_consecutive_engine_skips_stop_the_run(queue, monkeypatch):
    """B4: a deterministic engine-signal death used to re-acquire the same
    queue-head job forever (finite runs never reached the limit). Three
    consecutive skips stop the run loudly."""
    for i in range(1, 6):
        _seed_job(queue, f"https://x.com/jobs/{i}", fit_score=8)

    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "kill_all_chrome", lambda *a, **k: None)
    run_job_calls: list[str] = []

    def fake_run_job(job, **kwargs):
        run_job_calls.append(job["url"])
        return "skipped", 0

    monkeypatch.setattr(launcher, "run_job", fake_run_job)
    events: list[str] = []
    monkeypatch.setattr(launcher, "add_event", lambda msg: events.append(msg))

    applied, failed = launcher.worker_loop(limit=10, min_score=7, dry_run=True)

    assert (applied, failed) == (0, 0)
    assert len(run_job_calls) == launcher._MAX_CONSECUTIVE_SKIPS
    # Every skipped job's lock was released — fully retryable.
    statuses = [tuple(r) for r in queue.execute(
        "SELECT url, apply_status FROM jobs").fetchall()]
    assert all(status is None for _url, status in statuses)
    assert any("Stopping: the engine died" in event for event in events)


def test_a_success_resets_the_consecutive_skip_counter(queue, monkeypatch):
    """The cap is consecutive: a success between skips means the run
    keeps going."""
    for i in range(1, 8):
        _seed_job(queue, f"https://x.com/jobs/{i}", fit_score=8)

    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "kill_all_chrome", lambda *a, **k: None)
    outcomes = iter(["skipped", "skipped", "applied"] * 3)

    def fake_run_job(job, **kwargs):
        return next(outcomes), 0

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    applied, _failed = launcher.worker_loop(limit=9, min_score=7, dry_run=True)

    assert applied == 3  # every applied outcome ran; skips never stopped the run


# ---------------------------------------------------------------------------
# B6+M4 — reset_failed's carve-outs
# ---------------------------------------------------------------------------


def test_reset_failed_preserves_terminal_and_manual_rows(queue):
    """B6+M4: the vocabulary's terminal statuses and the user's own manual
    marks survive --reset-failed; only plain failed rows re-enqueue."""
    _seed_job(queue, "https://x.com/jobs/failed", fit_score=9,
              apply_status="failed", apply_error="timeout", apply_attempts=2)
    _seed_job(queue, "https://x.com/jobs/expired", fit_score=9,
              apply_status="expired", apply_attempts=99)
    _seed_job(queue, "https://x.com/jobs/login", fit_score=9,
              apply_status="login_issue", apply_attempts=99)
    _seed_job(queue, "https://x.com/jobs/manual", fit_score=9,
              apply_status="manual", apply_error="manual ATS", apply_attempts=1)
    _seed_job(queue, "https://x.com/jobs/captcha", fit_score=9,
              apply_status="captcha", apply_attempts=99)
    _seed_job(queue, "https://x.com/jobs/applied", fit_score=9,
              apply_status="applied")

    count = launcher.reset_failed()
    assert count == 1

    failed_row = _row(queue, "https://x.com/jobs/failed")
    assert failed_row["apply_status"] is None
    assert failed_row["apply_attempts"] == 0
    for name in ("expired", "login", "manual", "captcha", "applied"):
        row = _row(queue, f"https://x.com/jobs/{name}")
        assert row["apply_status"] == name if name != "login" else "login_issue", name
    assert _row(queue, "https://x.com/jobs/expired")["apply_attempts"] == 99
    assert _row(queue, "https://x.com/jobs/manual")["apply_status"] == "manual"


def test_reset_failed_counts_the_rows_it_touched(queue):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9,
              apply_status="failed", apply_attempts=1)
    _seed_job(queue, "https://x.com/jobs/2", fit_score=9,
              apply_status="failed", apply_attempts=3)

    assert launcher.reset_failed() == 2


# ---------------------------------------------------------------------------
# B8 — derive_batch carries the manual-ATS exclusion
# ---------------------------------------------------------------------------


def test_derive_batch_and_count_eligible_exclude_manual_ats(queue):
    """B8: the plan can no longer stage a job acquire_job would refuse."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9)
    _seed_job(queue, "https://x.com/jobs/manual", fit_score=10,
              application_url="https://ibegin.tcsapps.com/careers/123")

    batch = launcher.derive_batch(min_score=7, limit=10)
    assert [job["url"] for job in batch] == ["https://x.com/jobs/1"]
    assert launcher.count_eligible(min_score=7) == 1


def test_plan_reports_skipped_manual_ats(queue):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9)
    _seed_job(queue, "https://x.com/jobs/manual", fit_score=10,
              application_url="https://ibegin.tcsapps.com/careers/123")

    payload = json.loads(_plan().stdout)

    assert payload["batch_size"] == 1
    assert payload["skipped_manual_ats"] == 1
    # And the manual-ATS job is not in the batch the user confirms.
    assert [job["url"] for job in payload["jobs"]] == ["https://x.com/jobs/1"]


def test_queue_where_excludes_manual_ats_domains(queue):
    """The one selection truth: acquire_job's own WHERE never picks a
    manual-ATS URL, so the explicit --url path remains the only way one
    gets marked 'manual'."""
    _seed_job(queue, "https://x.com/jobs/manual", fit_score=10,
              application_url="https://ibegin.tcsapps.com/careers/123")

    where, params = launcher._queue_where(7)
    row = queue.execute(
        f"SELECT url FROM jobs {where}", params).fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# B9+M3 — readiness predicates
# ---------------------------------------------------------------------------


def test_readiness_counts_reject_non_http_urls(queue):
    """B9: the B1 literal 'None' (and any other non-http(s) value) is not
    an apply target — ready_to_apply/pending_apply must not count it."""
    _seed_job(queue, "https://x.com/jobs/none", fit_score=9,
              application_url="None")

    def _pending_apply(min_score: int | None = None) -> int:
        threshold = min_score or config.DEFAULTS["min_score"]
        return queue.execute(
            f"SELECT COUNT(*) FROM jobs "
            f"WHERE {database.STAGE_CONDITIONS['pending_apply']}",
            (threshold,),
        ).fetchone()[0]

    stats = database.get_stats(queue)
    assert stats["ready_to_apply"] == 0
    assert _pending_apply() == 0

    queue.execute("UPDATE jobs SET application_url = 'https://x.com/apply' "
                  "WHERE url = 'https://x.com/jobs/none'")
    queue.commit()
    stats = database.get_stats(queue)
    assert stats["ready_to_apply"] == 1
    assert _pending_apply() == 1


def test_untailored_eligible_respects_the_attempt_budget(queue):
    """M3(a): 'Pending tailoring (7+)' must not count rows the tailor
    queue will never run."""
    _seed_job(queue, "https://x.com/jobs/exhausted", fit_score=9,
              tailored_resume_path=None, tailor_attempts=5)

    stats = database.get_stats(queue)
    assert stats["untailored_eligible"] == 0


def test_untailored_eligible_counts_retryable_rows(queue):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9,
              tailored_resume_path=None, tailor_attempts=2)

    stats = database.get_stats(queue)
    assert stats["untailored_eligible"] == 1


def test_ready_to_apply_respects_the_live_min_score(queue, monkeypatch):
    """M3(b): a tailored job rescored below the threshold leaves the
    ready/pending counters — they carry the same live guard as the queue."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=5,
              application_url="https://x.com/apply/1")

    def _pending_apply(min_score: int | None = None) -> int:
        threshold = min_score or config.DEFAULTS["min_score"]
        return queue.execute(
            f"SELECT COUNT(*) FROM jobs "
            f"WHERE {database.STAGE_CONDITIONS['pending_apply']}",
            (threshold,),
        ).fetchone()[0]

    stats = database.get_stats(queue)
    assert stats["ready_to_apply"] == 0
    assert _pending_apply() == 0

    monkeypatch.setitem(config.DEFAULTS, "min_score", 5)
    stats = database.get_stats(queue)
    assert stats["ready_to_apply"] == 1
    assert _pending_apply() == 1


# ---------------------------------------------------------------------------
# H2 — the streaming cover pending count matches the runner threshold
# ---------------------------------------------------------------------------


def test_pending_cover_count_carries_the_score_threshold(queue):
    """H2: a tailored job rescored below the threshold is not pending
    cover work — the old count omitted the threshold, so `run --stream`
    settled nothing three passes in a row and B6 failed the stage."""
    _seed_job(queue, "https://x.com/jobs/subthreshold", fit_score=5,
              cover_letter_path=None)

    assert pipeline._count_pending("cover", min_score=7) == 0
    assert pipeline._count_pending("cover", min_score=5) == 1


def test_pending_cover_count_matches_the_pending_cover_queue(queue):
    _seed_job(queue, "https://x.com/jobs/ready", fit_score=9,
              cover_letter_path=None)

    assert pipeline._count_pending("cover", min_score=7) == 1
    assert launcher.count_eligible(min_score=7) == 1  # the apply queue agrees


# ---------------------------------------------------------------------------
# H3 — pending_detail excludes skip-site rows
# ---------------------------------------------------------------------------


def test_pending_detail_excludes_skip_sites_everywhere(queue):
    """H3: a glassdoor row with a short description is never scraped by
    the CLI enrich runner — it must not count as pending on any surface
    (stats, streaming pending count, portal stage filter), or `run
    --stream` never settles the enrich stage."""
    _seed_job(queue, "https://x.com/jobs/glassdoor", site="glassdoor",
              full_description=None, detail_scraped_at=None)
    _seed_job(queue, "https://x.com/jobs/normal", site="acme",
              full_description=None, detail_scraped_at=None)

    stats = database.get_stats(queue)
    assert stats["pending_detail"] == 1  # the normal row only

    assert pipeline._count_pending("enrich") == 1

    from applypilot.database import STAGE_CONDITIONS
    portal_pending = queue.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {STAGE_CONDITIONS['pending_detail']}"
    ).fetchone()[0]
    assert portal_pending == 1


def test_skip_site_rows_still_show_in_total(queue):
    """The exclusion is from *pending enrich work*, not from the store."""
    _seed_job(queue, "https://x.com/jobs/glassdoor", site="glassdoor",
              full_description=None, detail_scraped_at=None)

    stats = database.get_stats(queue)
    assert stats["total"] == 1
    assert stats["pending_detail"] == 0
