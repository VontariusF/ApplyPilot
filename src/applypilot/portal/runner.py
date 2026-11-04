"""Run registry: pipeline/apply runs as tracked subprocesses.

Runs are spawned as the package's own console entry points
(``python -m applypilot …``) in their own process group, never inside the
uvicorn process — this isolates signal handlers, the Rich console, and
Chrome cleanup, and makes cancel = kill process group. Registry rows live
in the same SQLite store, so they survive uvicorn restarts.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config
from applypilot.database import get_connection

# Params allowed per run kind, with validation bounds. Model ids are
# argv-passed (no shell), but are still character-restricted so a pasted
# value can never confuse a downstream CLI parser.
_PIPELINE_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf", "all")
_VALIDATION_MODES = ("strict", "normal", "lenient")
_MODEL_CHARS = {c for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-./"}

CANCEL_GRACE_SECONDS = 5.0

# Slack (seconds) when comparing a live process's /proc start time against
# the identity recorded at spawn — clock-tick rounding plus the gap between
# Popen and the /proc read must never read as a pid reuse.
_IDENTITY_SLACK_SECONDS = 5.0


class ParamError(ValueError):
    """A run form submitted an invalid value."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_param(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = (params.get(key) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as e:
        raise ParamError(f"{key} must be an integer (got {raw!r})") from e
    if not lo <= val <= hi:
        raise ParamError(f"{key} must be between {lo} and {hi} (got {val})")
    return val


def _model_param(params: dict, key: str, default: str) -> str:
    raw = (params.get(key) or "").strip() or default
    if not set(raw) <= _MODEL_CHARS or len(raw) > 120:
        raise ParamError(f"{key} has unsupported characters")
    return raw


def _url_param(params: dict, key: str = "url", required: bool = False) -> str | None:
    """Validated job URL from a form value (http/https only, argv-safe)."""
    raw = (params.get(key) or "").strip()
    if not raw:
        if required:
            raise ParamError(f"{key} is required")
        return None
    if not raw.startswith(("http://", "https://")) or len(raw) > 2048:
        raise ParamError(f"{key} must be an http(s) URL")
    if not set(raw) <= _MODEL_CHARS | {"?", "&", "=", "#", "%", "~", "[", "]", "@", "!", "$", "'", "(", ")", "*", "+", ",", ";"}:
        raise ParamError(f"{key} has unsupported characters")
    return raw


def build_pipeline_command(params: dict) -> list[str]:
    """Validated argv for a pipeline run (never shell-interpolated)."""
    stages_raw = (params.get("stages") or "all").strip() or "all"
    stages = [s.strip() for s in stages_raw.split(",") if s.strip()]
    for s in stages:
        if s not in _PIPELINE_STAGES:
            raise ParamError(f"unknown stage {s!r} — valid: {', '.join(_PIPELINE_STAGES)}")
    validation = (params.get("validation") or "normal").strip()
    if validation not in _VALIDATION_MODES:
        raise ParamError(f"validation must be one of {', '.join(_VALIDATION_MODES)}")
    min_score = _int_param(params, "min_score", 7, 0, 10)
    workers = _int_param(params, "workers", 1, 1, 8)

    argv = [sys.executable, "-m", "applypilot", "run", *stages,
            "--min-score", str(min_score), "--workers", str(workers),
            "--validation", validation]
    results_per_site = _int_param(params, "results_per_site", 0, 0, 500)
    if results_per_site:  # 0 = not set — discovery falls back to searches.yaml
        argv += ["--results-per-site", str(results_per_site)]
    if params.get("stream"):
        argv.append("--stream")
    if params.get("dry_run"):
        argv.append("--dry-run")
    return argv


