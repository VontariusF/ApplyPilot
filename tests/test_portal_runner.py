"""Run registry tests: spawn, monitor, cancel (process-group kill), orphans.

Spec D acceptance: run start creates a registry row and spawns a subprocess
via a fake entry point; cancel records a process-group kill; invalid form
values are rejected with readable errors.
"""

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


def test_invalid_params_rejected(portal_env):
    with pytest.raises(runner.ParamError, match="unknown stage"):
        runner.build_pipeline_command({"stages": "bogus"})
    with pytest.raises(runner.ParamError, match="between 1 and 8"):
        runner.build_pipeline_command({"workers": "999"})
    with pytest.raises(runner.ParamError, match="unsupported characters"):
        runner.build_apply_command({"model": "haiku; rm -rf /"})
    with pytest.raises(runner.ParamError, match="must be an integer"):
        runner.build_apply_command({"limit": "abc"})


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
