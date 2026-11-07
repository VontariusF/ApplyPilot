"""Reliability fixes 3 and 5 (spec art_ti3XJuNr): the apply no-op guard and
apply_status writer coordination.

Fix 3 — Apply on an in_progress job must fail honestly: the route refuses
with a 409 and a plain-language message, starts no run, and the card renders
a disabled state instead of a verb that cannot work.

Fix 5 — the worker's mark_result and a manual mark must not silently
overwrite each other: the later writer wins only over matching state, and a
mismatch is surfaced (apply_conflict note, conflict badge) instead of
clobbered.
"""

from datetime import datetime, timedelta, timezone

import pytest

from applypilot import config, database
from applypilot.apply import launcher
from applypilot.database import get_connection, init_db

HX = {"HX-Request": "true"}


@pytest.fixture()
def queue(app_dir):
    """Isolated store handle (the reclaim/mark tests seed rows directly)."""
    database.init_db()
    return get_connection()


@pytest.fixture()
def start_spy(portal_client, monkeypatch):
    """Replace runner.start_run (the real spawn) with a recording fake."""
    from applypilot.portal import runner

    calls: list[tuple[str, dict]] = []

    def fake_start(kind, params, **kwargs):
        calls.append((kind, dict(params)))
        return {"id": f"fake-{len(calls)}", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    return calls


def seed_job(**overrides) -> str:
    conn = init_db()
    url = overrides.pop("url", "https://jobs.example/posting-1")
    row = {
        "url": url, "title": "Pwidget Engineer", "site": "ExampleBoard",
        "location": "Remote", "description": "short desc",
        "full_description": "full description text", "fit_score": 9,
        "salary": "", "apply_status": None,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "is_remote": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    conn.commit()
    return url


def job_row(url) -> dict:
    row = get_connection().execute(
        "SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Fix 3 — Apply now refuses in_progress jobs
# ---------------------------------------------------------------------------


def test_apply_now_on_in_progress_job_returns_409(portal_client, seeded_profile,
                                                  start_spy):
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress")
    r = client.post("/jobs/apply-now", data={"url": url}, headers=HX)
    assert r.status_code == 409
    # Plain language: what is wrong and what happens next.
    assert "right now" in r.text
    assert "updates when it finishes" in r.text
    # No run was started — the old behavior spawned a doomed no-op run.
    assert start_spy == []


def test_apply_now_on_in_progress_job_non_htmx_refuses(portal_client,
                                                       seeded_profile,
                                                       start_spy):
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress")
    r = client.post("/jobs/apply-now", data={"url": url}, follow_redirects=False)
    assert r.status_code == 409
    assert "right now" in r.text
    assert start_spy == []


def test_board_card_renders_disabled_apply_for_in_progress_job(
        portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(tailored_resume_path="/x/resume.txt",
             apply_status="in_progress")
    board = client.get("/jobs")
    # The disabled state, not a working Apply verb.
    assert "Application in progress…" in board.text
    assert "A run owns this job" in board.text
    assert "Confirm &amp; apply" not in board.text


# ---------------------------------------------------------------------------
# Fix 5 — writer coordination
# ---------------------------------------------------------------------------


def test_worker_applied_after_manual_applied_supersedes(app_dir):
    """Worker result matches the manual mark: the user's record stands."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="applied",
                   applied_at="2026-01-01T00:00:00+00:00",
                   apply_task_id="manual-applied")
    outcome = launcher.mark_result(url, "applied", duration_ms=1234,
                                   task_id="task-1",
                                   resume_version_id=7)
    assert outcome == "superseded"
    row = job_row(url)
    # The manual record was not clobbered.
    assert row["apply_status"] == "applied"
    assert row["applied_at"] == "2026-01-01T00:00:00+00:00"
    assert row["applied_resume_version_id"] is None  # user's snapshot intact
    # The coordination bookkeeping is cleared for the next run.
    assert row["apply_task_id"] is None
    assert row["agent_id"] is None


def test_worker_failed_after_manual_failed_supersedes(app_dir):
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="failed", apply_error="manual",
                   apply_attempts=99, apply_task_id="manual-failed")
    outcome = launcher.mark_result(url, "failed", error="engine died")
    assert outcome == "superseded"
    row = job_row(url)
    assert row["apply_status"] == "failed"
    assert row["apply_error"] == "manual"  # the user's reason stands
    assert row["apply_attempts"] == 99     # no extra burn from the worker
    assert row["apply_task_id"] is None


def test_worker_applied_after_manual_failed_records_conflict(app_dir):
    """Mismatch: the run says applied, the user said failed — no clobber."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="failed", apply_error="manual",
                   applied_at=None, apply_task_id="manual-failed")
    outcome = launcher.mark_result(url, "applied", duration_ms=500,
                                   task_id="task-2")
    assert outcome == "conflict"
    row = job_row(url)
    # The manual record (including its state) is intact...
    assert row["apply_status"] == "failed"
    assert row["applied_at"] is None
    # ...and the worker's outcome is visible, not swallowed.
    assert row["apply_conflict"] is not None
    assert "applied" in row["apply_conflict"]
    assert "failed" in row["apply_conflict"]


def test_worker_failed_after_manual_applied_records_conflict(app_dir):
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="applied",
                   applied_at="2026-01-01T00:00:00+00:00",
                   apply_task_id="manual-applied")
    outcome = launcher.mark_result(url, "failed", error="captcha wall",
                                   permanent=True)
    assert outcome == "conflict"
    row = job_row(url)
    assert row["apply_status"] == "applied"
    assert row["applied_at"] == "2026-01-01T00:00:00+00:00"
    assert row["apply_attempts"] == 1  # surfaced, not the 99 ban
    assert "failed" in row["apply_conflict"]
    assert "captcha wall" in row["apply_conflict"]


def test_worker_result_without_manual_mark_writes_normally(app_dir):
    """No coordination involved: the plain worker path is unchanged."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress", apply_attempts=0)
    outcome = launcher.mark_result(url, "applied", duration_ms=100,
                                   task_id="task-3", resume_version_id=3)
    assert outcome == "recorded"
    row = job_row(url)
    assert row["apply_status"] == "applied"
    assert row["applied_at"] is not None
    assert row["apply_task_id"] == "task-3"
    assert row["applied_resume_version_id"] == 3

    # And a plain failed result still increments attempts.
    outcome = launcher.mark_result(url, "failed", error="timeout")
    assert outcome == "recorded"
    row = job_row(url)
    assert row["apply_status"] == "failed"
    assert row["apply_attempts"] == 1
    assert row["apply_error"] == "timeout"


# ---------------------------------------------------------------------------
# P2-1 — stale-lock reclaim then a late worker report is a lost race, not a
# human mark: the reclaim marker separates the two
# ---------------------------------------------------------------------------


def test_stale_reclaim_writes_the_marker(queue):
    """B4's reclaim path marks the row, so later writers can tell a lost
    race from a human mark (P2-1). A fresh claim on an unmarked row does
    not stamp the marker."""
    _seed_stale_locked_job(queue, "https://x.com/jobs/stale")

    job = launcher.acquire_job(min_score=1)
    assert job is not None
    assert job_row("https://x.com/jobs/stale")["apply_task_id"] == "reclaimed"

    # A fresh claim on an unmarked row leaves the marker absent.
    seed_job(url="https://x.com/jobs/fresh",
             tailored_resume_path="/tmp/fake/resume.pdf")
    fresh = launcher.acquire_job(min_score=1)
    assert fresh is not None
    assert job_row("https://x.com/jobs/fresh")["apply_task_id"] is None


def _seed_stale_locked_job(conn, url, **extra):
    cols = {
        "url": url, "title": "Engineer", "site": "acme", "fit_score": 9,
        "tailored_resume_path": "/tmp/fake/resume.pdf",
        "apply_status": "in_progress",
        "last_attempted_at": (datetime.now(timezone.utc) - timedelta(
            minutes=2 * config.DEFAULTS["apply_stale_lock_minutes"])).isoformat(),
        "apply_attempts": 1,
    }
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})",
                 tuple(cols.values()))
    conn.commit()


def test_reclaimed_race_late_applied_wins_without_blaming_the_user(queue):
    """The sweep's reproduced P2-1 case: worker A's lock goes stale, worker
    B reclaims and records 'failed', then A finally reports 'applied'. The
    old status-based fallback read 'failed with no marker' as a human mark
    and recorded a user-blaming conflict that discarded the real outcome."""
    _seed_stale_locked_job(queue, "https://x.com/jobs/stale")

    reclaiming = launcher.acquire_job(min_score=1)
    assert reclaiming is not None
    # The reclaimer's genuine failure (worker calls carry no task id).
    outcome_b = launcher.mark_result("https://x.com/jobs/stale", "failed",
                                     error="engine died")
    assert outcome_b == "recorded"

    # The original worker finally reports — a lost race, not a conflict.
    outcome_a = launcher.mark_result("https://x.com/jobs/stale", "applied",
                                     duration_ms=100, task_id="task-a",
                                     resume_version_id=3)
    assert outcome_a == "recorded"
    row = job_row("https://x.com/jobs/stale")
    assert row["apply_status"] == "applied"
    assert row["applied_at"] is not None
    assert row["applied_resume_version_id"] == 3
    # The note is neutral — no "you marked it failed" accusation.
    assert "concurrent run" in (row["apply_conflict"] or "")
    assert "you marked" not in (row["apply_conflict"] or "")


def test_reclaimed_race_late_failed_wins_with_neutral_note(queue):
    """Mirror case: the reclaimer recorded 'applied', the stalled original
    worker finally reports 'failed' — the later report wins and the note
    stays neutral."""
    _seed_stale_locked_job(queue, "https://x.com/jobs/stale")

    assert launcher.acquire_job(min_score=1) is not None
    outcome_b = launcher.mark_result("https://x.com/jobs/stale", "applied",
                                     duration_ms=100)
    assert outcome_b == "recorded"

    outcome_a = launcher.mark_result("https://x.com/jobs/stale", "failed",
                                     error="engine died")
    assert outcome_a == "recorded"
    row = job_row("https://x.com/jobs/stale")
    assert row["apply_status"] == "failed"
    assert row["apply_error"] == "engine died"
    assert "concurrent run" in (row["apply_conflict"] or "")
    assert "you marked" not in (row["apply_conflict"] or "")


def test_unmarked_failed_row_is_still_treated_as_a_manual_mark(queue):
    """A pre-marker row (apply_task_id NULL, no reclaim) keeps the
    status-based fallback: a failed row with no marker and no reclaim
    history is a human mark, and a contradicting worker report conflicts."""
    seed_job(url="https://x.com/jobs/manual",
             apply_status="failed", apply_error="manual",
             apply_task_id=None)

    outcome = launcher.mark_result("https://x.com/jobs/manual", "applied",
                                   duration_ms=10)
    assert outcome == "conflict"
    row = job_row("https://x.com/jobs/manual")
    assert row["apply_status"] == "failed"  # the manual record stands
    assert "you marked it failed" in (row["apply_conflict"] or "")


def test_manual_mark_on_in_progress_job_raises(app_dir):
    """The manual writer refuses a job a run currently owns."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress", agent_id="droid-worker-1")
    with pytest.raises(launcher.ManualMarkConflict):
        launcher.mark_job(url, "applied")
    with pytest.raises(launcher.ManualMarkConflict):
        launcher.mark_job(url, "failed", reason="manual")
    # Nothing was written.
    assert job_row(url)["apply_status"] == "in_progress"


def test_manual_mark_route_returns_409_on_owned_job(portal_client,
                                                    seeded_profile):
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress")
    r = client.post("/jobs/mark", data={"url": url, "status": "applied"},
                    follow_redirects=False)
    assert r.status_code == 409
    assert "right now" in r.text
    assert job_row(url)["apply_status"] == "in_progress"


def test_detail_page_disables_marks_and_shows_conflict(portal_client,
                                                       seeded_profile):
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress",
                   apply_conflict="Run reported 'applied' after you marked "
                                  "it failed — kept your mark: engine died")
    detail = client.get("/jobs/detail", params={"url": url})
    assert detail.status_code == 200
    # The conflict surfaces in the UI...
    assert "kept your mark" in detail.text
    # ...and the mark buttons are disabled while the run owns the job.
    assert "disabled" in detail.text


