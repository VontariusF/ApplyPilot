"""Run registry tests: spawn, monitor, cancel (process-group kill), orphans.

Spec D acceptance: run start creates a registry row and spawns a subprocess
via a fake entry point; cancel records a process-group kill; invalid form
values are rejected with readable errors.
"""

import json
import os
import subprocess
import sys
import time

import pytest

from applypilot.database import get_connection
from applypilot.portal import runner


def wait_for_status(run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = runner.get_run(run_id)
        if row and row["status"] != "running":
            return row
        time.sleep(0.05)
    row = runner.get_run(run_id)
    assert row is not None and row["status"] != "running", f"run {run_id} never finished"
    return row


def test_pipeline_command_shape(portal_env, monkeypatch):
    argv = runner.build_pipeline_command({
        "stages": "score,tailor", "min_score": "8", "workers": "2",
        "validation": "strict", "stream": "1", "dry_run": "1",
    })
    assert argv[1:4] == ["-m", "applypilot", "run"]
    assert argv[4:6] == ["score", "tailor"]
    assert "--min-score" in argv and "8" in argv
    assert "--stream" in argv and "--dry-run" in argv
    assert "--validation" in argv and "strict" in argv


def test_apply_command_shape(portal_env):
    argv = runner.build_apply_command({
        "limit": "5", "workers": "2", "min_score": "6",
        "model": "sonnet", "dry_run": "1", "headless": "1",
    })
    assert argv[1:4] == ["-m", "applypilot", "apply"]
    assert "--limit" in argv and "5" in argv
    assert "--model" in argv and "sonnet" in argv
    assert "--dry-run" in argv and "--headless" in argv
    assert "--continuous" not in argv


def test_apply_command_carries_the_daily_cap(portal_env):
    """P2-2: the worker re-checks the live application count before every
    submission, so the daily cap survives applications recorded mid-run by
    any other path. The flag rides along only when a cap is passed."""
    argv = runner.build_apply_command({
        "limit": "5", "workers": "2", "min_score": "6", "daily_cap": "10",
    })
    assert "--daily-cap" in argv and "10" in argv

    plain = runner.build_apply_command({"limit": "5", "workers": "2"})
    assert "--daily-cap" not in plain


def test_apply_command_model_default_reads_setting(portal_env, monkeypatch):
    """P1-2: a run that names no model gets the Settings page's saved
    AUTOAPPLY_MODEL (pushed to os.environ on save and copied into this
    process's env), not a hardcoded id."""
    monkeypatch.setenv("AUTOAPPLY_MODEL", "qwen2.5:7b")
    argv = runner.build_apply_command({})
    assert argv[argv.index("--model") + 1] == "qwen2.5:7b"


def test_apply_command_model_default_unset_falls_back(portal_env, monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_MODEL", raising=False)
    argv = runner.build_apply_command({})
    assert argv[argv.index("--model") + 1] == "haiku"


def test_apply_command_model_default_empty_falls_back(portal_env, monkeypatch):
    """A cleared Settings field must not put an empty --model on the argv."""
    monkeypatch.setenv("AUTOAPPLY_MODEL", "")
    argv = runner.build_apply_command({})
    assert argv[argv.index("--model") + 1] == "haiku"


def test_apply_command_named_model_beats_setting(portal_env, monkeypatch):
    monkeypatch.setenv("AUTOAPPLY_MODEL", "qwen2.5:7b")
    argv = runner.build_apply_command({"model": "sonnet"})
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_invalid_params_rejected(portal_env):
    with pytest.raises(runner.ParamError, match="unknown stage"):
        runner.build_pipeline_command({"stages": "bogus"})
    with pytest.raises(runner.ParamError, match="between 1 and 8"):
        runner.build_pipeline_command({"workers": "999"})
    with pytest.raises(runner.ParamError, match="unsupported characters"):
        runner.build_apply_command({"model": "haiku; rm -rf /"})
    with pytest.raises(runner.ParamError, match="must be an integer"):
        runner.build_apply_command({"limit": "abc"})


# --- Automation (spec Automation): target-URL apply and one-job tailor ------

def test_apply_command_accepts_target_url(portal_env):
    argv = runner.build_apply_command({"url": "https://jobs.example/1?ref=x"})
    assert argv[-2:] == ["--url", "https://jobs.example/1?ref=x"]


def test_apply_command_rejects_bad_target_url(portal_env):
    with pytest.raises(runner.ParamError, match=r"http\(s\) URL"):
        runner.build_apply_command({"url": "javascript:alert(1)"})


def test_pipeline_command_results_per_site(portal_env):
    argv = runner.build_pipeline_command({"results_per_site": "25"})
    assert "--results-per-site" in argv and "25" in argv


def test_tailor_command_shape(portal_env):
    argv = runner.build_tailor_command({"url": "https://jobs.example/1"})
    assert argv[1:4] == ["-m", "applypilot", "run"]
    assert argv[4:7] == ["tailor", "cover", "pdf"]
    assert "--url" in argv and "https://jobs.example/1" in argv
    assert "--min-score" in argv and "0" in argv


def test_retry_run_reruns_failed_params(portal_env, monkeypatch):
    runner.init_registry()
    conn = get_connection()
    now = "2026-11-04T00:00:00+00:00"
    conn.execute(
        "INSERT INTO runs (id, kind, params, status, started_at) "
        "VALUES ('dead-1', 'apply', ?, 'failed', ?)",
        (json.dumps({"min_score": "6", "limit": "3"}), now))
    conn.commit()
    captured: dict = {}

    def fake_start(kind, params, **kwargs):
        captured["kind"], captured["params"] = kind, dict(params)
        return {"id": "retry-1", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    row = runner.retry_run("dead-1")
    assert captured == {"kind": "apply",
                        "params": {"min_score": "6", "limit": "3"}}
    assert row["id"] == "retry-1"


def test_retry_run_rejects_non_failed_runs(portal_env):
    runner.init_registry()
    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (id, kind, params, status, started_at) "
        "VALUES ('live-1', 'pipeline', '{}', 'running', '2026-11-04T00:00:00+00:00')")
    conn.commit()
    with pytest.raises(runner.ParamError, match="only failed"):
        runner.retry_run("live-1")
    with pytest.raises(runner.ParamError, match="not found"):
        runner.retry_run("nope-1")


def test_start_run_records_row_and_spawns_subprocess(portal_env):
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        emit_jsonl=False,
    )
    assert row["status"] == "running"
    assert row["pid"] == row["pgid"]  # own process group (session leader)
    assert os.kill(row["pid"], 0) is None  # alive
    assert (portal_env / "runs" / f"{row['id']}.log").exists()
    runner.cancel_run(row["id"])


def test_monitor_records_exit_code(portal_env):
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c", "print('bye')"],
        emit_jsonl=False,
    )
    final = wait_for_status(row["id"])
    assert final["status"] == "completed"
    assert final["exit_code"] == 0
    assert final["ended_at"] is not None


