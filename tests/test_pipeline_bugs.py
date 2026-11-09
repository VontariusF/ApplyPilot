"""Remediation tests for sweep section 1 bugs (B2, B5-B12, B14-B16).

One test per bug per the spec's verification table (blueprint art_BmQxJjjF,
section C). Test names carry the bug id; the CI gate keeps the suite honest —
removing or skipping any of these fails the build.
"""

import json
import logging
import sqlite3
import sys
import threading
import types
import urllib.error

import httpx
import pandas as pd
import pytest

import kestrel.database as database
import kestrel.pipeline as pipeline
import kestrel.scoring.scorer as scorer


# ---------------------------------------------------------------------------
# Fixtures/helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def jobspy_stub(monkeypatch):
    """Stub the python-jobspy package so the jobspy discovery module imports."""
    stub = types.ModuleType("jobspy")
    stub.scrape_jobs = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "jobspy", stub)
    yield stub


class _FakeResponse:
    """Minimal httpx.Response stand-in for LLM parsing tests."""

    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )

    def json(self):
        return self._data


class _FakeHttpxClient:
    """Returns a canned JSON payload from post() — no network."""

    def __init__(self, data, status_code=200):
        self._response = _FakeResponse(data, status_code)

    def post(self, *args, **kwargs):
        return self._response


def _seed_job(conn, url, title="Engineer", **extra):
    cols = {"url": url, "title": title, "site": "acme", "full_description": "d" * 300}
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# B2 — scoring errors leave fit_score NULL (job stays in the retry queue)
# ---------------------------------------------------------------------------


def test_b2_scoring_error_keeps_fit_score_null(isolated_db, monkeypatch, tmp_path):
    import kestrel.scoring.scorer as scorer

    conn = database.init_db()
    _seed_job(conn, "https://x/1")

    resume = tmp_path / "resume.txt"
    resume.write_text("resume text", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)

    class _Boom:
        def chat(self, *args, **kwargs):
            raise RuntimeError("llm down")

    monkeypatch.setattr(scorer, "get_client", lambda: _Boom())

    result = scorer.run_scoring()

    assert result["errors"] == 1
    row = conn.execute(
        "SELECT fit_score FROM jobs WHERE url = 'https://x/1'"
    ).fetchone()
    assert row["fit_score"] is None, "errored job must not be persisted as scored-0"

    # The job reappears in the pending-score queue (retryable, not silently lost)
    pending = database.get_jobs_by_stage(conn=conn, stage="pending_score")
    assert [j["url"] for j in pending] == ["https://x/1"]


def test_b2_successful_score_still_persists(isolated_db, monkeypatch, tmp_path):
    conn = database.init_db()
    _seed_job(conn, "https://x/2")

    resume = tmp_path / "resume.txt"
    resume.write_text("resume text", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)

    class _Ok:
        def chat(self, *args, **kwargs):
            return "SCORE: 8\nKEYWORDS: python, sql\nREASONING: good fit"

    monkeypatch.setattr(scorer, "get_client", lambda: _Ok())

    result = scorer.run_scoring()
    assert result["scored"] == 1 and result["errors"] == 0
    row = conn.execute(
        "SELECT fit_score FROM jobs WHERE url = 'https://x/2'"
    ).fetchone()
    assert row["fit_score"] == 8


# ---------------------------------------------------------------------------
# B5 — URL-resolution collision must not delete rows
# ---------------------------------------------------------------------------


def test_b5_url_collision_keeps_both_rows_no_delete(isolated_db, monkeypatch):
    import kestrel.enrichment.detail as detail

    conn = database.init_db()
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description) VALUES (?, ?, ?, ?)",
        ("https://x/rich", "Rich", "acme", "the richer record"),
    )
    conn.execute(
        "INSERT INTO jobs (url, title, site) VALUES (?, ?, ?)",
        ("x/relative", "Rel", "acme"),
    )
    conn.commit()

    monkeypatch.setattr(detail, "resolve_url", lambda url, site: "https://x/rich")

    stats = detail.resolve_all_urls(conn)

    assert stats["collisions"] == 1
    count = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert count == 2, "collision must not delete either row"

    flagged = conn.execute(
        "SELECT detail_error FROM jobs WHERE url = 'x/relative'"
    ).fetchone()["detail_error"]
    assert flagged and "collision" in flagged

    rich = conn.execute(
        "SELECT full_description FROM jobs WHERE url = 'https://x/rich'"
    ).fetchone()["full_description"]
    assert rich == "the richer record"