def test_supersede_paths_clear_conflict_note(app_dir):
    """A later normal worker result clears a stale conflict note."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress",
                   apply_conflict="stale note from an earlier clash")
    launcher.mark_result(url, "failed", error="timeout")
    assert job_row(url)["apply_conflict"] is None


def test_reset_failed_clears_the_manual_marker(app_dir):
    """Reset clears the marker so a fresh run starts with clean bookkeeping."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="failed", apply_task_id="manual-failed")
    launcher.reset_failed()
    assert job_row(url)["apply_task_id"] is None


# ---------------------------------------------------------------------------
# Reliability sweep P3 fixes
# ---------------------------------------------------------------------------

def test_count_apply_eligible_agrees_with_acquire_on_manual_ats(queue):
    """Reliability sweep P3-1 (REPRODUCED): the scheduler's guard used to
    count manual-ATS jobs the worker refuses — every scheduled run then
    marked one 'manual' and exited 'Queue empty', burning a tracked run
    per manual-ATS job. The count mirrors the worker's predicate."""
    from applypilot.apply import launcher

    manual_url = "https://ibegin.tcsapps.com/job/123"
    seed_job(url=manual_url, application_url=manual_url,
             tailored_resume_path="/tmp/resume.pdf")
    assert config.is_manual_ats(manual_url)
    assert database.count_apply_eligible() == 0
    # The worker agrees: nothing to acquire.
    assert launcher.acquire_job() is None
    # A normal job alongside still counts.
    seed_job(url="https://jobs.example/normal",
             tailored_resume_path="/tmp/resume.pdf")
    assert database.count_apply_eligible() == 1