def test_monitor_records_failure_exit_code(portal_env):
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c", "raise SystemExit(3)"],
        emit_jsonl=False,
    )
    final = wait_for_status(row["id"])
    assert final["status"] == "failed"
    assert final["exit_code"] == 3


def test_cancel_kills_process_group(portal_env):
    # Child spawns a grandchild; the process-group kill must take both.
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c",
                 "import subprocess, time, sys;"
                 "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                 "print(p.pid, flush=True);"
                 "time.sleep(60)"],
        emit_jsonl=False,
    )
    time.sleep(0.6)  # let the child spawn its grandchild
    log_text = (portal_env / "runs" / f"{row['id']}.log").read_text(encoding="utf-8")
    grandchild_pid = int(log_text.strip().splitlines()[-1])

    assert runner.cancel_run(row["id"]) is True
    final = runner.get_run(row["id"])
    assert final["status"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(row["pid"], 0)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)

    # Cancel is idempotent on a finished run.
    assert runner.cancel_run(row["id"]) is False


def test_apply_run_gets_event_log_env(portal_env):
    row = runner.start_run(
        "apply", {}, command=[sys.executable, "-c", "print('x')"],
    )
    assert row["event_path"] is not None
    wait_for_status(row["id"])


def test_pipeline_run_without_jsonl_has_no_event_path(portal_env):
    row = runner.start_run(
        "pipeline", {}, command=[sys.executable, "-c", "print('x')"],
        emit_jsonl=False,
    )
    assert row["event_path"] is None
    wait_for_status(row["id"])


def test_sweep_orphans_marks_dead_pids_failed(portal_env):
    # A real process that has already exited: its pid is guaranteed dead.
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    runner.init_registry()
    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (id, kind, params, status, pid, pgid, started_at, log_path, event_path) "
        "VALUES ('orph1', 'pipeline', '{}', 'running', 999999, 999999, '2026-01-01', 'l', NULL)"
    )
    conn.commit()

    swept = runner.sweep_orphans()
    assert "orph1" in swept
    row = runner.get_run("orph1")
    assert row["status"] == "failed"