# ---------------------------------------------------------------------------
# B6 + B7 — streaming stage: bounded retries, error status recorded
# ---------------------------------------------------------------------------


def test_b6_poison_job_stops_after_bounded_passes(monkeypatch):
    tracker = pipeline._StageTracker()
    calls = {"n": 0}

    def _poison(**kwargs):
        calls["n"] += 1
        raise RuntimeError("deterministic crash")

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", _poison)
    monkeypatch.setattr(pipeline, "_count_pending", lambda stage, min_score=7: 1)
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)

    pipeline._run_stage_streaming("score", tracker, threading.Event())

    assert calls["n"] == pipeline._MAX_ERROR_PASSES, (
        "runner must be retried a bounded number of times, not forever"
    )
    result = tracker.get_results()["score"]
    assert result["status"].startswith("error")


def test_b7_streaming_stage_error_reaches_summary(monkeypatch):
    """The error status must land in the run summary's errors dict (exit code 1)."""
    tracker = pipeline._StageTracker()

    def _poison(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", _poison)
    monkeypatch.setattr(pipeline, "_count_pending", lambda stage, min_score=7: 1)
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)

    pipeline._run_stage_streaming("score", tracker, threading.Event())

    status = tracker.get_results()["score"]["status"]
    # Same classification _run_streaming uses to populate its errors dict:
    assert status not in ("ok", "partial", "skipped")
    assert status.startswith("error")


def test_b7_healthy_stage_still_reports_ok(monkeypatch):
    tracker = pipeline._StageTracker()

    def _fine(**kwargs):
        return None

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", _fine)
    monkeypatch.setattr(pipeline, "_count_pending", lambda stage, min_score=7: 0)
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)
    tracker.mark_done("enrich")  # upstream done

    pipeline._run_stage_streaming("score", tracker, threading.Event())
    assert tracker.get_results()["score"]["status"] == "ok"


# ---------------------------------------------------------------------------
# P1-1 — zero-progress passes (a dead provider's swallowed errors) stop the
# tailor/cover stages loudly instead of burning the queue's attempt budget
# ---------------------------------------------------------------------------


def test_p1_zero_progress_passes_stop_tailor_stage_after_bounded_passes(monkeypatch):
    """A runner that returns normally while settling nothing (every per-job
    LLM call failed and the runner swallowed it) must count toward the same
    consecutive-error cap as a raising runner — the stage stops with a
    recorded error instead of looping at ~1 pass/sec while the queue's
    attempt budget burns."""
    tracker = pipeline._StageTracker()
    calls = {"n": 0}

    def _swallowed_failure(**kwargs):
        calls["n"] += 1
        return {"status": "ok", "approved": 0}  # returns normally, settles nothing

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "tailor", _swallowed_failure)
    monkeypatch.setattr(pipeline, "_count_pending",
                        lambda stage, min_score=7: 3)  # never shrinks
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pipeline, "_STREAM_BUSY_INTERVAL", 0.01)
    tracker.mark_done("score")  # tailor's upstream

    pipeline._run_stage_streaming("tailor", tracker, threading.Event())

    assert calls["n"] == pipeline._MAX_ERROR_PASSES, (
        "zero-progress passes must be bounded by the same cap as raising passes"
    )
    result = tracker.get_results()["tailor"]
    assert result["status"].startswith("error")
    assert "pending" in result["status"]


def test_p1_partial_progress_resets_the_zero_progress_error_count(monkeypatch):
    """A pass that settles at least one job resets the consecutive-error
    count — only entirely unproductive passes accrue toward the cap, so a
    slow-but-working provider is never killed by this guard."""
    tracker = pipeline._StageTracker()
    pending = {"n": 3}

    def _halting_progress(**kwargs):
        pending["n"] = max(pending["n"] - 1, 0)

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "tailor", _halting_progress)
    monkeypatch.setattr(pipeline, "_count_pending",
                        lambda stage, min_score=7: pending["n"])
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pipeline, "_STREAM_BUSY_INTERVAL", 0.01)
    tracker.mark_done("score")

    pipeline._run_stage_streaming("tailor", tracker, threading.Event())
    assert tracker.get_results()["tailor"]["status"] == "ok"