def build_apply_command(params: dict) -> list[str]:
    """Validated argv for an apply run (never shell-interpolated)."""
    argv = [sys.executable, "-m", "applypilot", "apply",
            "--min-score", str(_int_param(params, "min_score", 7, 0, 10)),
            "--workers", str(_int_param(params, "workers", 1, 1, 4)),
            "--model", _model_param(params, "model", "haiku")]
    url = _url_param(params)
    if url:
        # One-job apply run: PR #6's exact-first --url matching does the rest.
        argv += ["--url", url]
    if params.get("limit", "").strip():
        argv += ["--limit", str(_int_param(params, "limit", 1, 0, 500))]
    if params.get("dry_run"):
        argv.append("--dry-run")
    if params.get("headless"):
        argv.append("--headless")
    if params.get("continuous"):
        argv.append("--continuous")
    return argv


def build_tailor_command(params: dict) -> list[str]:
    """Validated argv for a single-job tailor run (tailor + cover + pdf).

    min_score defaults to 0: the user explicitly picked this job, so its
    stored fit score must not gate the run (a NULL score tailors nothing).
    """
    url = _url_param(params, required=True)
    min_score = _int_param(params, "min_score", 0, 0, 10)
    argv = [sys.executable, "-m", "applypilot", "run", "tailor", "cover", "pdf",
            "--url", str(url), "--min-score", str(min_score)]
    if params.get("dry_run"):
        argv.append("--dry-run")
    return argv


def build_rescore_command(params: dict) -> list[str]:
    """Validated argv for a rescore-all run (no user params).

    Re-scoring every described job is an LLM call per job, so it runs as a
    tracked subprocess like pipeline/apply runs (E).
    """
    return [sys.executable, "-m", "applypilot", "rescore", "--all"]


# ---------------------------------------------------------------------------
# Registry storage
# ---------------------------------------------------------------------------

def init_registry() -> None:
    """Create the runs table (idempotent) in the shared store."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            params      TEXT NOT NULL,
            status      TEXT NOT NULL,
            pid         INTEGER,
            pgid        INTEGER,
            pid_start_time REAL,
            pid_cmdline TEXT,
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            exit_code   INTEGER,
            log_path    TEXT,
            event_path  TEXT
        )
    """)
    # Forward migration: registries created before identity recording (the
    # PID-reuse fix) get the new columns; rows keep NULL = unchecked.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "pid_start_time" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN pid_start_time REAL")
    if "pid_cmdline" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN pid_cmdline TEXT")
    conn.commit()


def get_run(run_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def running_runs() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
    return [dict(r) for r in rows]


def try_claim_terminal(run_id: str, status: str, exit_code: int | None = None) -> bool:
    """Atomically transition a run from 'running' to a terminal status.

    Returns True when this call made the transition — used by both the
    monitor thread and cancel_run so terminal states can never overwrite
    each other (a user cancel must not be relabelled 'failed' by the
    monitor seeing the killed process's exit code).
    """
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE runs SET status = ?, exit_code = ?, ended_at = ? "
        "WHERE id = ? AND status = 'running'",
        (status, exit_code, _now(), run_id),
    )
    conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# PID-reuse identity checks
#
# A stored pid is only a handle: after the run's child exits, the kernel can
# hand the same pid to an unrelated process. Everything that signals or
# judges a stored pid first asks whether the live process still matches the
# identity recorded at spawn (or, for legacy rows, whether its start time
# predates the registry row — a child can never start after it was inserted).
# ---------------------------------------------------------------------------

def _proc_start_time(pid: int | None) -> float | None:
    """Wall-clock epoch seconds when `pid` started (Linux /proc; None elsewhere)."""
    if not pid:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        boot = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
                break
        if boot is None:
            return None
        # comm can contain spaces/parens — fields after the last ')' start at #3,
        # so starttime (field 22) is index 19 of the remainder.
        ticks = float(stat.rsplit(")", 1)[1].split()[19])
        clk_tck = os.sysconf("SC_CLK_TCK") or 100
        return boot + ticks / clk_tck
    except (OSError, ValueError, IndexError):
        return None


