"""Automation backend tests (spec Automation): config, presets, scheduler,
daily cap, retry, and the single-job tailor-now trigger.

Spec acceptance pins: preset starts produce the right commands; the
scheduler fires only when toggled and never double-starts; the cap pauses
auto-apply with a recorded reason; retry reproduces a failed run's
parameters; tailor-now resets + runs one job through the registry.
"""

import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from kestrel.database import get_connection, init_db
from kestrel.portal import automation, runner

# The autouse conftest fixture replaces automation._provider_probe with a
# constant for every test; this is the real gate, captured at collection
# time, for the one test that exercises the cache itself.
_REAL_PROBE_GATE = automation._provider_probe


def seed_job(**overrides) -> str:
    conn = init_db()
    url = overrides.pop("url", "https://jobs.example/posting-1")
    row = {
        "url": url, "title": "Pwidget Engineer", "site": "ExampleBoard",
        "location": "Remote", "description": "short desc",
        "full_description": "full description text", "fit_score": 9,
        "salary": "", "apply_status": None,
        "discovered_at": "2026-11-01T12:00:00+00:00",
        "is_remote": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    conn.commit()
    return url


def seed_eligible_job(**overrides) -> str:
    """A job the apply worker would actually pick up (tailored, unapplied)."""
    return seed_job(tailored_resume_path="/tmp/tailored-resume.txt", **overrides)


def seed_applied(n: int) -> None:
    """n applications recorded right now (today's UTC calendar day)."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    for i in range(n):
        seed_job(url=f"https://jobs.example/applied-{i}",
                 apply_status="applied", applied_at=now)
    conn.commit()


def insert_registry_row(run_id: str, kind: str,
                        params: dict | None = None,
                        status: str = "running",
                        live: bool = False) -> None:
    runner.init_registry()
    conn = get_connection()
    if live:
        # A row that must look like a genuinely active run to the orphan
        # sweep: identity columns from the current process, which is very
        # much alive (P3-2 moved the sweep into run_tick).
        pid = os.getpid()
        conn.execute(
            "INSERT INTO runs (id, kind, params, status, started_at, pid, pgid, "
            "pid_start_time, pid_cmdline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, json.dumps(params or {}), status,
             datetime.now(timezone.utc).isoformat(), pid, pid,
             runner._proc_start_time(pid),
             json.dumps(runner._proc_cmdline(pid))),
        )
    else:
        conn.execute(
            "INSERT INTO runs (id, kind, params, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, kind, json.dumps(params or {}), status,
             datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()


@pytest.fixture()
def start_spy(monkeypatch):
    """Replace runner.start_run (the real spawn) with a recording fake."""
    calls: list[tuple[str, dict]] = []

    def fake_start(kind, params, **kwargs):
        calls.append((kind, dict(params)))
        return {"id": f"fake-{len(calls)}", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    return calls


# ---------------------------------------------------------------------------
# Config: automation.json (0600)
# ---------------------------------------------------------------------------

def test_config_defaults_before_first_save(portal_env):
    cfg = automation.load()
    assert cfg["auto_apply_enabled"] is False
    assert cfg["schedule_enabled"] is False
    assert cfg["presets"]["find_jobs"] == {"results_level": "standard"}
    assert cfg["presets"]["apply_matches"] == {
        "min_score": 7, "daily_cap": None, "keep_going": False}
    assert cfg["presets"]["refresh_scores"] == {}
    assert cfg["labels"]["pipeline"] == "Finding jobs"


def test_config_saved_0600_and_round_trips(portal_env):
    automation.update({"auto_apply_enabled": True,
                       "presets": {"find_jobs": {"results_level": "high"}}})
    path = automation.config_path()
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cfg = automation.load()
    assert cfg["auto_apply_enabled"] is True
    assert cfg["presets"]["find_jobs"]["results_level"] == "high"


def test_config_rejects_values_the_ui_never_offers(portal_env):
    with pytest.raises(runner.ParamError, match="min_score"):
        automation.update({"presets": {"apply_matches": {"min_score": 4}}})
    with pytest.raises(runner.ParamError, match="daily_cap"):
        automation.update({"presets": {"apply_matches": {"daily_cap": 50}}})
    with pytest.raises(runner.ParamError, match="results_level"):
        automation.update({"presets": {"find_jobs": {"results_level": "max"}}})
    with pytest.raises(runner.ParamError, match="boolean"):
        automation.update({"schedule_enabled": "yes"})


def test_config_corrupt_file_falls_back_to_defaults(portal_env):
    automation.save(automation.load())
    automation.config_path().write_text("{not json", encoding="utf-8")
    cfg = automation.load()
    assert cfg["auto_apply_enabled"] is False  # defaults, not a crash


def test_run_kind_labels_are_plain_language(portal_env):
    assert automation.run_kind_label("pipeline") == "Finding jobs"
    assert automation.run_kind_label("apply") == "Applying"
    assert automation.run_kind_label("rescore") == "Refreshing scores"
    assert automation.run_kind_label("tailor") == "Tailoring job"
    assert automation.run_kind_label("mystery") == "mystery"


# ---------------------------------------------------------------------------
# Presets produce the right commands
# ---------------------------------------------------------------------------

def test_find_jobs_preset_command(portal_env):
    for level, per_site in (("low", 25), ("standard", 50), ("high", 100)):
        automation.update({"presets": {"find_jobs": {"results_level": level}}})
        params = automation.preset_params(name="find_jobs")
        argv = runner.build_pipeline_command(params)
        assert "--results-per-site" in argv
        assert str(per_site) in argv
        assert argv[4] == "all"


def test_apply_matches_preset_command(portal_env):
    automation.update({"presets": {"apply_matches": {
        "min_score": 6, "daily_cap": 25, "keep_going": False}}})
    argv = runner.build_apply_command(automation.preset_params(name="apply_matches"))
    assert "--min-score" in argv and "6" in argv
    assert "--continuous" not in argv

    automation.update({"presets": {"apply_matches": {"keep_going": True}}})
    argv = runner.build_apply_command(automation.preset_params(name="apply_matches"))
    assert "--continuous" in argv


def test_refresh_scores_preset_command(portal_env):
    argv = runner.build_rescore_command(automation.preset_params(name="refresh_scores"))
    assert argv[1:5] == ["-m", "kestrel", "rescore", "--all"]


def test_start_preset_starts_tracked_runs(portal_env, start_spy):
    automation.start_preset("find_jobs")
    assert start_spy[0] == ("pipeline", {"stages": "all", "results_per_site": "50"})
    automation.start_preset("refresh_scores")
    assert start_spy[1] == ("rescore", {})


def test_start_preset_apply_carries_remaining_cap(portal_env, start_spy):
    seed_applied(7)
    automation.update({"presets": {"apply_matches": {"daily_cap": 10}}})
    automation.start_preset("apply_matches")
    kind, params = start_spy[0]
    assert kind == "apply"
    assert params["limit"] == "3"  # cap 10 - 7 used today


def test_start_preset_apply_refuses_when_cap_spent(portal_env, start_spy):
    seed_applied(10)
    automation.update({"presets": {"apply_matches": {"daily_cap": 10}}})
    with pytest.raises(runner.ParamError, match="Daily cap reached"):
        automation.start_preset("apply_matches")
    assert start_spy == []


# ---------------------------------------------------------------------------
# Daily cap counting
# ---------------------------------------------------------------------------

def test_applications_today_counts_only_today(portal_env):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    seed_applied(2)
    seed_job(url="https://jobs.example/old", apply_status="applied",
             applied_at=yesterday)
    assert automation.applications_today() == 2


def test_cap_status_reports_usage_and_pause(portal_env):
    seed_applied(3)
    cfg = automation.update({"presets": {"apply_matches": {"daily_cap": 5}}})
    status = automation.cap_status(cfg)
    assert status == {"cap": 5, "used_today": 3, "remaining": 2,
                      "paused": False, "pause_reason": None}


# ---------------------------------------------------------------------------
# Scheduler: fires only when toggled, never double-starts
# ---------------------------------------------------------------------------

def test_scheduler_does_nothing_when_toggles_off(portal_env, start_spy):
    assert automation.run_tick() == {"started": [], "skipped": [], "paused": None}
    assert start_spy == []


def test_scheduler_fires_daily_schedule_once_per_day(portal_env, start_spy):
    automation.update({"schedule_enabled": True})
    first = automation.run_tick()
    assert [s["kind"] for s in first["started"]] == ["pipeline"]
    second = automation.run_tick()
    assert second["started"] == []  # last_schedule_run == today
    assert len(start_spy) == 1


def test_scheduler_fires_auto_apply(portal_env, start_spy):
    seed_eligible_job()
    automation.update({"auto_apply_enabled": True,
                       "presets": {"apply_matches": {"daily_cap": 5}}})
    outcome = automation.run_tick()
    assert [s["kind"] for s in outcome["started"]] == ["apply"]
    kind, params = start_spy[0]
    assert params["limit"] == "5"  # full cap available


def test_scheduler_skips_apply_when_queue_empty(portal_env, start_spy):
    """Zero eligible jobs: log the nothing-to-apply outcome instead of
    launching a run that would exit 'Queue empty' — the ~60s tick must
    not flood Runs history with empty apply runs."""
    automation.update({"auto_apply_enabled": True,
                       "presets": {"apply_matches": {"daily_cap": 5}}})
    outcome = automation.run_tick()
    assert outcome["started"] == []
    assert outcome["skipped"] == ["nothing to apply to"]
    assert start_spy == []


def test_scheduler_empty_queue_guard_matches_worker_eligibility(
        portal_env, start_spy):
    """The guard mirrors the worker's acquire predicate: a job that fails
    it (below preset min_score) is not 'work available', but a failed-
    status job the worker would retry still counts."""
    automation.update({"auto_apply_enabled": True,
                       "presets": {"apply_matches": {"daily_cap": 5,
                                                    "min_score": 8}}})
    # Below the preset's min_score: not eligible — no run.
    seed_eligible_job(url="https://jobs.example/low", fit_score=7)
    outcome = automation.run_tick()
    assert outcome["started"] == []
    assert outcome["skipped"] == ["nothing to apply to"]
    # A failed apply the worker would retry IS eligible — run starts.
    seed_eligible_job(url="https://jobs.example/retry", fit_score=9,
                      apply_status="failed")
    outcome = automation.run_tick()
    assert [s["kind"] for s in outcome["started"]] == ["apply"]


def test_scheduler_never_double_starts_while_a_run_is_active(portal_env, start_spy):
    # A live row (P3-2: run_tick sweeps dead ones now, so the busy guard
    # needs a row whose process exists) blocks a second start.
    insert_registry_row("active-1", "pipeline", live=True)
    automation.update({"schedule_enabled": True, "auto_apply_enabled": True})
    outcome = automation.run_tick()
    assert outcome["started"] == []
    assert outcome["skipped"] == ["a run is already active"]
    assert start_spy == []


def test_scheduler_stale_pause_blocks_apply_not_schedule(portal_env, start_spy):
    """A stale pause from a previous day clears on the next tick, and the
    (separate) schedule toggle still fires its Find jobs run."""
    yesterday = (_today_shift(-1))
    cfg = automation.update({"schedule_enabled": True,
                             "presets": {"apply_matches": {"daily_cap": 5}}})
    cfg["state"]["auto_apply_paused"] = {"on": yesterday, "reason": "cap"}
    automation.save(cfg)
    outcome = automation.run_tick()
    assert [s["kind"] for s in outcome["started"]] == ["pipeline"]


def _today_shift(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Cap pauses auto-apply with a recorded reason
# ---------------------------------------------------------------------------

def test_cap_pauses_auto_apply_with_reason(portal_env, start_spy):
    seed_applied(5)
    automation.update({"auto_apply_enabled": True,
                       "presets": {"apply_matches": {"daily_cap": 5}}})
    outcome = automation.run_tick()
    assert outcome["started"] == []
    assert "Daily cap reached" in outcome["paused"]
    status = automation.cap_status()
    assert status["paused"] is True
    assert "Daily cap reached" in status["pause_reason"]
    # The pause is persisted — the UI reads it from a fresh load.
    assert automation.load()["state"]["auto_apply_paused"]["reason"]


def test_cap_pause_ends_the_next_day(portal_env, start_spy):
    seed_eligible_job()
    cfg = automation.update({"auto_apply_enabled": True,
                             "presets": {"apply_matches": {"daily_cap": 5}}})
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    cfg["state"]["auto_apply_paused"] = {"on": yesterday, "reason": "cap"}
    automation.save(cfg)
    outcome = automation.run_tick()
    assert [s["kind"] for s in outcome["started"]] == ["apply"]
    assert automation.load()["state"]["auto_apply_paused"] is None


# ---------------------------------------------------------------------------
# Retry: identical parameters from the registry row
# ---------------------------------------------------------------------------

def test_retry_reruns_failed_run_with_identical_params(portal_env, monkeypatch):
    insert_registry_row("dead-1", "apply",
                        {"min_score": "6", "limit": "3", "model": "sonnet"},
                        status="failed")
    captured: dict = {}

    def fake_start(kind, params, **kwargs):
        captured["kind"], captured["params"] = kind, dict(params)
        return {"id": "retry-1", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    row = runner.retry_run("dead-1")
    assert captured == {"kind": "apply",
                        "params": {"min_score": "6", "limit": "3", "model": "sonnet"}}
    assert row["id"] == "retry-1"


def test_retry_rejects_non_failed_and_missing_runs(portal_env):
    insert_registry_row("live-1", "pipeline")
    with pytest.raises(runner.ParamError, match="only failed"):
        runner.retry_run("live-1")
    with pytest.raises(runner.ParamError, match="not found"):
        runner.retry_run("nope-1")


def test_retry_route_spawns_a_new_registry_row(portal_env, portal_client, monkeypatch):
    client, _ = portal_client
    insert_registry_row("dead-2", "pipeline", {"min_score": "8"}, status="failed")
    captured: dict = {}

    def fake_start(kind, params, **kwargs):
        captured["kind"], captured["params"] = kind, dict(params)
        return {"id": "retry-2", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    response = client.post("/runs/retry/dead-2", follow_redirects=False)
    assert response.status_code == 303
    # P3-4: the outcome is surfaced, not a silent redirect.
    assert response.headers["location"] == "/runs?notice=Retry+started."
    assert captured == {"kind": "pipeline", "params": {"min_score": "8"}}


def test_retry_route_surfaces_errors_as_redirect(portal_env, portal_client):
    client, _ = portal_client
    insert_registry_row("live-2", "pipeline")
    response = client.post("/runs/retry/live-2", follow_redirects=False)
    assert response.status_code == 303
    assert "/runs?error=" in response.headers["location"]


# ---------------------------------------------------------------------------
# Single-job tailor-now trigger
# ---------------------------------------------------------------------------

def test_tailor_now_resets_and_starts_one_job_run(portal_env, start_spy):
    url = seed_job(tailored_resume_path="/x/resume.txt", tailor_attempts=2,
                   cover_letter_path="/x/cover.txt")
    row = automation.start_single_job_tailor(url, include_cover=True)
    # enqueue: tailoring state cleared for the tailor stage to pick up
    job = get_connection().execute(
        "SELECT tailored_resume_path, tailor_attempts, cover_letter_path "
        "FROM jobs WHERE url = ?", (url,)).fetchone()
    assert job["tailored_resume_path"] is None
    assert job["tailor_attempts"] == 0
    assert job["cover_letter_path"] is None
    # immediate one-job run, tracked in the registry
    assert start_spy == [("tailor", {"url": url, "min_score": "0"})]
    assert row["kind"] == "tailor"


def test_tailor_now_rejects_unknown_job(portal_env, start_spy):
    with pytest.raises(runner.ParamError, match="job not found"):
        automation.start_single_job_tailor("https://jobs.example/ghost")
    assert start_spy == []


def test_tailor_now_route(portal_env, portal_client, start_spy):
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/x/resume.txt", tailor_attempts=2)
    response = client.post("/jobs/tailor-now", data={"url": url},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "jobs/detail" in response.headers["location"]
    assert start_spy[0] == ("tailor", {"url": url, "min_score": "0"})


def test_tailor_now_route_rejects_missing_url(portal_env, portal_client):
    client, _ = portal_client
    response = client.post("/jobs/tailor-now", data={})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Single-job apply: target URL reaches the command
# ---------------------------------------------------------------------------

def test_one_job_apply_run_targets_the_url(portal_env):
    argv = runner.build_apply_command(
        {"url": "https://jobs.example/posting-1", "min_score": "7"})
    assert argv[-2:] == ["--url", "https://jobs.example/posting-1"]


def test_one_job_apply_run_rejects_bad_urls(portal_env):
    with pytest.raises(runner.ParamError, match=r"http\(s\) URL"):
        runner.build_apply_command({"url": "javascript:alert(1)"})


# ---------------------------------------------------------------------------
# P1-1 — the scheduler gates scheduled starts on a (cached) provider probe
# ---------------------------------------------------------------------------


def test_scheduler_skips_when_provider_unreachable(portal_env, start_spy,
                                                   monkeypatch):
    """A scheduled start against a dead provider used to burn every queued
    job's attempt budget through silent per-job errors. The tick now skips
    and records the plain-language reason for the Runs page."""
    automation.update({"schedule_enabled": True})
    detail = "the provider rejected the test request (HTTP 401) — the API key was refused."
    monkeypatch.setattr(automation, "_provider_probe",
                        lambda: ("unreachable", detail))

    outcome = automation.run_tick()

    assert outcome["started"] == []
    assert outcome["skipped"], "the skip must be visible in the outcome"
    assert "Provider unreachable" in outcome["skipped"][0]
    assert detail in outcome["skipped"][0]
    assert start_spy == []  # nothing was spawned
    assert "Provider unreachable" in automation.load()["state"]["scheduler_skip"]


def test_scheduler_clears_the_pause_when_provider_recovers(portal_env,
                                                           start_spy,
                                                           monkeypatch):
    """The next healthy tick clears the recorded skip reason and proceeds."""
    automation.update({"schedule_enabled": True})
    monkeypatch.setattr(automation, "_provider_probe",
                        lambda: ("unreachable", "down"))
    assert automation.run_tick()["started"] == []

    monkeypatch.setattr(automation, "_provider_probe",
                        lambda: ("reachable", ""))
    outcome = automation.run_tick()

    assert outcome["started"], "the scheduled run fires again"
    assert automation.load()["state"]["scheduler_skip"] is None


def test_probe_cache_reuses_the_real_verdict(portal_env, monkeypatch):
    """The real _provider_probe caches llm.probe_provider's verdict for the
    TTL: two calls, one underlying probe — run_tick fires every ~30s and
    must not absorb the probe's bounds once per tick."""
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return ("reachable", "The provider answered a test request.")

    monkeypatch.setattr("kestrel.llm.probe_provider", _probe)
    monkeypatch.setattr(automation, "_provider_probe", _REAL_PROBE_GATE)
    automation.reset_probe_cache()
    try:
        first = automation._provider_probe()
        second = automation._provider_probe()
        assert first == second == ("reachable",
                                   "The provider answered a test request.")
        assert calls["n"] == 1
    finally:
        automation.reset_probe_cache()


# ---------------------------------------------------------------------------
# P2-2 — manual preset starts go through the guarded path
# ---------------------------------------------------------------------------


def test_start_preset_refuses_while_a_run_is_active(portal_env, start_spy):
    """A second click used to spawn a concurrent run whose limit was
    computed from the same cap snapshot — together the runs could apply up
    to twice the cap. The guard refuses in plain language instead."""
    insert_registry_row("r-active", "pipeline", status="running")

    with pytest.raises(runner.ParamError, match="already active"):
        automation.start_preset("find_jobs")
    assert start_spy == []  # no spawn was even attempted


def test_start_preset_apply_refuses_while_a_run_is_active(portal_env,
                                                          start_spy):
    # The apply path reads the live cap (count_applications_on) before the
    # guard; production reaches start_preset through the app factory, which
    # has already initialized the store.
    init_db()
    insert_registry_row("r-active", "apply", status="running")

    with pytest.raises(runner.ParamError, match="already active"):
        automation.start_preset("apply_matches")
    assert start_spy == []


def test_start_preset_apply_carries_the_daily_cap(portal_env, start_spy):
    """A requested cap rides into the spawned run: the worker re-checks
    the live count before every job, so the cap holds against
    applications recorded mid-run by any path."""
    init_db()
    automation.update({"presets": {"apply_matches": {"daily_cap": 10}}})
    automation.start_preset("apply_matches")

    kind, params = start_spy[0]
    assert kind == "apply"
    assert params["daily_cap"] == "10"  # the cap the user picked
    assert int(params["limit"]) <= 10


def test_start_preset_without_a_cap_passes_none(portal_env, start_spy):
    """Spec art_NcNShMYR: an empty/absent preset cap means NO cap — the
    spawned run carries no daily_cap (the launcher treats None as
    uncapped) and is bounded by the eligible queue, not a number."""
    init_db()
    seed_eligible_job()
    seed_eligible_job(url="https://jobs.example/posting-2")
    automation.start_preset("apply_matches")

    kind, params = start_spy[0]
    assert kind == "apply"
    assert "daily_cap" not in params
    assert params["limit"] == "2"  # the eligible queue is the bound


def test_start_preset_honors_a_preset_of_25(portal_env, start_spy):
    init_db()
    automation.update({"presets": {"apply_matches": {"daily_cap": 25}}})
    automation.start_preset("apply_matches")

    kind, params = start_spy[0]
    assert params["daily_cap"] == "25"
    assert int(params["limit"]) <= 25


# ---------------------------------------------------------------------------
# P2-4 — tailor-now refuses a job an apply run currently owns
# ---------------------------------------------------------------------------


def test_tailor_now_refuses_while_apply_in_progress(portal_env, start_spy):
    """Resetting tailoring mid-run would unlink the very PDF that run is
    about to upload. Same refusal as /jobs/apply-now."""
    url = seed_job(apply_status="in_progress",
                   tailored_resume_path="/x/resume.txt")

    with pytest.raises(runner.ParamError,
                       match="applying to this job right now"):
        automation.start_single_job_tailor(url)

    assert start_spy == []  # no run started
    # The documents were not reset.
    conn = get_connection()
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?",
                       (url,)).fetchone()
    assert row["tailored_resume_path"] == "/x/resume.txt"