# --- PID-reuse identity checks (spec PR13, sweep B F10) ---------------------


def _insert_run(run_id: str, pid: int, *, start_time: float | None,
                cmdline: list[str] | None, started_at: str) -> None:
    runner.init_registry()
    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (id, kind, params, status, pid, pgid, pid_start_time, "
        "pid_cmdline, started_at, log_path, event_path) "
        "VALUES (?, 'pipeline', '{}', 'running', ?, ?, ?, ?, ?, 'l', NULL)",
        (run_id, pid, pid, start_time, json.dumps(cmdline) if cmdline else None, started_at),
    )
    conn.commit()


def _spawn_unrelated_process() -> subprocess.Popen:
    # start_new_session makes the victim its own group leader (pgid == pid),
    # so a killpg at its pid WOULD reach it — the skip must be proven, not an
    # artifact of an unkillable group id.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_start_run_records_identity(portal_env):
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        emit_jsonl=False,
    )
    try:
        if os.path.isdir("/proc"):
            assert row["pid_start_time"] is not None
        assert json.loads(row["pid_cmdline"]) == [sys.executable, "-c", "import time; time.sleep(30)"]
    finally:
        runner.cancel_run(row["id"])


def test_cancel_does_not_signal_reused_pid(portal_env):
    # The registry claims a pid that now belongs to an unrelated process
    # (recorded identity: a run's argv — not the victim's). Cancel must
    # refuse to signal it.
    victim = _spawn_unrelated_process()
    try:
        _insert_run("reuse1", victim.pid, start_time=None,
                    cmdline=[sys.executable, "-m", "applypilot", "run", "all"],
                    started_at=runner._now())
        assert runner.cancel_run("reuse1") is True
        time.sleep(0.2)
        assert victim.poll() is None  # untouched
        assert runner.get_run("reuse1")["status"] == "cancelled"
    finally:
        victim.kill()
        victim.wait()


def test_cancel_legacy_row_skips_reused_pid(portal_env):
    # A row spawned before identity was recorded: a live process whose start
    # time postdates the row's started_at cannot be the run's child.
    victim = _spawn_unrelated_process()
    try:
        _insert_run("reuse-legacy", victim.pid, start_time=None,
                    cmdline=None, started_at="2026-01-01T00:00:00+00:00")
        assert runner.cancel_run("reuse-legacy") is True
        time.sleep(0.2)
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def test_cancel_still_kills_matching_identity(portal_env):
    # The identity check must not neuter a legitimate cancel: a row whose
    # recorded identity matches the live process is signalled normally.
    row = runner.start_run(
        "pipeline", {},
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        emit_jsonl=False,
    )
    assert runner.cancel_run(row["id"]) is True
    assert runner.get_run(row["id"])["status"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(row["pid"], 0)


def test_sweep_orphans_sweeps_reused_pid_but_keeps_live_child(portal_env):
    victim = _spawn_unrelated_process()
    try:
        # Identity mismatch (argv differs from the victim's) -> swept.
        _insert_run("reuse2", victim.pid, start_time=None,
                    cmdline=[sys.executable, "-m", "applypilot", "run", "all"],
                    started_at=runner._now())
        # Identity match -> the run is genuinely alive, left alone.
        _insert_run("live1", victim.pid, start_time=runner._proc_start_time(victim.pid),
                    cmdline=[sys.executable, "-c", "import time; time.sleep(60)"],
                    started_at=runner._now())
        if not os.path.isdir("/proc"):
            pytest.skip("identity checks need /proc")
        swept = runner.sweep_orphans()
        assert "reuse2" in swept
        assert runner.get_run("reuse2")["status"] == "failed"
        assert "live1" not in swept
        assert runner.get_run("live1")["status"] == "running"
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def test_start_run_popen_failure_leaves_a_terminal_row(portal_env, monkeypatch):
    """Portal sweep P3-6: the registry row exists before the spawn — a
    Popen failure retires the row as 'failed' instead of leaking an
    uncancellable orphan (previously the row was inserted only after a
    successful spawn, so a crash in between left nothing to cancel)."""
    def boom(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(runner.subprocess, "Popen", boom)
    with pytest.raises(OSError):
        runner.start_run("pipeline", {},
                         command=[sys.executable, "-c", "pass"],
                         emit_jsonl=False)
    conn = get_connection()
    rows = conn.execute("SELECT status, ended_at FROM runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["ended_at"] is not None