def test_p1_pdf_stage_still_tolerates_zero_progress_passes(monkeypatch):
    """The pdf stage legitimately leaves rows pending between budgeted
    passes while its reconciliation catches up — the zero-progress guard
    must stay scoped to tailor/cover."""
    tracker = pipeline._StageTracker()
    calls = {"n": 0}
    pending = {"n": 2}

    def _budgeted(**kwargs):
        calls["n"] += 1
        # Reconciliation settles the rows a few passes in (the real stage's
        # parity checks do this against the on-disk state).
        if calls["n"] >= 4:
            pending["n"] = 0
        return {"status": "ok", "converted": 0}  # settles nothing this pass

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "pdf", _budgeted)
    monkeypatch.setattr(pipeline, "_count_pending",
                        lambda stage, min_score=7: pending["n"])
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pipeline, "_STREAM_BUSY_INTERVAL", 0.01)
    tracker.mark_done("cover")  # pdf's upstream

    pipeline._run_stage_streaming("pdf", tracker, threading.Event())
    assert calls["n"] > pipeline._MAX_ERROR_PASSES  # not killed by the guard
    assert tracker.get_results()["pdf"]["status"] == "ok"


# ---------------------------------------------------------------------------
# B8 — LLM response parsing guards (Gemini native + OpenAI compat)
# ---------------------------------------------------------------------------


def _gemini_client(data):
    from kestrel.llm import LLMClient

    client = LLMClient("http://test", "gemini-2.0-flash", "key")
    client._client = _FakeHttpxClient(data)
    return client


