"""Remediation tests for the apply-loop bugs (B1, B3, B4, B13).

One test per bug per the spec's verification table (blueprint art_BmQxJjjF,
section C). Test names carry the bug id; the CI gate keeps the suite honest —
removing or skipping any of these fails the build.
"""

import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from applypilot import config, database
from applypilot.apply import launcher
from applypilot.scoring import docs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def queue(app_dir):
    database.init_db()
    return database.get_connection()


def _seed_job(conn, url, title="Engineer", **extra):
    cols = {
        "url": url,
        "title": title,
        "site": "acme",
        "fit_score": 9,
        "tailored_resume_path": f"/tmp/fake/{abs(hash(url))}.pdf",
    }
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


# ---------------------------------------------------------------------------
# B1 — --limit shorter than --workers no longer spawns forever-polling workers
# ---------------------------------------------------------------------------


def test_b1_split_limits_yields_no_infinite_zero_slice_workers():
    limits = launcher._split_worker_limits(2, 4)
    assert limits == [1, 1, 0, 0]
    assert sum(limits) == 2

    # Exact division and limit < workers both behave
    assert launcher._split_worker_limits(4, 2) == [2, 2]
    assert launcher._split_worker_limits(1, 3) == [1, 0, 0]


def test_b1_zero_limit_non_continuous_worker_exits_cleanly(monkeypatch):
    """A zero-slice worker must not poll the queue (the old limit==0 inference)."""
    def _boom(*args, **kwargs):
        raise AssertionError("acquire_job must not be reached by a zero-slice worker")

    monkeypatch.setattr(launcher, "acquire_job", _boom)
    started = time.monotonic()
    applied, failed = launcher.worker_loop(worker_id=0, limit=0, continuous=False)
    assert (applied, failed) == (0, 0)
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
# B3 — URL-derived hash ends same-site/title filename collisions
# ---------------------------------------------------------------------------


def test_b3_url_hash_makes_colliding_jobs_distinct():
    a = {"title": "Engineer", "site": "acme", "url": "https://acme.com/jobs/1"}
    b = {"title": "Engineer", "site": "acme", "url": "https://acme.com/jobs/2"}
    assert docs.output_prefix(a) != docs.output_prefix(b)
    # Same job → stable prefix
    assert docs.output_prefix(a) == docs.output_prefix(dict(a))
    # Readable prefix is preserved
    for prefix in (docs.output_prefix(a), docs.output_prefix(b)):
        assert prefix.startswith("acme_Engineer_")
        assert len(prefix.rsplit("_", 1)[1]) == 8


def test_b3_output_prefix_guards_nullable_title():
    job = {"title": None, "site": None, "url": ""}
    prefix = docs.output_prefix(job)
    assert prefix.startswith("unknown_untitled_")


# ---------------------------------------------------------------------------
# B4 — stale in_progress locks are reclaimable; reset_failed clears them
# ---------------------------------------------------------------------------


def test_b4_stale_in_progress_lock_is_reclaimed(queue):
    _seed_job(queue, "https://x.com/jobs/stale",
              apply_status="in_progress",
              last_attempted_at=_iso(minutes_ago=2 * config.DEFAULTS["apply_stale_lock_minutes"]),
              apply_attempts=1)
    _seed_job(queue, "https://x.com/jobs/fresh",
              fit_score=5,
              apply_status="in_progress",
              last_attempted_at=_iso(minutes_ago=1))

    job = launcher.acquire_job(min_score=1)
    assert job is not None
    assert job["url"] == "https://x.com/jobs/stale"


def test_b4_fresh_in_progress_lock_is_not_reclaimed(queue):
    _seed_job(queue, "https://x.com/jobs/fresh",
              apply_status="in_progress",
              last_attempted_at=_iso(minutes_ago=1))

    assert launcher.acquire_job(min_score=1) is None


def test_b4_reset_failed_clears_stale_lock_only(queue):
    _seed_job(queue, "https://x.com/jobs/stale",
              apply_status="in_progress",
              last_attempted_at=_iso(minutes_ago=2 * config.DEFAULTS["apply_stale_lock_minutes"]))
    _seed_job(queue, "https://x.com/jobs/fresh",
              apply_status="in_progress",
              last_attempted_at=_iso(minutes_ago=1))

    reset = launcher.reset_failed()
    assert reset == 1

    rows = {
        row["url"]: row["apply_status"]
        for row in queue.execute("SELECT url, apply_status FROM jobs")
    }
    assert rows["https://x.com/jobs/stale"] is None
    assert rows["https://x.com/jobs/fresh"] == "in_progress"


# ---------------------------------------------------------------------------
# B13 — apply_timeout covers the streaming read (watchdog kills a hung agent)
# ---------------------------------------------------------------------------


class _FakeHangingProc:
    """Agent process that never writes output until it is killed."""

    def __init__(self, killed: threading.Event):
        self.killed = killed
        self.pid = 4242
        self.returncode = None
        self.stdin = types.SimpleNamespace(write=lambda data: None, close=lambda: None)
        self._stdout = None

    @property
    def stdout(self):
        if self._stdout is None:
            self._stdout = self._hang()
        return self._stdout

    def _hang(self):
        # Blocks like a real pipe read; EOF only once the watchdog killed us.
        while not self.killed.is_set():
            time.sleep(0.02)
        return
        yield  # pragma: no cover — makes this a generator

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.returncode = -9
        return 0


def test_b13_streaming_read_killed_at_apply_timeout(app_dir, queue, tmp_path, monkeypatch):
    killed = threading.Event()
    fake = _FakeHangingProc(killed)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(launcher, "_kill_process_tree", lambda pid: killed.set())
    monkeypatch.setitem(config.DEFAULTS, "apply_timeout", 1)
    # main() calls ensure_dirs() before run_job; replicate it here.
    config.ensure_dirs()
    config.PROFILE_PATH.write_text(
        '{"personal": {"email": "a@b.c", "full_name": "Test User", "phone": "555"},'
        ' "work_authorization": {},'
        ' "compensation": {"salary_expectation": "100000"}}',
        encoding="utf-8",
    )
    resume = tmp_path / "tailored.txt"
    resume.write_text("resume body", encoding="utf-8")
    (tmp_path / "tailored.pdf").write_bytes(b"%PDF-fake")

    job = {
        "url": "https://x.com/jobs/1",
        "title": "Engineer",
        "site": "acme",
        "tailored_resume_path": str(resume),
        "fit_score": 9,
    }

    started = time.monotonic()
    status, duration_ms = launcher.run_job(job, port=9222, worker_id=0)
    elapsed = time.monotonic() - started

    assert status == "failed:timeout"
    assert killed.is_set()
    assert elapsed < 10  # watchdog fired near the 1s budget, not the old 300s
    assert duration_ms >= 1000
