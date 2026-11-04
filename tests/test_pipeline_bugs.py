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

import applypilot.database as database
import applypilot.pipeline as pipeline
import applypilot.scoring.scorer as scorer


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
    import applypilot.scoring.scorer as scorer

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
    import applypilot.enrichment.detail as detail

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
# B8 — LLM response parsing guards (Gemini native + OpenAI compat)
# ---------------------------------------------------------------------------


def _gemini_client(data):
    from applypilot.llm import LLMClient

    client = LLMClient("http://test", "gemini-2.0-flash", "key")
    client._client = _FakeHttpxClient(data)
    return client


def test_b8_gemini_empty_candidates_safety_block():
    from applypilot.llm import LLMParseError

    client = _gemini_client({"candidates": []})
    with pytest.raises(LLMParseError, match="no candidates"):
        client._chat_native_gemini(
            [{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=16
        )


def test_b8_gemini_function_call_part():
    from applypilot.llm import LLMParseError

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
    from applypilot.llm import LLMClient, LLMParseError

    resp = _FakeResponse({"choices": [{"message": {"content": None}}]})
    with pytest.raises(LLMParseError, match="null/non-text"):
        LLMClient._handle_compat_response(resp)


def test_b8_compat_normal_content_still_works():
    from applypilot.llm import LLMClient

    resp = _FakeResponse({"choices": [{"message": {"content": "hello"}}]})
    assert LLMClient._handle_compat_response(resp) == "hello"


# ---------------------------------------------------------------------------
# B9 — Qwen preprocessing tolerates None first-message content
# ---------------------------------------------------------------------------


def test_b9_qwen_none_content_no_attribute_error():
    from applypilot.llm import LLMClient

    client = LLMClient("http://test", "Qwen/Qwen2.5-7B", None)
    client._client = _FakeHttpxClient({"choices": [{"message": {"content": "ok"}}]})

    out = client.chat([{"role": "user", "content": None}, {"role": "user", "content": "hi"}])
    assert out == "ok"


def test_b9_qwen_string_content_still_gets_no_think():
    from applypilot.llm import LLMClient

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
    import applypilot.discovery.jobspy as jobspy_mod

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
    from applypilot.discovery.filters import location_ok

    assert location_ok("San Francisco, CA", [], []) is True
    assert location_ok("New York, NY", None or [], []) is True


def test_b11_explicit_accept_still_filters():
    from applypilot.discovery.filters import location_ok

    assert location_ok("New York, NY", ["new york"], []) is True
    assert location_ok("Boston, MA", ["new york"], []) is False


def test_b11_reject_still_applies_with_empty_accept():
    from applypilot.discovery.filters import location_ok

    assert location_ok("Boston, MA", [], ["boston"]) is False
    assert location_ok("Remote", [], ["boston"]) is True


def test_b11_location_filters_handles_absent_and_none():
    from applypilot.discovery.filters import location_filters

    assert location_filters({}) == ([], [])
    assert location_filters({"location_accept": None}) == ([], [])


def test_b11_all_sources_share_one_implementation(jobspy_stub):
    from applypilot.discovery import jobspy as jobspy_mod
    from applypilot.discovery import workday as workday_mod
    from applypilot.discovery.filters import location_ok

    assert jobspy_mod._location_ok is location_ok
    assert workday_mod._location_ok is location_ok


# ---------------------------------------------------------------------------
# B12 — nullable job title must not crash hot paths
# ---------------------------------------------------------------------------


def test_b12_job_title_helper_guards_none():
    from applypilot.apply.launcher import _job_title

    assert _job_title({"title": None}) == "untitled"
    assert _job_title({}) == "untitled"
    assert _job_title({"title": "Senior Engineer"}) == "Senior Engineer"
    assert _job_title({"title": None})[:30] == "untitled"


def test_b12_tailor_null_title_error_path_no_typeerror(isolated_db, monkeypatch, tmp_path):
    import applypilot.scoring.tailor as tailor

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
    import applypilot.scoring.cover_letter as cover_mod

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
    import applypilot.discovery.workday as workday_mod

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
    import applypilot.discovery.workday as workday_mod

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
    import applypilot.discovery.workday as workday_mod

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
    import applypilot.discovery.workday as workday_mod

    def _down(employer, search_text, limit=20, offset=0):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(workday_mod, "workday_search", _down)

    employer = {
        "name": "Acme",
        "base_url": "http://x",
        "tenant": "t",
        "site_id": "s",
    }
    with caplog.at_level(logging.WARNING, logger="applypilot.discovery.workday"):
        jobs = workday_mod.search_employer("acme", employer, "query")

    assert jobs == []
    assert any("PARTIAL" in record.getMessage() for record in caplog.records), (
        "truncation must be surfaced as a warning, not silently swallowed"
    )


# ---------------------------------------------------------------------------
# B15 — get_client() singleton is thread-safe
# ---------------------------------------------------------------------------


def test_b15_concurrent_first_use_constructs_one_client(monkeypatch):
    import applypilot.llm as llm_mod

    created = []

    class _Stub:
        def __init__(self, base_url, model, api_key):
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
    from applypilot.database import _check_sqlite_version

    with pytest.raises(RuntimeError, match="SQLite >= 3.30"):
        _check_sqlite_version((3, 29, 0))


def test_b16_minimum_version_passes():
    from applypilot.database import _check_sqlite_version

    _check_sqlite_version((3, 30, 0))  # no raise
    _check_sqlite_version((3, 46, 1))  # no raise


def test_b16_runtime_sqlite_passes():
    from applypilot.database import _check_sqlite_version

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
