"""Guardrail tests for `kestrel apply` Gates 2-3 (spec art_A71wyYS4).

Gate 2 — plan-then-confirm: `--plan` prints the batch as ONE pure-JSON
payload and commits nothing; `--i-confirm <plan-hash>` on a second
invocation the same UTC day runs the planned batch through the launcher's
own acquire/lock paths, and the token is consumed on use — never
replayable, and an edited or drifted job set refuses with a structured
error envelope.

Gate 3 — the cap defaults to 5/day for externally driven batches (the
smallest automation preset, automation.DAILY_CAP_CHOICES[0]) and only an
explicit --daily-cap lifts it; the count is the launcher's live-from-DB
guard, shared with portal and manual starts (PR #50 semantics) — these
tests exercise the real count, never a snapshot.
"""

import json
import stat

import pytest
from typer.testing import CliRunner

from kestrel import config, database
from kestrel.apply import launcher, planning
from kestrel.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
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
        "tailored_resume_path": f"/tmp/fake/{abs(hash(url))}.pdf",
    }
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


def _plan(*args):
    """Invoke `kestrel apply --plan ...` — the read-only preview."""
    return runner.invoke(app, ["apply", "--plan", *args])


def _confirm(plan_hash, *args):
    """Invoke `kestrel apply --i-confirm <hash> ...`."""
    return runner.invoke(app, ["apply", "--i-confirm", plan_hash, *args])


def _job_state(conn):
    """Every apply-relevant column, for the commits-nothing assertion."""
    rows = conn.execute("""
        SELECT url, apply_status, applied_at, apply_attempts, agent_id,
               apply_task_id, apply_error
        FROM jobs ORDER BY url
    """).fetchall()
    return [tuple(row) for row in rows]


def _error_of(result) -> dict:
    assert result.exit_code == 1
    return json.loads(result.stdout)["error"]


# ---------------------------------------------------------------------------
# Gate 2 — --plan prints the batch and commits NOTHING
# ---------------------------------------------------------------------------


def test_plan_prints_batch_and_commits_nothing(queue):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=8)
    _seed_job(queue, "https://x.com/jobs/2", fit_score=9)
    before = _job_state(queue)

    result = _plan("--limit", "2")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)  # one pure-JSON document, nothing else
    assert payload["commits_nothing"] is True
    assert payload["cap"] == planning.AGENT_DEFAULT_DAILY_CAP
    assert payload["cap_source"] == "default"
    assert payload["applied_today"] == 0
    assert payload["headroom"] == payload["cap"] - payload["applied_today"]
    # Worker order (fit_score DESC, url), with per-job estimates attached.
    assert [job["url"] for job in payload["jobs"]] == [
        "https://x.com/jobs/2", "https://x.com/jobs/1"]
    assert all(job["estimate_seconds"] == config.DEFAULTS["apply_timeout"]
               for job in payload["jobs"])
    assert len(payload["plan_hash"]) == 64
    assert payload["plan_hash"] in payload["next"]
    # Acceptance 1: the plan invocation changed no row in any way.
    assert _job_state(queue) == before


def test_plan_stages_the_token_owner_only(queue):
    _seed_job(queue, "https://x.com/jobs/1")

    result = _plan()

    assert result.exit_code == 0
    staged = json.loads(planning.plan_path().read_text(encoding="utf-8"))
    payload = json.loads(result.stdout)
    assert staged["plan_hash"] == payload["plan_hash"]
    assert staged["urls"] == [job["url"] for job in payload["jobs"]]
    mode = stat.S_IMODE(planning.plan_path().stat().st_mode)
    assert mode == 0o600  # app state, like automation.json


def test_plan_refuses_an_empty_queue(queue):
    error = _error_of(_plan())
    assert error["code"] == "plan_empty"


def test_plan_rejects_incompatible_flags(queue):
    _seed_job(queue, "https://x.com/jobs/1")
    for extra in (["--continuous"], ["--url", "https://x.com/jobs/1"],
                  ["--gen"], ["--mark-applied", "https://x.com/jobs/1"]):
        error = _error_of(_plan(*extra))
        assert error["code"] == "invalid_usage", extra


def test_plan_hash_tracks_the_batch(queue):
    _seed_job(queue, "https://x.com/jobs/1")
    first = json.loads(_plan().stdout)
    second = json.loads(_plan().stdout)
    assert first["plan_hash"] == second["plan_hash"]  # same batch, same token

    _seed_job(queue, "https://x.com/jobs/0", fit_score=10)
    third = json.loads(_plan().stdout)
    assert third["plan_hash"] != first["plan_hash"]  # new batch, new token


def test_plan_explicit_daily_cap_lifts_the_default(queue):
    _seed_job(queue, "https://x.com/jobs/1")
    payload = json.loads(_plan("--daily-cap", "25").stdout)
    assert payload["cap"] == 25
    assert payload["cap_source"] == "explicit"


# ---------------------------------------------------------------------------
# Gate 2 — --i-confirm verification and the one-shot token
# ---------------------------------------------------------------------------