def _proc_cmdline(pid: int | None) -> list[str] | None:
    """The process's argv (Linux /proc); None when unreadable, empty (zombie), or off-Linux."""
    if not pid:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _process_matches_row(row: dict, pid: int | None) -> bool | None:
    """Can the live process at `pid` still be this run's child?

    Returns True when identity confirms the child, False when it contradicts
    (the pid was reused by an unrelated process), or None when identity can't
    be checked — no /proc, or a legacy row with nothing comparable. Callers
    keep the pre-identity behavior on None.
    """
    live_start = _proc_start_time(pid)
    live_cmdline = _proc_cmdline(pid)
    if live_start is None and live_cmdline is None:
        return None  # no /proc (or already reaped) — nothing to compare

    recorded_cmdline = None
    if row.get("pid_cmdline"):
        try:
            recorded_cmdline = json.loads(row["pid_cmdline"])
        except ValueError:
            recorded_cmdline = None

    # argv recorded at spawn is a strong signal: a reused pid almost never
    # runs the exact same argv as our child.
    if recorded_cmdline is not None and live_cmdline is not None and live_cmdline != recorded_cmdline:
        return False

    recorded_start = row.get("pid_start_time")
    if recorded_start is not None and live_start is not None:
        return abs(live_start - recorded_start) <= _IDENTITY_SLACK_SECONDS

    # Legacy row (spawned before identity was recorded): the run's child
    # cannot have started after the registry row was written.
    started_at = _parse_iso(row.get("started_at"))
    if live_start is not None and started_at is not None:
        return live_start <= started_at + _IDENTITY_SLACK_SECONDS

    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # Unexpected (e.g. Windows semantics) — assume alive.


def sweep_orphans() -> list[str]:
    """At startup: mark 'running' rows whose process is gone as failed.

    Returns the swept run ids. Rows with a live pid are left alone —
    cancel still works for them via the stored pgid.
    """
    swept: list[str] = []
    for row in running_runs():
        pid = row["pid"]
        # A live pid that fails the identity check is a reuse: the row would
        # stay 'running' forever (its real child is gone), so sweep it.
        if not _pid_alive(pid) or _process_matches_row(row, pid) is False:
            try_claim_terminal(row["id"], "failed")
            swept.append(row["id"])
    return swept


# ---------------------------------------------------------------------------
# Spawn / cancel
# ---------------------------------------------------------------------------

def runs_dir() -> Path:
    """Per-run log and event files live under ~/.applypilot/runs."""
    d = config.APP_DIR / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start_run(kind: str, params: dict, command: list[str] | None = None,
              emit_jsonl: bool | None = None) -> dict:
    """Spawn a run subprocess and record it.

    Args:
        kind: "pipeline", "apply", "rescore", or "tailor".
        params: Form dict; validated by the build functions.
        command: Override argv (tests use a fake entry point). Must never
            come from raw user input — routes pass build_* output.
        emit_jsonl: Whether the child gets APPLYPILOT_EVENT_LOG. Defaults
            to True for apply runs (dashboard events drive SSE).

    Returns:
        The registry row (as a dict).
    Raises:
        ParamError: on invalid form values.
    """
    if kind not in ("pipeline", "apply", "rescore", "tailor"):
        raise ParamError(f"unknown run kind {kind!r}")
    init_registry()  # idempotent; start_run must work without prior setup
    if command is None:
        if kind == "pipeline":
            command = build_pipeline_command(params)
        elif kind == "apply":
            command = build_apply_command(params)
        elif kind == "tailor":
            command = build_tailor_command(params)
        else:
            command = build_rescore_command(params)

    run_id = uuid.uuid4().hex[:12]
    log_path = runs_dir() / f"{run_id}.log"
    event_path = runs_dir() / f"{run_id}.jsonl"

    env = os.environ.copy()
    if emit_jsonl if emit_jsonl is not None else kind == "apply":
        env["APPLYPILOT_EVENT_LOG"] = str(event_path)
    else:
        env.pop("APPLYPILOT_EVENT_LOG", None)

    log_file = open(log_path, "ab")  # noqa: SIM115 — lifetime = child lifetime
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # own process group for killpg
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(
        command,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=str(Path.home()),
        **popen_kwargs,
    )

    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (id, kind, params, status, pid, pgid, pid_start_time, pid_cmdline, "
        "started_at, log_path, event_path) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
        (run_id, kind, json.dumps(params), proc.pid, proc.pid,
         _proc_start_time(proc.pid), json.dumps(command), _now(),
         str(log_path), str(event_path) if env.get("APPLYPILOT_EVENT_LOG") else None),
    )
    conn.commit()

    _monitor(proc, run_id, log_file)
    row = get_run(run_id)
    assert row is not None  # just inserted
    return row


