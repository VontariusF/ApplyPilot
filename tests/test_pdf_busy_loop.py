"""Regression tests for the pdf stage busy-loop (audit P0, spec fix 4).

Before the fix, the streaming pdf stage's pending count read
``tailored_resume_path LIKE '%.txt'`` — DB state batch_convert never
wrote — and the streaming loop only slept on its idle branch. Any
tailored resume whose PDF already existed (or whose conversion could
not settle) kept the count above zero forever, and the stage spun at
100% CPU between no-op passes.

These tests pin the contract on both halves of the fix:

- batch_convert settles the jobs table's PDF bookkeeping, so the
  pending count converges once conversions settle;
- the streaming loop backs off between busy passes and terminates
  with a bounded loop.
"""

import threading
from pathlib import Path

import pytest

from applypilot import database, pipeline
from applypilot.scoring import pdf as pdf_mod


def _seed_tailored_job(url: str, txt_path: str) -> None:
    conn = database.init_db()
    conn.execute(
        "INSERT INTO jobs (url, tailored_resume_path) VALUES (?, ?)",
        (url, txt_path),
    )
    conn.commit()


def _make_tailored_pair(tmp_path, name: str = "resume_acme"):
    """Create a tailored .txt whose .pdf already exists (stale accounting)."""
    tailored = tmp_path / "tailored"
    tailored.mkdir(exist_ok=True)
    txt = tailored / f"{name}.txt"
    txt.write_text("Jane Doe\nTITLE\nEngineer\nSUMMARY\ntext", encoding="utf-8")
    txt.with_suffix(".pdf").write_bytes(b"%PDF-1.4 already converted")
    return txt


def test_stale_txt_accounting_settles_when_pdf_exists(isolated_db, tmp_path, monkeypatch):
    """A .txt row whose PDF already exists must leave the pending count.

    This is the exact busy-loop condition: the DB says pending, but
    batch_convert has nothing to convert — before the fix the pending
    count stayed 1 forever and the stage spun.
    """
    txt = _make_tailored_pair(tmp_path)
    _seed_tailored_job("https://example.com/job-1", str(txt))
    monkeypatch.setattr(pdf_mod, "TAILORED_DIR", tmp_path / "tailored")

    # Reproduce the pre-fix pending state
    assert pipeline._count_pending("pdf") == 1

    converted = pdf_mod.batch_convert()

    assert converted == 0, "the PDF already exists — nothing to convert"
    conn = database.get_connection()
    row = conn.execute(
        "SELECT pdf_generated_at FROM jobs WHERE url = ?",
        ("https://example.com/job-1",),
    ).fetchone()
    assert row["pdf_generated_at"] is not None, (
        "batch_convert must settle the stale .txt row it can convert nothing for"
    )
    assert pipeline._count_pending("pdf") == 0, (
        "pending count must converge to zero once conversions settle"
    )