def test_confirm_without_a_staged_plan_refuses(queue):
    error = _error_of(_confirm("0" * 64))
    assert error["code"] == "plan_required"


def test_confirm_runs_the_planned_batch_and_consumes_the_token(
        queue, seeded_profile, tier_ok, monkeypatch):
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9)
    _seed_job(queue, "https://x.com/jobs/2", fit_score=8)

    planned = _plan("--limit", "2")
    assert planned.exit_code == 0
    payload = json.loads(planned.stdout)

    calls: list[dict] = []

    def fake_main(**kwargs):
        calls.append(kwargs)
        return 0, 0

    monkeypatch.setattr(launcher, "main", fake_main)

    confirmed = _confirm(payload["plan_hash"])
    assert confirmed.exit_code == 0
    assert len(calls) == 1
    call = calls[0]
    # The run is the planned batch: pinned URLs, the plan's size, and the
    # staged cap — Gate 3's 5/day default when no --daily-cap was passed.
    assert call["limit"] == 2
    assert call["daily_cap"] == planning.AGENT_DEFAULT_DAILY_CAP
    assert call["allowed_urls"] == {"https://x.com/jobs/1", "https://x.com/jobs/2"}
    assert call["continuous"] is False
    # Per-batch token: consumed on use, never replayable.
    assert not planning.plan_path().exists()


def test_confirm_replay_refuses_after_the_token_is_consumed(
        queue, seeded_profile, tier_ok, monkeypatch):
    _seed_job(queue, "https://x.com/jobs/1")
    monkeypatch.setattr(launcher, "main", lambda **kwargs: (0, 0))

    payload = json.loads(_plan().stdout)
    first = _confirm(payload["plan_hash"])
    assert first.exit_code == 0

    error = _error_of(_confirm(payload["plan_hash"]))
    assert error["code"] == "plan_required"


def test_confirm_refuses_a_plan_from_another_utc_day(queue, monkeypatch):
    _seed_job(queue, "https://x.com/jobs/1")
    payload = json.loads(_plan().stdout)

    monkeypatch.setattr(planning, "utc_today", lambda: "2000-01-01")
    error = _error_of(_confirm(payload["plan_hash"]))
    assert error["code"] == "plan_stale"
    assert error["details"]["staged_day"] == payload["day"]


def test_confirm_refuses_an_edited_plan(queue):
    _seed_job(queue, "https://x.com/jobs/1")
    _seed_job(queue, "https://x.com/jobs/2")
    payload = json.loads(_plan("--limit", "2").stdout)

    staged = json.loads(planning.plan_path().read_text(encoding="utf-8"))
    staged["urls"] = staged["urls"][:1]  # someone trimmed the batch on disk
    config.write_secret_text(planning.plan_path(), json.dumps(staged))

    error = _error_of(_confirm(payload["plan_hash"]))
    assert error["code"] == "plan_edited"


def test_confirm_refuses_a_wrong_hash(queue):
    _seed_job(queue, "https://x.com/jobs/1")
    _plan()
    error = _error_of(_confirm("f" * 64))
    assert error["code"] == "plan_hash_mismatch"


def test_confirm_refuses_a_drifted_queue(queue, tier_ok):
    """Acceptance 2: the eligible set moving between plan and confirm —
    here one planned job was applied by another path — refuses."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9)
    _seed_job(queue, "https://x.com/jobs/2", fit_score=8)
    payload = json.loads(_plan("--limit", "2").stdout)

    # Reality moved: the manual path recorded an application.
    launcher.mark_job("https://x.com/jobs/2", "applied")

    error = _error_of(_confirm(payload["plan_hash"]))
    assert error["code"] == "plan_drift"
    # The plan's batch, in worker order (fit_score DESC, url): jobs/1
    # (fit 9) first here.
    assert error["details"]["planned"] == [
        "https://x.com/jobs/1", "https://x.com/jobs/2"]
    assert error["details"]["current"] == ["https://x.com/jobs/1"]


def test_confirm_refuses_when_flags_select_a_different_set(queue, tier_ok):
    """Repeating the plan with different selection flags is a different
    job set — the drift check refuses it."""
    _seed_job(queue, "https://x.com/jobs/1", fit_score=9)
    _seed_job(queue, "https://x.com/jobs/2", fit_score=8)
    payload = json.loads(_plan("--limit", "2").stdout)

    error = _error_of(_confirm(payload["plan_hash"], "--min-score", "9"))
    assert error["code"] == "plan_drift"


def test_confirm_refuses_a_cap_disagreement(queue, tier_ok):
    _seed_job(queue, "https://x.com/jobs/1")
    payload = json.loads(_plan().stdout)  # staged cap: the 5/day default
    error = _error_of(_confirm(payload["plan_hash"], "--daily-cap", "25"))
    assert error["code"] == "plan_cap_mismatch"
    assert error["details"]["staged_cap"] == planning.AGENT_DEFAULT_DAILY_CAP


def test_confirm_passes_an_explicit_cap_through(queue, seeded_profile, tier_ok,
                                                monkeypatch):
    _seed_job(queue, "https://x.com/jobs/1")
    payload = json.loads(_plan("--daily-cap", "25").stdout)

    calls: list[dict] = []
    monkeypatch.setattr(launcher, "main", lambda **kwargs: calls.append(kwargs))

    confirmed = _confirm(payload["plan_hash"])
    assert confirmed.exit_code == 0
    assert calls[0]["daily_cap"] == 25


# ---------------------------------------------------------------------------
# Gate 3 — the cap stops the batch at 5 by default
# ---------------------------------------------------------------------------


def test_cap_stops_the_batch_at_five_by_default(queue, monkeypatch):
    """Acceptance 3: with no --daily-cap, a confirmed batch's run stops at
    the 5/day default — the launcher's live guard over the real count."""
    urls = [f"https://x.com/jobs/{i}" for i in range(8)]
    for url in urls:
        _seed_job(queue, url, fit_score=5)
    for url in urls[:5]:
        launcher.mark_job(url, "applied")  # any path's marks count

    assert launcher._daily_cap_reached(5) is True
    assert launcher._daily_cap_reached(6) is False

    def _no_acquire(**kwargs):
        raise AssertionError("the 5/day default must stop the batch before acquisition")

    monkeypatch.setattr(launcher, "acquire_job", _no_acquire)
    events: list[str] = []
    monkeypatch.setattr(launcher, "add_event", lambda msg: events.append(msg))

    applied, failed = launcher.worker_loop(
        limit=3, min_score=1, dry_run=True,
        daily_cap=planning.AGENT_DEFAULT_DAILY_CAP)

    assert (applied, failed) == (0, 0)
    assert any("Daily cap reached" in event for event in events)