def test_b8_gemini_empty_candidates_safety_block():
    from kestrel.llm import LLMParseError

    client = _gemini_client({"candidates": []})
    with pytest.raises(LLMParseError, match="no candidates"):
        client._chat_native_gemini(
            [{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=16
        )


def test_b8_gemini_function_call_part():
    from kestrel.llm import LLMParseError

    client = _gemini_client({
        "candidates": [{
            "content": {"parts": [{"functionCall": {"name": "x", "args": {}}}]},
        }]
    })
    with pytest.raises(LLMParseError, match="no text parts"):
        client._chat_native_gemini(
            [{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=16
        )


def test_b8_compat_null_content():
    from kestrel.llm import LLMClient, LLMParseError

    resp = _FakeResponse({"choices": [{"message": {"content": None}}]})
    with pytest.raises(LLMParseError, match="null/non-text"):
        LLMClient._handle_compat_response(resp)


def test_b8_compat_normal_content_still_works():
    from kestrel.llm import LLMClient

    resp = _FakeResponse({"choices": [{"message": {"content": "hello"}}]})
    assert LLMClient._handle_compat_response(resp) == "hello"


# ---------------------------------------------------------------------------
# B9 — Qwen preprocessing tolerates None first-message content
# ---------------------------------------------------------------------------


def test_b9_qwen_none_content_no_attribute_error():
    from kestrel.llm import LLMClient

    client = LLMClient("http://test", "Qwen/Qwen2.5-7B", None)
    client._client = _FakeHttpxClient({"choices": [{"message": {"content": "ok"}}]})

    out = client.chat([{"role": "user", "content": None}, {"role": "user", "content": "hi"}])
    assert out == "ok"


def test_b9_qwen_string_content_still_gets_no_think():
    from kestrel.llm import LLMClient

    client = LLMClient("http://test", "Qwen/Qwen2.5-7B", None)
    captured = {}

    class _Capture(_FakeHttpxClient):
        def post(self, url, json=None, **kwargs):
            captured["messages"] = json["messages"]
            return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    client._client = _Capture({})
    client.chat([{"role": "user", "content": "summarize"}])
    assert captured["messages"][0]["content"].startswith("/no_think")


# ---------------------------------------------------------------------------
# B10 — one malformed salary cell must not abort the JobSpy batch
# ---------------------------------------------------------------------------


def test_b10_bad_salary_cell_skips_row_keeps_batch(isolated_db, jobspy_stub):
    import kestrel.discovery.jobspy as jobspy_mod

    conn = database.init_db()
    df = pd.DataFrame([
        {"job_url": "https://x/1", "title": "Good", "min_amount": 50000, "max_amount": 70000},
        {"job_url": "https://x/2", "title": "Bad", "min_amount": "abc", "max_amount": "nan"},
    ])

    new, _existing = jobspy_mod.store_jobspy_results(conn, df, "test")

    assert new == 2, "the malformed salary row must not abort the batch"
    salaries = {
        row["url"]: row["salary"]
        for row in conn.execute("SELECT url, salary FROM jobs").fetchall()
    }
    assert salaries["https://x/1"] == "50,000-70,000"
    assert salaries["https://x/2"] is None


# ---------------------------------------------------------------------------
# B11 — empty/absent location_accept means "accept all non-remote"
# ---------------------------------------------------------------------------


def test_b11_empty_accept_passes_non_remote_city():
    from kestrel.discovery.filters import location_ok

    assert location_ok("San Francisco, CA", [], []) is True
    assert location_ok("New York, NY", None or [], []) is True


def test_b11_explicit_accept_still_filters():
    from kestrel.discovery.filters import location_ok

    assert location_ok("New York, NY", ["new york"], []) is True
    assert location_ok("Boston, MA", ["new york"], []) is False


def test_b11_reject_still_applies_with_empty_accept():
    from kestrel.discovery.filters import location_ok

    assert location_ok("Boston, MA", [], ["boston"]) is False
    assert location_ok("Remote", [], ["boston"]) is True


def test_b11_location_filters_handles_absent_and_none():
    from kestrel.discovery.filters import location_filters

    assert location_filters({}) == ([], [])
    assert location_filters({"location_accept": None}) == ([], [])


# Dogfood 2026-11-09: only the flat keys were read, so both other config
# shapes silently disabled location filtering — a wizard-generated config
# stored ~3,350 out-of-area employer-site jobs.


def test_b11_example_file_nested_shape_is_read():
    from kestrel.discovery.filters import location_filters

    cfg = {"location": {"accept_patterns": ["San Francisco"],
                        "reject_patterns": ["London"]}}
    assert location_filters(cfg) == (["San Francisco"], ["London"])


def test_b11_wizard_shape_derives_accept_patterns():
    from kestrel.discovery.filters import location_filters

    cfg = {"locations": [{"location": "New York, NY", "remote": False},
                         {"location": "Remote", "remote": True}]}
    accept, reject = location_filters(cfg)
    # Full string plus each comma part; the Remote entry contributes
    # nothing (remote jobs always pass).
    assert accept == ["New York, NY", "New York", "NY"]
    assert reject == []


def test_b11_wizard_shape_falls_back_to_defaults_location():
    from kestrel.discovery.filters import location_filters

    cfg = {"defaults": {"location": "Austin, TX"}}
    assert location_filters(cfg)[0] == ["Austin, TX", "Austin", "TX"]


def test_b11_flat_keys_win_over_derived_and_nested():
    from kestrel.discovery.filters import location_filters

    cfg = {"location_accept": [],
           "location_reject_non_remote": ["London"],
           "location": {"accept_patterns": ["SF"]},
           "locations": [{"location": "Austin, TX"}]}
    # Explicit flat keys — even an empty accept list — keep B11 semantics.
    assert location_filters(cfg) == ([], ["London"])


def test_b11_derived_patterns_filter_workday_postings():
    from kestrel.discovery.filters import location_filters, location_ok

    cfg = {"locations": [{"location": "New York, NY", "remote": False}]}
    accept, reject = location_filters(cfg)

    assert location_ok("New York, NY, USA", accept, reject) is True
    assert location_ok("Brooklyn, NY", accept, reject) is True
    assert location_ok("Santa Clara, CA, USA", accept, reject) is False
    assert location_ok("Bangalore, India", accept, reject) is False
    assert location_ok("Remote — NYC", accept, reject) is True


def test_b11_all_sources_share_one_implementation(jobspy_stub):
    from kestrel.discovery import jobspy as jobspy_mod
    from kestrel.discovery import workday as workday_mod
    from kestrel.discovery.filters import location_ok

    assert jobspy_mod._location_ok is location_ok
    assert workday_mod._location_ok is location_ok


# ---------------------------------------------------------------------------
# B12 — nullable job title must not crash hot paths
# ---------------------------------------------------------------------------


def test_b12_job_title_helper_guards_none():
    from kestrel.apply.launcher import _job_title

    assert _job_title({"title": None}) == "untitled"
    assert _job_title({}) == "untitled"
    assert _job_title({"title": "Senior Engineer"}) == "Senior Engineer"
    assert _job_title({"title": None})[:30] == "untitled"


def test_b12_tailor_null_title_error_path_no_typeerror(isolated_db, monkeypatch, tmp_path):
    import kestrel.scoring.tailor as tailor

    conn = database.init_db()
    _seed_job(conn, "https://x/1", title=None, fit_score=10)

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    monkeypatch.setattr(tailor, "RESUME_PATH", resume)
    monkeypatch.setattr(tailor, "load_profile", lambda: {})

    class _Boom:
        def chat(self, *args, **kwargs):
            raise RuntimeError("no llm configured")

    monkeypatch.setattr(tailor, "get_client", lambda: _Boom())

    result = tailor.run_tailoring()

    assert result["errors"] == 1, (
        "the original error must be reported — previously the error handler "
        "itself crashed on the None title and masked it"
    )


def test_b12_cover_letter_null_title_error_path_no_typeerror(isolated_db, monkeypatch, tmp_path):
    import kestrel.scoring.cover_letter as cover_mod

    conn = database.init_db()
    _seed_job(conn, "https://x/1", title=None, fit_score=10, tailored_resume_path="/tmp/t.txt")

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    monkeypatch.setattr(cover_mod, "RESUME_PATH", resume)
    monkeypatch.setattr(cover_mod, "load_profile", lambda: {})

    class _Boom:
        def chat(self, *args, **kwargs):
            raise RuntimeError("no llm configured")

    monkeypatch.setattr(cover_mod, "get_client", lambda: _Boom())

    result = cover_mod.run_cover_letters()

    assert result["errors"] == 1


# ---------------------------------------------------------------------------
# B14 — Workday retry with backoff; truncation surfaced as a warning
# ---------------------------------------------------------------------------


class _Body:
    """Context-manager response whose read() returns canned bytes."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def test_b14_retry_honors_429_then_succeeds(monkeypatch):
    import kestrel.discovery.workday as workday_mod

    monkeypatch.setattr(workday_mod, "_RETRY_DELAY", 0)
    calls = {"n": 0}

    def _flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError("http://u", 429, "slow down", None, None)
        return _Body(json.dumps({"total": 0, "jobPostings": []}).encode())

    monkeypatch.setattr(workday_mod, "_urlopen", _flaky)

    req = urllib.request.Request("http://u")
    data = workday_mod._open_with_retry(req, timeout=30)

    assert json.loads(data)["total"] == 0
    assert calls["n"] == 3


def test_b14_permanent_500_exhausts_retries_and_raises(monkeypatch):
    import kestrel.discovery.workday as workday_mod

    monkeypatch.setattr(workday_mod, "_RETRY_DELAY", 0)
    calls = {"n": 0}

    def _broken(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://u", 500, "server error", None, None)

    monkeypatch.setattr(workday_mod, "_urlopen", _broken)

    req = urllib.request.Request("http://u")
    with pytest.raises(urllib.error.HTTPError):
        workday_mod._open_with_retry(req, timeout=30)
    assert calls["n"] == workday_mod._RETRY_ATTEMPTS


def test_b14_non_retryable_404_raises_immediately(monkeypatch):
    import kestrel.discovery.workday as workday_mod

    calls = {"n": 0}

    def _missing(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://u", 404, "nope", None, None)

    monkeypatch.setattr(workday_mod, "_urlopen", _missing)

    with pytest.raises(urllib.error.HTTPError):
        workday_mod._open_with_retry(urllib.request.Request("http://u"), timeout=30)
    assert calls["n"] == 1


def test_b14_mid_pagination_truncation_surfaced_as_warning(
    isolated_db, monkeypatch, caplog
):
    import kestrel.discovery.workday as workday_mod

    def _down(employer, search_text, limit=20, offset=0):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(workday_mod, "workday_search", _down)

    employer = {
        "name": "Acme",
        "base_url": "http://x",
        "tenant": "t",
        "site_id": "s",
    }
    with caplog.at_level(logging.WARNING, logger="kestrel.discovery.workday"):
        jobs = workday_mod.search_employer("acme", employer, "query")

    assert jobs == []
    assert any("PARTIAL" in record.getMessage() for record in caplog.records), (
        "truncation must be surfaced as a warning, not silently swallowed"
    )


# ---------------------------------------------------------------------------
# B15 — get_client() singleton is thread-safe
# ---------------------------------------------------------------------------


def test_b15_concurrent_first_use_constructs_one_client(monkeypatch):
    import kestrel.llm as llm_mod

    created = []

    class _Stub:
        def __init__(self, *args, **kwargs):  # PR #2 passes provider_name= too
            created.append(self)
            # Widen the race window so unguarded construction would overlap
            import time
            time.sleep(0.01)

    monkeypatch.setattr(llm_mod, "LLMClient", _Stub)
    monkeypatch.setattr(llm_mod, "_detect_provider", lambda: ("http://x", "m", None))
    monkeypatch.setattr(llm_mod, "_instance", None)

    n_threads = 8
    gate = threading.Barrier(n_threads)

    def worker():
        gate.wait()
        llm_mod.get_client()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1


# ---------------------------------------------------------------------------
# B16 — SQLite version floor for NULLS LAST
# ---------------------------------------------------------------------------


def test_b16_old_sqlite_version_raises_readable_error():
    from kestrel.database import _check_sqlite_version

    with pytest.raises(RuntimeError, match="SQLite >= 3.30"):
        _check_sqlite_version((3, 29, 0))


def test_b16_minimum_version_passes():
    from kestrel.database import _check_sqlite_version

    _check_sqlite_version((3, 30, 0))  # no raise
    _check_sqlite_version((3, 46, 1))  # no raise


def test_b16_runtime_sqlite_passes():
    from kestrel.database import _check_sqlite_version

    _check_sqlite_version()  # the actual runtime — must not raise


# ---------------------------------------------------------------------------
# ensure_columns migration race (spike-confirmed 40/40 repro)
# ---------------------------------------------------------------------------


def test_ensure_columns_concurrent_migration_no_duplicate_column(
    isolated_db, tmp_path
):
    """Two threads migrating the same old-schema DB must not collide."""
    path = str(tmp_path / "old.db")
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    # Switch to WAL up front: the mode switch itself needs exclusive access,
    # and racing it from both threads raises 'database is locked' before the
    # migration-under-test even starts.
    setup.execute("PRAGMA journal_mode=WAL")
    setup.commit()
    setup.close()

    errors: list[Exception] = []
    gate = threading.Barrier(2)

    def worker():
        try:
            gate.wait()
            conn = sqlite3.connect(path, timeout=30)
            conn.execute("PRAGMA busy_timeout=10000")
            database.ensure_columns(conn)
            conn.close()
        except Exception as exc:  # noqa: BLE001 — collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"migration race raised: {errors}"

    verify = sqlite3.connect(path)
    cols = {row[1] for row in verify.execute("PRAGMA table_info(jobs)").fetchall()}
    verify.close()
    assert "fit_score" in cols and "url" in cols


class _WinningProcessConn:
    """Proxy that replays the cross-process race against every ALTER: a
    concurrent winner commits the same column first, so this connection's
    ALTER raises 'duplicate column name' (P2-3)."""

    def __init__(self, conn, winner):
        self._conn = conn
        self._winner = winner

    def execute(self, sql, *args, **kwargs):
        if sql.startswith("ALTER TABLE jobs ADD COLUMN"):
            col = sql.split("ADD COLUMN ", 1)[1].split()[0]
            self._winner.execute(sql)
            self._winner.commit()
            raise sqlite3.OperationalError(f"duplicate column name: {col}")
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._conn.commit()


def test_ensure_columns_tolerates_cross_process_duplicate_column(
    isolated_db, tmp_path
):
    """P2-3: the portal and CLI processes race their first init_db; a
    threading lock cannot serialize them, so the loser's ALTER hits
    'duplicate column name' after the winner committed. That is the
    winner's success: the migration completes instead of crashing the
    portal startup or a run spawn."""
    path = str(tmp_path / "old.db")
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    setup.execute("PRAGMA journal_mode=WAL")
    setup.commit()
    setup.close()

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    winner = sqlite3.connect(path, timeout=30)
    winner.execute("PRAGMA busy_timeout=10000")

    added = database.ensure_columns(_WinningProcessConn(conn, winner))

    # Every column the loser lost the race for is still reported as added.
    assert "fit_score" in added
    verify = sqlite3.connect(path)
    cols = {row[1] for row in verify.execute("PRAGMA table_info(jobs)").fetchall()}
    verify.close()
    assert "fit_score" in cols and "apply_conflict" in cols

    # And a subsequent pass adds nothing (idempotent, as before).
    assert database.ensure_columns(conn) == []


def test_ensure_columns_still_raises_other_operational_errors(isolated_db, tmp_path):
    """The duplicate-column tolerance must not swallow real failures."""
    path = str(tmp_path / "old.db")
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    setup.commit()
    setup.close()

    class _LockedAlterConn:
        """Passes reads through, fails every ALTER like a busy database."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE jobs ADD COLUMN"):
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, *args, **kwargs)

        def commit(self):
            return self._conn.commit()

    conn = sqlite3.connect(path, timeout=30)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        database.ensure_columns(_LockedAlterConn(conn))


# ---------------------------------------------------------------------------
# F5 — streaming discover must surface sub-scraper failures
# ---------------------------------------------------------------------------


def _discover_stats(**overrides) -> dict:
    stats = {"jobspy": "ok", "workday": "ok", "smartextract": "ok", "composio": "ok"}
    stats.update(overrides)
    return stats


def test_f5_streaming_discover_suberrors_surfaced(monkeypatch):
    """One failed sub-scraper must land in errors (CLI exits nonzero),
    not print ok while every source is down."""
    def _one_failing(**kwargs):
        return _discover_stats(workday="error: no network")

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "discover", _one_failing)

    summary = pipeline._run_streaming(["discover"], min_score=7)

    discover = next(s for s in summary["stages"] if s["stage"] == "discover")
    assert discover["status"] == "partial"
    assert "discover" in summary["errors"]
    assert "workday: error: no network" in summary["errors"]["discover"]


def test_f5_streaming_discover_all_sources_down(monkeypatch):
    """All four scrapers failing is an error, never ok."""
    def _all_failing(**kwargs):
        return _discover_stats(
            jobspy="error: dns", workday="error: timeout",
            smartextract="error: nav", composio="error: 1810",
        )

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "discover", _all_failing)

    summary = pipeline._run_streaming(["discover"], min_score=7)

    discover = next(s for s in summary["stages"] if s["stage"] == "discover")
    assert discover["status"] == "partial"
    assert "discover" in summary["errors"]