def test_pdf_stage_terminates_with_stale_txt_accounting(
    isolated_db, tmp_path, monkeypatch
):
    """The streaming pdf stage must terminate (bounded loop), not spin forever.

    Runs the real stage thread against the real runner + DB with a
    tailored resume whose PDF already exists. Pre-fix this never
    returned; the join timeout is the bounded-loop assertion.
    """
    txt = _make_tailored_pair(tmp_path)
    _seed_tailored_job("https://example.com/job-1", str(txt))
    monkeypatch.setattr(pdf_mod, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(pipeline, "_STREAM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pipeline, "_STREAM_BUSY_INTERVAL", 0.01)

    tracker = pipeline._StageTracker()
    tracker.mark_done("cover")  # upstream done: no new work can arrive

    finished = threading.Event()

    def _run():
        pipeline._run_stage_streaming("pdf", tracker, threading.Event())
        finished.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert finished.wait(timeout=30), (
        "pdf stage must terminate once conversions settle — "
        "pre-fix it busy-looped forever on stale .txt accounting"
    )

    result = tracker.get_results()["pdf"]
    assert result["status"] == "ok"
    assert pipeline._count_pending("pdf") == 0


def test_busy_path_backs_off_between_passes(monkeypatch):
    """The busy branch must sleep between passes, not only the idle one."""
    tracker = pipeline._StageTracker()
    tracker.mark_done("enrich")  # skip the first-pass upstream wait
    calls = {"n": 0}

    def _runner(**kwargs):
        calls["n"] += 1

    class _StopOnFirstWait:
        """Fake stop event: records waits, then requests stop."""

        def __init__(self):
            self.timeouts = []

        def is_set(self):
            return False

        def wait(self, timeout=None):
            self.timeouts.append(timeout)
            return True  # stop requested

    stop = _StopOnFirstWait()
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", _runner)
    monkeypatch.setattr(pipeline, "_count_pending", lambda stage, min_score=7: 1)

    pipeline._run_stage_streaming("score", tracker, stop)

    assert calls["n"] == 1
    assert stop.timeouts == [pipeline._STREAM_BUSY_INTERVAL], (
        "the busy branch must back off between passes instead of spinning"
    )


def test_missing_txt_burns_attempts_until_not_pending(isolated_db, tmp_path, monkeypatch):
    """A row whose source .txt vanished must age out under the attempt cap.

    Retry semantics stay bounded (the same < 5 cap the tailor and cover
    stages use), so a dead row cannot keep the stage pending forever.
    """
    _seed_tailored_job("https://example.com/job-1", "/nonexistent/resume.txt")
    monkeypatch.setattr(pdf_mod, "TAILORED_DIR", tmp_path / "tailored")
    conn = database.get_connection()

    for _ in range(5):
        pdf_mod.batch_convert()

    row = conn.execute(
        "SELECT pdf_attempts, pdf_generated_at FROM jobs WHERE url = ?",
        ("https://example.com/job-1",),
    ).fetchone()
    assert row["pdf_attempts"] == 5
    assert row["pdf_generated_at"] is None
    assert pipeline._count_pending("pdf") == 0


def test_pdf_columns_migrate_existing_databases(isolated_db):
    """ensure_columns must add the pdf columns to pre-existing DBs."""
    conn = database.init_db()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "pdf_generated_at" in columns
    assert "pdf_attempts" in columns


def test_reset_job_tailoring_clears_pdf_bookkeeping(isolated_db, tmp_path):
    """Re-tailoring a job must re-enqueue it for PDF conversion.

    A regenerated resume is a different document: the stale
    pdf_generated_at/pdf_attempts of the previous one must not keep the
    new resume out of the pdf stage's pending count — pre-fix, a failed
    render of the new resume was never retried.
    """
    txt = _make_tailored_pair(tmp_path)
    _seed_tailored_job("https://example.com/job-1", str(txt))
    conn = database.get_connection()
    conn.execute(
        "UPDATE jobs SET pdf_generated_at = ?, pdf_attempts = 2 "
        "WHERE url = ?",
        ("2026-01-01T00:00:00Z", "https://example.com/job-1"),
    )
    conn.commit()

    assert database.reset_job_tailoring(conn, "https://example.com/job-1")

    row = conn.execute(
        "SELECT tailored_resume_path, pdf_generated_at, pdf_attempts "
        "FROM jobs WHERE url = ?",
        ("https://example.com/job-1",),
    ).fetchone()
    assert row["tailored_resume_path"] is None
    assert row["pdf_generated_at"] is None, (
        "reset must clear the old PDF timestamp so the new resume re-pends"
    )
    assert row["pdf_attempts"] == 0


def test_reconciliation_respects_remaining_limit(isolated_db, tmp_path, monkeypatch):
    """batch_convert's total renders per call must stay within `limit`.

    The reconciliation pass converts DB-backed sources whose PDFs are
    missing, so unbounded it could render far more than the API's limit
    reported. Budget the reconciliation by the remaining quota; excess
    rows stay pending for the next pass.
    """
    tailored = tmp_path / "tailored"
    tailored.mkdir()
    rendered: list[str] = []

    def _fake_convert(txt_path):
        txt = Path(txt_path)
        txt.with_suffix(".pdf").write_bytes(b"%PDF-1.4 fake")
        rendered.append(txt.name)

    for i in range(4):
        txt = tailored / f"resume_{i}.txt"
        txt.write_text(f"Jane Doe\nTITLE\nEngineer\nSUMMARY\ntext {i}", encoding="utf-8")
        _seed_tailored_job(f"https://example.com/job-{i}", str(txt))
    monkeypatch.setattr(pdf_mod, "TAILORED_DIR", tailored)
    monkeypatch.setattr(pdf_mod, "convert_to_pdf", _fake_convert)

    converted = pdf_mod.batch_convert(limit=2)

    assert converted == 2
    assert rendered == ["resume_0.txt", "resume_1.txt"], (
        "reconciliation must not render beyond the remaining quota"
    )
    assert pipeline._count_pending("pdf") == 2

    # Next pass converts the rest — the work converges across passes.
    assert pdf_mod.batch_convert(limit=2) == 2
    assert pipeline._count_pending("pdf") == 0


if __name__ == "__main__":
    pytest.main([__file__])