def test_tailor_now_route_surfaces_the_refusal(portal_env, portal_client,
                                               start_spy):
    """The route renders the plain-language message as a toast (the same
    path a ParamError from the job lookup already takes)."""
    client, _ = portal_client
    url = seed_job(apply_status="in_progress",
                   tailored_resume_path="/x/resume.txt")

    response = client.post("/jobs/tailor-now", data={"url": url},
                           headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "applying to this job right now" in response.text
    assert start_spy == []


def test_run_tick_sweeps_dead_registry_row(portal_env, start_spy):
    """Reliability sweep P3-2: a leaked 'running' row (monitor thread died,
    child reaped unobserved) used to stall every future tick with 'a run is
    already active' until a portal restart. run_tick runs the cheap orphan
    sweep first, the dead row is reaped, and the scheduler proceeds."""
    insert_registry_row("leaked-1", "pipeline")  # no live process behind it
    automation.update({"schedule_enabled": True})
    outcome = automation.run_tick()
    # The reap itself is noted in the tick outcome; the important part is
    # that the leaked row no longer reads as "a run is already active".
    assert "a run is already active" not in outcome["skipped"]
    assert [s["kind"] for s in outcome["started"]] == ["pipeline"]
    row = runner.get_run("leaked-1")
    assert row["status"] == "failed"  # reaped


def test_cancel_route_surfaces_outcome(portal_env, portal_client):
    """Portal sweep P3-4: cancel tells the user what happened instead of a
    silent redirect — 'cancelled' vs 'nothing was running'."""
    client, _ = portal_client
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        emit_jsonl=False,
    )
    response = client.post(f"/runs/cancel/{row['id']}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/runs?notice=Run+cancelled."
    # A terminal run: the no-op is surfaced as an error, not silence.
    response = client.post(f"/runs/cancel/{row['id']}", follow_redirects=False)
    assert response.status_code == 303
    assert "That+run+was+not+active" in response.headers["location"]