def test_busy_timeout_is_the_single_documented_budget(queue):
    """Portal sweep P3-2: one busy budget (10s), set immediately after
    connect — the connect(timeout=30) default used to be silently lowered
    by a later pragma, and WAL conversion ran before any budget was set."""
    conn = get_connection()
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 10000


# ---------------------------------------------------------------------------
# Distinct captcha status — writer coordination (spec art_tBtaZIWB)
# ---------------------------------------------------------------------------


def test_captcha_result_writes_terminal_status_with_pinned_attempts(app_dir):
    """The plain worker path persists the captcha verdict as its own status:
    apply_status='captcha', apply_error='captcha', attempts pinned at 99."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="in_progress", apply_attempts=0)
    outcome = launcher.mark_result(url, "captcha", "captcha", permanent=True,
                                   duration_ms=100)
    assert outcome == "recorded"
    row = job_row(url)
    assert row["apply_status"] == "captcha"
    assert row["apply_error"] == "captcha"
    assert row["apply_attempts"] == 99


def test_worker_captcha_after_manual_failed_supersedes(app_dir):
    """captcha is a 'not applied' outcome: it matches a manual failed mark
    (both agree the application did not happen), so it supersedes — the
    user's record stands and the marker is cleared."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="failed", apply_error="manual",
                   apply_attempts=99, apply_task_id="manual-failed")
    outcome = launcher.mark_result(url, "captcha", "captcha", permanent=True)
    assert outcome == "superseded"
    row = job_row(url)
    assert row["apply_status"] == "failed"      # the user's mark stands
    assert row["apply_error"] == "manual"
    assert row["apply_attempts"] == 99
    assert row["apply_task_id"] is None         # the marker is cleared