def test_f5_streaming_discover_all_ok_reports_ok(monkeypatch):
    """Healthy discovery must keep its ok summary with an empty errors dict."""
    def _all_ok(**kwargs):
        return _discover_stats()

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "discover", _all_ok)

    summary = pipeline._run_streaming(["discover"], min_score=7)

    discover = next(s for s in summary["stages"] if s["stage"] == "discover")
    assert discover["status"] == "ok"
    assert summary["errors"] == {}


def test_f5_sequential_discover_suberror_exits_nonzero(monkeypatch):
    """Sequential mode: sub-scraper failure reaches the errors dict, which
    drives `kestrel run`'s nonzero exit (cli.py exits 1 on errors)."""

    def _one_failing(**kwargs):
        return _discover_stats(workday="error: no network")

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "discover", _one_failing)

    summary = pipeline.run_pipeline(stages=["discover"])

    discover = next(s for s in summary["stages"] if s["stage"] == "discover")
    assert discover["status"] == "partial"
    assert summary["errors"]["discover"].startswith("workday: error")


def test_f5_discover_helpers():
    """Pure helper contract: only 'error'-prefixed strings count."""
    assert pipeline._discover_sub_errors(_discover_stats()) == []
    assert pipeline._discover_stage_status(_discover_stats()) == "ok"
    result = _discover_stats(jobspy="error: dns")
    assert pipeline._discover_sub_errors(result) == ["jobspy: error: dns"]
    assert pipeline._discover_stage_status(result) == "partial"
    # the summary "status" key never counts as a sub-scraper error
    assert pipeline._discover_sub_errors({"status": "partial"}) == []

