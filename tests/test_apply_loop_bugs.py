"""Remediation tests for the apply-loop bugs (B1, B3, B4, B13).

One test per bug per the spec's verification table (blueprint art_BmQxJjjF,
section C). Test names carry the bug id; the CI gate keeps the suite honest —
removing or skipping any of these fails the build.
"""

import signal
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from kestrel import config, database
from kestrel.apply import launcher
from kestrel.scoring import docs


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


# ---------------------------------------------------------------------------
# P2-3 — SIGTERM (cancel_run) releases the in-flight apply lock
# ---------------------------------------------------------------------------


def test_sigterm_handler_releases_the_in_flight_lock(queue):
    """The old handler was `lambda *_: sys.exit(0)` — SystemExit skipped
    worker_loop's cleanup and the job stayed locked for the whole stale
    window (a cancelled run's job card read in_progress for an hour and
    /jobs/apply-now refused with a 409 about a run that no longer
    existed). The handler now releases the registered lock before exit."""
    _seed_job(queue, "https://x.com/jobs/cancelled",
              tailored_resume_path="/tmp/fake/resume.pdf")
    job = launcher.acquire_job(min_score=1)
    assert job is not None

    launcher._register_in_flight(job["url"])
    with pytest.raises(SystemExit):
        launcher._sigterm_handler(signal.SIGTERM, None)

    row = queue.execute(
        "SELECT apply_status, agent_id FROM jobs WHERE url = ?",
        ("https://x.com/jobs/cancelled",)).fetchone()
    assert row["apply_status"] is None  # unlocked — immediately retryable
    assert row["agent_id"] is None
    with launcher._in_flight_guard:
        assert launcher._in_flight_locks == set()


def test_worker_loop_tracks_the_in_flight_lock_during_run_job(queue, monkeypatch):
    """The handler can only release what the loop registers: the URL is
    registered while run_job is in flight and gone after the job."""
    _seed_job(queue, "https://x.com/jobs/1",
              tailored_resume_path="/tmp/fake/resume.pdf")
    snapshot = {}

    def fake_run_job(job, **kwargs):
        with launcher._in_flight_guard:
            snapshot["urls"] = set(launcher._in_flight_locks)
        return "applied", 10

    monkeypatch.setattr(launcher, "run_job", fake_run_job)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)

    applied, failed = launcher.worker_loop(limit=1, min_score=1, dry_run=True)
    assert (applied, failed) == (1, 0)
    assert snapshot["urls"] == {"https://x.com/jobs/1"}
    with launcher._in_flight_guard:
        assert launcher._in_flight_locks == set()


# ---------------------------------------------------------------------------
# P2-2 — the worker re-checks the live daily cap before every job
# ---------------------------------------------------------------------------


def test_daily_cap_reached_reads_live_applications(queue):
    """The check counts rows with applied_at on today's UTC date, read at
    the moment of the check — not a snapshot taken at run start."""
    _seed_job(queue, "https://x.com/jobs/1")
    assert launcher._daily_cap_reached(1) is False

    queue.execute(
        "UPDATE jobs SET applied_at = ? WHERE url = ?",
        (datetime.now(timezone.utc).isoformat(), "https://x.com/jobs/1"))
    queue.commit()
    assert launcher._daily_cap_reached(1) is True


def test_worker_loop_stops_before_acquiring_when_cap_is_reached(queue, monkeypatch):
    """A cap recorded mid-run by any path (a concurrent run, a manual mark)
    stops the worker before it acquires the next job — the snapshot limit
    alone could let two runs apply twice the cap."""
    acquired = {"n": 0}

    def _acquire(**kwargs):
        acquired["n"] += 1
        return None

    monkeypatch.setattr(launcher, "acquire_job", _acquire)
    monkeypatch.setattr(launcher, "_daily_cap_reached", lambda cap: True)
    events: list[str] = []
    monkeypatch.setattr(launcher, "add_event", lambda msg: events.append(msg))

    applied, failed = launcher.worker_loop(limit=1, min_score=1, dry_run=True,
                                           daily_cap=10)

    assert (applied, failed) == (0, 0)
    assert acquired["n"] == 0  # the cap check precedes any acquisition
    assert any("Daily cap reached" in e for e in events)


# ---------------------------------------------------------------------------
# F-3 — Chrome launch failures are environment errors, not job failures
# ---------------------------------------------------------------------------