# ---------------------------------------------------------------------------
# Gate 4 — concurrent batches share the live-from-DB guard
# ---------------------------------------------------------------------------


def test_second_concurrent_batch_hits_the_shared_guard(queue, monkeypatch):
    """Acceptance 4: a protocol batch and a manual/portal path share ONE
    cap count (PR #50 semantics) — the second batch stops the moment the
    shared count reaches the cap, before acquiring anything."""
    planned = [f"https://a.com/jobs/{i}" for i in range(3)]
    other = [f"https://b.com/jobs/{i}" for i in range(3)]
    for url in planned + other:
        _seed_job(queue, url, fit_score=5)

    # The concurrent path records through the same applied_at column the
    # launcher's guard reads: three manual marks, then two of the protocol
    # batch's own jobs landing.
    for url in other:
        launcher.mark_job(url, "applied")
    for url in planned[:2]:
        launcher.mark_job(url, "applied")

    def _no_acquire(**kwargs):
        raise AssertionError("the shared guard must stop the second batch")

    monkeypatch.setattr(launcher, "acquire_job", _no_acquire)
    events: list[str] = []
    monkeypatch.setattr(launcher, "add_event", lambda msg: events.append(msg))

    applied, failed = launcher.worker_loop(
        limit=3, min_score=1, dry_run=True, daily_cap=5,
        allowed_urls=set(planned))

    assert (applied, failed) == (0, 0)
    assert any("Daily cap reached" in event for event in events)


# ---------------------------------------------------------------------------
# Gate 2 — the pin narrows the queue through the launcher's own lock path
# ---------------------------------------------------------------------------


def test_acquire_pin_narrows_the_queue_to_planned_urls(queue):
    _seed_job(queue, "https://x.com/jobs/stranger", fit_score=10)
    _seed_job(queue, "https://x.com/jobs/planned", fit_score=5)

    job = launcher.acquire_job(min_score=1,
                               allowed_urls={"https://x.com/jobs/planned"})

    assert job["url"] == "https://x.com/jobs/planned"
    row = queue.execute(
        "SELECT apply_status FROM jobs WHERE url = ?",
        ("https://x.com/jobs/planned",)).fetchone()
    assert row["apply_status"] == "in_progress"  # the same lock path


def test_worker_loop_runs_only_planned_urls(queue, monkeypatch):
    planned = [f"https://x.com/jobs/{i}" for i in range(1, 3)]
    for url in planned:
        _seed_job(queue, url, fit_score=5)
    _seed_job(queue, "https://x.com/jobs/stranger", fit_score=10)

    ran: list[str] = []

    def fake_run_job(job, **kwargs):
        ran.append(job["url"])
        return "applied", 1

    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    applied, failed = launcher.worker_loop(
        limit=2, min_score=1, dry_run=True,
        allowed_urls=set(planned))

    assert (applied, failed) == (2, 0)
    assert ran == planned  # worker order; the higher-scored stranger stays out
    row = queue.execute(
        "SELECT apply_status FROM jobs WHERE url = 'https://x.com/jobs/stranger'"
    ).fetchone()
    assert row["apply_status"] is None


def test_main_refuses_a_pinned_continuous_run():
    with pytest.raises(ValueError):
        launcher.main(limit=1, continuous=True, allowed_urls={"https://x.com/jobs/1"})
