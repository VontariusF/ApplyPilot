"""Automation backend tests (spec Automation): config, presets, scheduler,
daily cap, retry, and the single-job tailor-now trigger.

Spec acceptance pins: preset starts produce the right commands; the
scheduler fires only when toggled and never double-starts; the cap pauses
auto-apply with a recorded reason; retry reproduces a failed run's
parameters; tailor-now resets + runs one job through the registry.
"""

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from applypilot.database import get_connection, init_db
from applypilot.portal import automation, runner


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
                        status: str = "running") -> None:
    runner.init_registry()
    conn = get_connection()
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
        "min_score": 7, "daily_cap": 10, "keep_going": False}
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
    assert argv[1:5] == ["-m", "applypilot", "rescore", "--all"]


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
    automation.update({"auto_apply_enabled": True,
                       "presets": {"apply_matches": {"daily_cap": 5}}})
    outcome = automation.run_tick()
    assert [s["kind"] for s in outcome["started"]] == ["apply"]
    kind, params = start_spy[0]
    assert params["limit"] == "5"  # full cap available


def test_scheduler_never_double_starts_while_a_run_is_active(portal_env, start_spy):
    insert_registry_row("active-1", "pipeline")
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
    assert response.headers["location"] == "/runs"
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