def test_f3_chrome_launch_failure_refunds_the_attempt(queue, monkeypatch):
    """A Chrome launch failure is an environment problem (fresh machine, no
    profile dir), not a job failure: the acquisition attempt is refunded,
    the job is not counted as failed, and consecutive launch failures stop
    the run — bounded like the pipeline's consecutive-error cap — instead
    of churning the queue."""
    _seed_job(queue, "https://x.com/jobs/1",
              tailored_resume_path="/tmp/fake/resume.pdf")

    launches = {"n": 0}

    def boom(*a, **k):
        launches["n"] += 1
        raise FileNotFoundError("no such directory: ~/.config/google-chrome")

    monkeypatch.setattr(launcher, "launch_chrome", boom)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)

    def run_job_must_not_run(job, **kwargs):
        raise AssertionError("run_job must not run when Chrome never launched")

    monkeypatch.setattr(launcher, "run_job", run_job_must_not_run)

    applied, failed = launcher.worker_loop(limit=1, min_score=1, dry_run=True)
    assert (applied, failed) == (0, 0)  # not a job failure
    # Bounded: the worker retries the launch a fixed number of times and
    # stops loudly instead of churning the queue forever.
    assert launches["n"] == launcher._MAX_LAUNCH_FAILURES

    row = queue.execute(
        "SELECT apply_attempts, apply_status FROM jobs WHERE url = ?",
        ("https://x.com/jobs/1",)).fetchone()
    assert row["apply_attempts"] == 0  # the attempt was refunded
    assert row["apply_status"] is None  # unlocked, fully retryable
    # The job is still in the queue once the environment is fixed.
    assert launcher.acquire_job(min_score=1) is not None


# ---------------------------------------------------------------------------
# Distinct captcha status — terminal by decision (spec art_tBtaZIWB)
# ---------------------------------------------------------------------------


def test_captcha_verdict_persists_distinct_terminal_status(queue, monkeypatch):
    """The worker loop persists the engine's CAPTCHA verdict as
    apply_status='captcha' (not 'failed'), with the attempts ban pinned."""
    _seed_job(queue, "https://x.com/jobs/captcha", fit_score=5)

    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "run_job",
                        lambda job, **kwargs: ("captcha", 42))
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **k: None)

    applied, failed = launcher.worker_loop(limit=1, min_score=1, dry_run=True)
    assert (applied, failed) == (0, 1)

    row = queue.execute(
        "SELECT apply_status, apply_error, apply_attempts FROM jobs "
        "WHERE url = ?", ("https://x.com/jobs/captcha",)).fetchone()
    assert row["apply_status"] == "captcha"
    assert row["apply_error"] == "captcha"
    assert row["apply_attempts"] == 99  # permanent: the belt-and-braces ban


def test_captcha_job_is_never_acquired_by_the_queue(queue):
    """Queue predicate copy 1 (acquire_job): captcha is terminal by decision —
    never requeued, even with attempts far under any cap."""
    _seed_job(queue, "https://x.com/jobs/captcha",
              apply_status="captcha", apply_error="captcha", apply_attempts=99)
    _seed_job(queue, "https://x.com/jobs/normal", fit_score=5)

    assert launcher.acquire_job(min_score=1) is not None  # normal job flows
    row = queue.execute(
        "SELECT apply_status FROM jobs WHERE url = ?",
        ("https://x.com/jobs/captcha",)).fetchone()
    assert row["apply_status"] == "captcha"


def test_captcha_job_is_not_in_the_derived_batch(queue):
    """Queue predicate copy 2 (derive_batch): the plan preview never offers a
    captcha-blocked job."""
    _seed_job(queue, "https://x.com/jobs/captcha",
              apply_status="captcha", apply_error="captcha", apply_attempts=99)
    _seed_job(queue, "https://x.com/jobs/normal", fit_score=5)

    batch = launcher.derive_batch(min_score=1)
    urls = [job["url"] for job in batch]
    assert urls == ["https://x.com/jobs/normal"]


def test_reset_failed_leaves_captcha_rows_terminal(queue):
    """The reset sweep keeps its behavior for failed rows but must not sweep
    captcha rows back to retryable NULL — that would silently re-enqueue
    blocked jobs."""
    _seed_job(queue, "https://x.com/jobs/captcha",
              apply_status="captcha", apply_error="captcha", apply_attempts=99)
    _seed_job(queue, "https://x.com/jobs/failed",
              apply_status="failed", apply_error="engine died", apply_attempts=2)

    reset = launcher.reset_failed()
    assert reset == 1  # only the failed row

    rows = {row["url"]: dict(row) for row in queue.execute(
        "SELECT url, apply_status, apply_error, apply_attempts FROM jobs")}
    assert rows["https://x.com/jobs/captcha"]["apply_status"] == "captcha"
    assert rows["https://x.com/jobs/captcha"]["apply_error"] == "captcha"
    assert rows["https://x.com/jobs/captcha"]["apply_attempts"] == 99
    assert rows["https://x.com/jobs/failed"]["apply_status"] is None
    assert rows["https://x.com/jobs/failed"]["apply_attempts"] == 0

