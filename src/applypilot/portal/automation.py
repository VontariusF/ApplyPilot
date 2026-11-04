"""Automation backend: preset config, toggles, and the in-process scheduler.

automation.json lives next to the other secret-adjacent config in
APP_DIR and is written 0600 through config.write_secret_text. It holds
the three run presets, the auto-apply master toggle, the daily schedule
toggle, the run-kind -> plain-language label mapping, and runtime state
(schedule last-fired date, auto-apply pause reason).

The scheduler is a lightweight loop inside the portal process. It never
runs pipeline work itself: a tick calls the same runner.start_run path
the buttons call, so every scheduled run is a tracked subprocess with a
registry row, and a tick refuses to start while any run is active.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config, database
from applypilot.database import count_applications_on, get_connection
from applypilot.portal import runner

log = logging.getLogger(__name__)

# Find jobs preset: result level -> discovery results per site (spec).
RESULTS_LEVELS = {"low": 25, "standard": 50, "high": 100}
# Preset registry shared by the Automation page and the ask-the-agent
# panel: one plain-language identity per preset, mapped to the run kind
# it starts. Both surfaces act through start_preset/stop_preset — no
# other path may spawn automation runs.
PRESETS: dict = {
    "find_jobs": {
        "kind": "pipeline",
        "title": "Find jobs",
        "description": "Searches your job sites, scores every listing "
                       "against your resume, and prepares application documents.",
    },
    "apply_matches": {
        "kind": "apply",
        "title": "Apply to matches",
        "description": "Applies to your best-scoring jobs, up to the "
                       "daily cap you set.",
    },
    "refresh_scores": {
        "kind": "rescore",
        "title": "Refresh scores",
        "description": "Re-scores every saved job against your current resume.",
    },
}

MIN_SCORE_CHOICES = (5, 6, 7, 8, 9)
DAILY_CAP_CHOICES = (5, 10, 25)

DEFAULTS: dict = {
    "auto_apply_enabled": False,
    "schedule_enabled": False,
    "presets": {
        "find_jobs": {"results_level": "standard"},
        "apply_matches": {"min_score": 7, "daily_cap": 10, "keep_going": False},
        "refresh_scores": {},
    },
    # Plain-language run-kind labels for the UI (spec: no jargon).
    "labels": {
        "pipeline": "Finding jobs",
        "apply": "Applying",
        "rescore": "Refreshing scores",
        "tailor": "Tailoring job",
    },
    "state": {
        # UTC date the daily schedule last fired (one Find jobs run per day).
        "last_schedule_run": None,
        # While the daily cap is hit: {"on": "YYYY-MM-DD", "reason": str}.
        "auto_apply_paused": None,
    },
}

# Seconds between scheduler passes. Deliberately coarse: the scheduler only
# decides WHEN to start a tracked run, never does the work itself.
TICK_SECONDS = 30


def config_path() -> Path:
    """automation.json under the live APP_DIR (tests repoint it)."""
    return config.APP_DIR / "automation.json"


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def load() -> dict:
    """Current config merged over DEFAULTS (missing keys filled in).

    A corrupt file falls back to defaults with a logged warning rather
    than crashing the portal or the scheduler loop; the next save
    rewrites it.
    """
    cfg: dict = json.loads(json.dumps(DEFAULTS))  # deep-copy of the defaults
    path = config_path()
    if not path.exists():
        return cfg
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("automation.json is unreadable (%s); using defaults", e)
        return cfg
    for section in ("presets", "labels", "state"):
        if isinstance(stored.get(section), dict):
            cfg[section].update(stored[section])
    for key in ("auto_apply_enabled", "schedule_enabled"):
        if key in stored:
            cfg[key] = stored[key]
    return cfg


def _validate(cfg: dict) -> None:
    """Reject config values the UI never offered (defense in depth)."""
    level = cfg["presets"]["find_jobs"].get("results_level")
    if level not in RESULTS_LEVELS:
        raise runner.ParamError(f"results_level must be one of {', '.join(RESULTS_LEVELS)}")
    apply_preset = cfg["presets"]["apply_matches"]
    if apply_preset.get("min_score") not in MIN_SCORE_CHOICES:
        raise runner.ParamError("min_score must be between 5 and 9")
    if apply_preset.get("daily_cap") not in DAILY_CAP_CHOICES:
        raise runner.ParamError("daily_cap must be one of 5, 10, 25")
    for key in ("keep_going", "auto_apply_enabled", "schedule_enabled"):
        if not isinstance(cfg[key] if key in cfg else apply_preset.get(key), bool):
            raise runner.ParamError(f"{key} must be a boolean")


def save(cfg: dict) -> dict:
    """Validate and persist the config at 0600 (same path as .env writes)."""
    _validate(cfg)
    config.write_secret_text(config_path(), json.dumps(cfg, indent=2) + "\n")
    return cfg


def update(updates: dict) -> dict:
    """Merge a partial update (from the Automation page) into the config."""
    cfg = load()
    presets = updates.get("presets") or {}
    if "find_jobs" in presets:
        cfg["presets"]["find_jobs"].update(presets["find_jobs"])
    if "apply_matches" in presets:
        cfg["presets"]["apply_matches"].update(presets["apply_matches"])
    for key in ("auto_apply_enabled", "schedule_enabled"):
        if key in updates:
            cfg[key] = updates[key]
    return save(cfg)


def run_kind_label(kind: str) -> str:
    """Stored plain-language label for a run kind (raw kind as fallback)."""
    return load()["labels"].get(kind, kind)


def run_summary(run: dict) -> str:
    """Plain-language one-line summary of a run row (no jargon).

    Spec: no model ids, workers, headless, or raw stage names outside
    the Adjust disclosures — run rows speak the preset language. Built
    on the stored kind label, with a qualifier only where it adds
    information (one-job apply, keep-going apply).
    """
    kind = run["kind"]
    try:
        params = json.loads(run["params"]) if run["params"] else {}
    except (TypeError, json.JSONDecodeError):
        params = {}
    label = run_kind_label(kind)
    if kind == "apply":
        if params.get("url"):
            return f"{label} — one job"
        if params.get("continuous"):
            return f"{label} — keeps going"
    return label


def progress_for_run(run: dict) -> dict:
    """Plain-language progress for the jobs-board strip.

    Counts store rows stamped since the run started (discovered,
    scored, or applied, by kind). Store timestamps are UTC isoformat,
    so a 19-char prefix keeps string comparison consistent.
    """
    started = (run["started_at"] or "")[:19]
    column_unit = {"pipeline": ("discovered_at", "found"),
                   "rescore": ("scored_at", "refreshed"),
                   "apply": ("applied_at", "applied")}
    if run["kind"] not in column_unit:
        return {"count": None, "unit": "done"}
    column, unit = column_unit[run["kind"]]
    conn = get_connection()
    count = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE substr({column}, 1, 19) >= ?",
        (started,)).fetchone()[0]
    return {"label": run_summary(run), "count": count, "unit": unit}


# ---------------------------------------------------------------------------
# Presets -> runner params
# ---------------------------------------------------------------------------

def preset_params(cfg: dict | None = None, name: str = "") -> dict:
    """Runner params for a preset name ("find_jobs"|"apply_matches"|
    "refresh_scores") — the same dict a manual form would submit."""
    cfg = cfg or load()
    if name == "find_jobs":
        level = cfg["presets"]["find_jobs"]["results_level"]
        return {"stages": "all", "results_per_site": str(RESULTS_LEVELS[level])}
    if name == "apply_matches":
        preset = cfg["presets"]["apply_matches"]
        return {"min_score": str(preset["min_score"]),
                "continuous": "1" if preset["keep_going"] else ""}
    if name == "refresh_scores":
        return {}
    raise runner.ParamError(f"unknown preset {name!r}")


def start_preset(name: str) -> dict:
    """Start a preset as a tracked run (the Automation-page button path).

    Apply runs get limit = today's remaining cap, so a manual start cannot
    blow past the cap the user set.
    """
    if name not in PRESETS:
        raise runner.ParamError(f"unknown preset {name!r}")
    cfg = load()
    if name == "apply_matches":
        status = cap_status(cfg)
        if status["remaining"] <= 0:
            raise runner.ParamError(cap_pause_reason(cfg, status))
        params = preset_params(cfg, name)
        params["limit"] = str(status["remaining"])
        return runner.start_run("apply", params)
    return runner.start_run(PRESETS[name]["kind"], preset_params(cfg, name))


def stop_preset(name: str) -> bool:
    """Stop a preset: cancel its newest active run of the preset's kind.

    Returns True when a run was cancelled, False when nothing was
    running. Raises runner.ParamError for an unknown preset name.
    """
    if name not in PRESETS:
        raise runner.ParamError(f"unknown preset {name!r}")
    kind = PRESETS[name]["kind"]
    active = sorted(
        (r for r in runner.running_runs() if r["kind"] == kind),
        key=lambda r: r["started_at"], reverse=True,
    )
    if not active:
        return False
    return runner.cancel_run(active[0]["id"])


# ---------------------------------------------------------------------------
# Daily cap
# ---------------------------------------------------------------------------

def applications_today() -> int:
    """Applications recorded in the store today (UTC calendar day)."""
    return count_applications_on(_today())


def cap_status(cfg: dict | None = None) -> dict:
    """What the automation cards show about the daily cap."""
    cfg = cfg or load()
    cap = cfg["presets"]["apply_matches"]["daily_cap"]
    used = applications_today()
    paused = cfg["state"].get("auto_apply_paused")
    return {
        "cap": cap,
        "used_today": used,
        "remaining": max(0, cap - used),
        "paused": paused is not None,
        "pause_reason": paused["reason"] if paused else None,
    }


def cap_pause_reason(cfg: dict, status: dict) -> str:
    return (f"Daily cap reached — {status['used_today']} of {status['cap']} "
            f"applications today. Auto-apply resumes tomorrow, or raise the cap.")


# ---------------------------------------------------------------------------
# Run starts (guarded) and single-job triggers
# ---------------------------------------------------------------------------

_start_lock = threading.Lock()


def start_guarded(kind: str, params: dict) -> dict | None:
    """Start a run only when the registry has no active run.

    The scheduler's double-start guard: check-then-spawn happens under one
    lock, and the registry row (status 'running') is inserted inside
    runner.start_run, so two ticks can never both pass the check.
    """
    with _start_lock:
        if runner.running_runs():
            return None
        return runner.start_run(kind, params)


def start_single_job_tailor(url: str, include_cover: bool = False) -> dict:
    """Tailor-now: reset the job's tailoring state, then run tailor+cover+pdf
    scoped to exactly this job, tracked in the runs registry.

    Raises:
        runner.ParamError: when the job URL is not in the store.
    """
    # The portal app factory inits the store; the trigger may also fire
    # straight after process start (and in tests) without it.
    database.init_db()
    if database.get_connection().execute(
        "SELECT 1 FROM jobs WHERE url = ?", (url,)
    ).fetchone() is None:
        raise runner.ParamError(f"job not found: {url}")
    database.reset_job_tailoring(database.get_connection(), url,
                                 include_cover=include_cover)
    return runner.start_run("tailor", {"url": url, "min_score": "0"})


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def run_tick(cfg: dict | None = None, now: datetime | None = None) -> dict:
    """One scheduler pass. Returns what it did (tests assert on this).

    Fires the daily schedule (one Find jobs run per UTC day) and auto-apply
    (apply runs until the daily cap), only for enabled toggles, only when
    no run is active. The cap pause is recorded as soon as the day's
    applications reach the cap — even mid-run — so the UI always sees why.
    """
    cfg = cfg or load()
    today = _today(now)
    state = cfg["state"]
    outcome: dict = {"started": [], "skipped": [], "paused": None}
    # Idempotent: the app factory inits these at startup; ticks and tests
    # may reach run_tick without either.
    database.init_db()
    runner.init_registry()

    if not (cfg["schedule_enabled"] or cfg["auto_apply_enabled"]):
        return outcome

    # A cap pause ends with the calendar day that filled it.
    paused = state.get("auto_apply_paused")
    if paused and paused.get("on") != today:
        state["auto_apply_paused"] = None
        paused = None
        save(cfg)

    if cfg["auto_apply_enabled"] and not paused:
        status = cap_status(cfg)
        if status["remaining"] <= 0:
            reason = cap_pause_reason(cfg, status)
            state["auto_apply_paused"] = {"on": today, "reason": reason}
            save(cfg)
            paused = state["auto_apply_paused"]
            outcome["paused"] = reason

    if runner.running_runs():
        outcome["skipped"].append("a run is already active")
        return outcome

    if cfg["schedule_enabled"] and state.get("last_schedule_run") != today:
        row = start_guarded("pipeline", preset_params(cfg, "find_jobs"))
        if row is None:  # raced into activity between the check and spawn
            outcome["skipped"].append("run start refused — a run became active")
            return outcome
        state["last_schedule_run"] = today
        save(cfg)
        outcome["started"].append(
            {"kind": "pipeline", "trigger": "schedule", "run_id": row["id"]})
        return outcome  # one trigger per tick; apply waits for the next

    if cfg["auto_apply_enabled"] and not state.get("auto_apply_paused"):
        status = cap_status(cfg)
        if status["remaining"] <= 0:
            outcome["skipped"].append("daily cap reached")
            return outcome
        params = preset_params(cfg, "apply_matches")
        params["limit"] = str(status["remaining"])
        row = start_guarded("apply", params)
        if row is None:
            outcome["skipped"].append("run start refused — a run became active")
            return outcome
        outcome["started"].append(
            {"kind": "apply", "trigger": "auto_apply", "run_id": row["id"]})
    return outcome


_scheduler_thread: threading.Thread | None = None


def start_scheduler() -> None:
    """Start the in-portal scheduler loop (idempotent)."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_thread = threading.Thread(
        target=_loop, name="automation-scheduler", daemon=True)
    _scheduler_thread.start()


def _loop() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        try:
            run_tick()
        except Exception:
            # A broken tick must not kill the loop; log and let the next
            # tick retry.
            log.exception("automation scheduler tick failed")