def test_worker_captcha_after_manual_applied_records_conflict(app_dir):
    """Mismatch: the user says applied, the run says captcha-blocked — no
    clobber; the conflict note carries the captcha outcome."""
    database.init_db()
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   apply_status="applied",
                   applied_at="2026-01-01T00:00:00+00:00",
                   apply_task_id="manual-applied")
    outcome = launcher.mark_result(url, "captcha", "captcha", permanent=True)
    assert outcome == "conflict"
    row = job_row(url)
    assert row["apply_status"] == "applied"
    assert row["applied_at"] == "2026-01-01T00:00:00+00:00"
    assert "captcha" in row["apply_conflict"]


def test_count_apply_eligible_excludes_captcha(queue):
    """Queue predicate copy 3 (count_apply_eligible): captcha rows are
    terminal by decision and are never counted as eligible."""
    from applypilot.apply import launcher as launcher_mod

    captcha_url = "https://jobs.example/blocked"
    seed_job(url=captcha_url, application_url=captcha_url,
             tailored_resume_path="/tmp/resume.pdf",
             apply_status="captcha", apply_error="captcha", apply_attempts=99)
    assert database.count_apply_eligible() == 0
    # The worker agrees: nothing to acquire.
    assert launcher_mod.acquire_job() is None
    # A normal job alongside still counts.
    seed_job(url="https://jobs.example/normal",
             tailored_resume_path="/tmp/resume.pdf")
    assert database.count_apply_eligible() == 1


def test_get_stats_reports_captcha_separately_from_apply_errors(queue):
    """status --json contract: captcha blocks are their own count and are not
    folded into apply_errors (a captcha block is not an application failure)."""
    database.init_db()
    seed_job(url="https://jobs.example/captcha",
             tailored_resume_path="/tmp/resume.pdf",
             apply_status="captcha", apply_error="captcha", apply_attempts=99)
    seed_job(url="https://jobs.example/failed",
             tailored_resume_path="/tmp/resume.pdf",
             apply_status="failed", apply_error="engine died", apply_attempts=1)

    stats = database.get_stats()
    assert stats["captcha"] == 1
    assert stats["apply_errors"] == 1  # the failed row only