def _monitor(proc: subprocess.Popen, run_id: str, log_file) -> None:
    """Watch a running child in a daemon thread; record its terminal state."""
    import threading

    def watch() -> None:
        code = proc.wait()
        log_file.close()
        # Claim first; a concurrent cancel (which claimed 'cancelled') wins.
        try_claim_terminal(run_id, "completed" if code == 0 else "failed",
                           exit_code=code)

    threading.Thread(target=watch, name=f"run-monitor-{run_id}", daemon=True).start()


def cancel_run(run_id: str) -> bool:
    """Kill a running run's process group (SIGTERM, then SIGKILL).

    The child (launcher) handles SIGTERM by exiting, which runs its own
    atexit Chrome cleanup; the process-group kill also reaches Chrome and
    any agent CLI it spawned (they inherit the group).

    Returns:
        True when a running run was cancelled, False otherwise.
    """
    row = get_run(run_id)
    if not row or row["status"] != "running":
        return False

    # Claim 'cancelled' before signalling so the monitor thread (which will
    # see the killed child's exit code) can never relabel the run 'failed'.
    if not try_claim_terminal(run_id, "cancelled"):
        return False

    pgid = row["pgid"] or row["pid"]
    if _process_matches_row(row, pgid) is False:
        # PID reuse: the group leader is not the child we spawned — signalling
        # it would kill an unrelated process. The run is already terminal.
        _append_run_event(row["event_path"],
                          "cancel skipped: recorded pid was reused by an unrelated process")
        return True

    _signal_group(pgid, signal.SIGTERM)
    deadline = time.time() + CANCEL_GRACE_SECONDS
    while time.time() < deadline:
        if not _group_alive(pgid):
            break
        time.sleep(0.1)
    else:
        _signal_group(pgid, signal.SIGKILL)

    _append_run_event(row["event_path"], "cancelled by user (process group killed)")
    return True


def retry_run(run_id: str) -> dict:
    """Re-run a failed run with identical parameters.

    The registry's `params` column holds the original form dict (rendered
    in plain language by automation.run_summary), so re-validating it
    through the build functions reproduces the failed run's exact command.
    Runs spawned with a command override (tests) rebuild from params instead.

    Raises:
        ParamError: when the run doesn't exist, isn't failed, or its stored
            params no longer validate.
    """
    row = get_run(run_id)
    if row is None:
        raise ParamError(f"run {run_id} not found")
    if row["status"] != "failed":
        raise ParamError("only failed runs can be retried")
    try:
        params = json.loads(row["params"]) if row["params"] else {}
    except (TypeError, json.JSONDecodeError) as e:
        raise ParamError("stored run parameters are unreadable") from e
    return start_run(row["kind"], params)


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pgid, sig)
        else:  # Windows: no process groups; signal the leader only.
            os.kill(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0) if hasattr(os, "killpg") else os.kill(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _append_run_event(event_path: str | None, msg: str) -> None:
    """Portal-side event (cancel, cancel-failure) into the run's JSONL."""
    if not event_path:
        return
    from applypilot.apply import eventlog

    writer = eventlog.get_writer(event_path)
    if writer is not None:
        writer.write({"kind": "run", "msg": msg})
