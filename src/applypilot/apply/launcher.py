"""Apply orchestration: acquire jobs, spawn engine sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome plus the selected engine (Claude Code or
Factory Droid) for each job, parses the result, and updates the database.
Supports parallel workers via --workers.
"""

import atexit
import logging
import platform
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import (
    KIND_COVER_LETTER,
    KIND_RESUME,
    get_connection,
    resolve_version_id,
)
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)
from applypilot.apply.engines import ApplyEngine, make_engine

logger = logging.getLogger(__name__)

def _job_title(job: dict, fallback: str = "untitled") -> str:
    """Return the job title as a non-None string (B12).

    ``title`` is nullable in the schema (smart-extract can emit NULL); every
    slice site must guard against None instead of raising TypeError.
    """
    return job.get("title") or fallback


def _load_blocked():
    """Load blocked sites from config/sites.yaml."""
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active engine processes (any engine) for skip (Ctrl+C) handling
_engine_procs: dict[int, subprocess.Popen] = {}
_engine_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, agent_label: str = "claude") -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        agent_label: Engine name recorded in agent_id ("{label}-worker-{id}").

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            core = target_url.split("?")[0].rstrip("/")
            # S7: escape LIKE wildcards (and the escape char itself) so a URL
            # containing % or _ matches itself only, never unrelated rows.
            escaped = core.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            # B4: an in_progress lock whose last_attempted_at predates the
            # stale cutoff is reclaimable (the owning worker crashed).
            params: list = [_stale_lock_cutoff(),
                            config.DEFAULTS["max_apply_attempts"],
                            min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed'
                       OR (apply_status = 'in_progress' AND last_attempted_at IS NOT NULL
                           AND last_attempted_at <= ?))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"{agent_label}-worker-{worker_id}", now, row["url"]))
        conn.commit()

        job = dict(row)
        # Apply-time snapshot (spec D3): pin the document versions this worker
        # will upload so mark_result can record what was actually sent. None
        # for pre-history documents is stored honestly.
        job["resume_version_id"] = resolve_version_id(
            conn, job["url"], KIND_RESUME, job["tailored_resume_path"])
        job["cover_version_id"] = resolve_version_id(
            conn, job["url"], KIND_COVER_LETTER, job["cover_letter_path"])
        return job
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None,
                resume_version_id: int | None = None,
                cover_version_id: int | None = None) -> None:
    """Update a job's apply status in the database.

    On 'applied', records the apply-time snapshot (spec D3): the version ids
    of the documents the worker actually uploaded. None means the document
    had no version row (pre-history or history disabled) — stored as NULL.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           applied_resume_version_id = ?,
                           applied_cover_version_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, resume_version_id, cover_version_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def _stale_lock_cutoff() -> str:
    """ISO timestamp before which an in_progress lock is stale (B4).

    A worker killed mid-apply never releases its lock; locks older than
    ``config.DEFAULTS["apply_stale_lock_minutes"]`` are reclaimable.
    """
    stale_minutes = config.DEFAULTS["apply_stale_lock_minutes"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    return cutoff.isoformat()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0,
               engine: str = "claude") -> Path | None:
    """Generate a prompt file for manual debugging runs.

    Returns the prompt file path, or None if no job found. Callers that want
    the engine's manual command build it with manual_command().
    """
    engine_obj = make_engine(engine)
    job = acquire_job(target_url=target_url, min_score=min_score,
                      worker_id=worker_id, agent_label=engine_obj.name)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{_job_title(job)[:30].replace(' ', '_')}.txt"
    # S1: prompts on disk must never carry credential values (defense-in-depth
    # on top of build_prompt no longer embedding the password).
    prompt_file.write_text(prompt_mod.redact_prompt_for_log(prompt), encoding="utf-8")

    # Engine setup writes its MCP config for reference (claude:
    # .mcp-apply-{id}.json; droid: the merged global ~/.factory/mcp.json).
    port = BASE_CDP_PORT + worker_id
    engine_obj.setup(worker_id, port)

    return prompt_file


def manual_command(engine: str, prompt_file: Path, model: str,
                   worker_id: int = 0) -> str:
    """The engine's manual run command, printed by `apply --gen`."""
    engine_obj = make_engine(engine)
    if engine_obj.prompt_delivery == "file":
        worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
        return " ".join(engine_obj.build_command(prompt_file, model, worker_dir))
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    return (
        f"claude --model {model} -p --mcp-config {mcp_path} "
        f"--permission-mode bypassPermissions < {prompt_file}"
    )


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        # Apply-time snapshot (spec D3): for a manual mark the best answer to
        # "what did I send them?" is the document the job pointed at when the
        # mark was made. NULL stays NULL for pre-history documents.
        row = conn.execute(
            "SELECT tailored_resume_path, cover_letter_path FROM jobs WHERE url = ?",
            (url,),
        ).fetchone()
        resume_vid = cover_vid = None
        if row is not None:
            resume_vid = resolve_version_id(conn, url, KIND_RESUME, row["tailored_resume_path"])
            cover_vid = resolve_version_id(conn, url, KIND_COVER_LETTER, row["cover_letter_path"])
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           applied_resume_version_id = ?,
                           applied_cover_version_id = ?
            WHERE url = ?
        """, (now, resume_vid, cover_vid, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset failed jobs so they can be retried.

    Stale in_progress locks (B4: a crashed worker never released its job)
    are cleared too; fresh locks are left alone.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
          OR (apply_status = 'in_progress' AND last_attempted_at IS NOT NULL
              AND last_attempted_at <= ?)
    """, (_stale_lock_cutoff(),))
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            engine: ApplyEngine | None = None) -> tuple[str, int]:
    """Spawn an agent session for one job application.

    Args:
        engine: Engine adapter (make_engine("claude" | "droid")). Defaults
            to the Claude engine, whose behavior is identical to the
            pre-adapter implementation.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    engine = engine or make_engine("claude")
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Engine-specific setup: claude writes its per-worker MCP config; droid
    # merges this run's servers into the global ~/.factory/mcp.json.
    engine.setup(worker_id, port)

    worker_dir = reset_worker_dir(worker_id)

    # Prompt delivery: claude pipes the prompt on stdin; droid reads it from
    # a file passed with -f. The functional file for file-delivery engines
    # lives in the per-worker scratch dir (under APP_DIR, 0700 since S2) —
    # it still carries the CapSolver key the agent needs at runtime. Nothing
    # is written to LOG_DIR here (it is a log-viewer surface); --gen's
    # reference copies are redacted (S1).
    prompt_path: Path | None = None
    if engine.prompt_delivery == "file":
        prompt_path = worker_dir / "prompt.txt"
        prompt_path.write_text(agent_prompt, encoding="utf-8")

    cmd = engine.build_command(prompt_path, model, worker_dir)
    env = engine.child_env()

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {_job_title(job)[:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None
    watchdog: threading.Timer | None = None

    try:
        proc = engine.spawn(cmd, env, worker_dir)
        with _engine_lock:
            _engine_procs[worker_id] = proc

        if engine.prompt_delivery == "stdin":
            proc.stdin.write(agent_prompt)
            proc.stdin.close()

        # B13: the streaming read blocks indefinitely if the agent hangs
        # without producing output, and the old post-EOF wait never covered
        # it. A watchdog enforces the configured apply_timeout over the whole
        # run — no output or no exit within the budget kills the process.
        apply_timeout = config.DEFAULTS["apply_timeout"]
        timed_out = threading.Event()

        def _watchdog_kill() -> None:
            timed_out.set()
            logger.warning(
                "Worker %d exceeded apply_timeout=%ss; killing agent process",
                worker_id, apply_timeout,
            )
            # Look the process up in the registry instead of closing over the
            # local: the finally block may already have cleared it.
            with _engine_lock:
                current = _engine_procs.get(worker_id)
            if current is not None and current.poll() is None:
                _kill_process_tree(current.pid)

        watchdog = threading.Timer(apply_timeout, _watchdog_kill)
        watchdog.daemon = True
        watchdog.start()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                events = engine.parse_stream(line)
                if events is None:
                    # Raw (non-JSON) line — keep it in the transcript
                    text_parts.append(line)
                    lf.write(line + "\n")
                    continue
                for event in events:
                    if event.kind == "text":
                        text_parts.append(event.text)
                        lf.write(event.text + "\n")
                    elif event.kind == "action":
                        lf.write(f"  >> {event.action_desc}\n")
                        ws = get_state(worker_id)
                        cur_actions = ws.actions if ws else 0
                        update_state(worker_id,
                                     actions=cur_actions + 1,
                                     last_action=event.action_desc[:35])
                    elif event.kind == "usage":
                        stats = event.stats
                        text_parts.append(event.text)

        proc.wait(timeout=apply_timeout)
        returncode = proc.returncode
        watchdog.cancel()
        proc = None

        # The watchdog kill leaves a negative return code too, so the
        # timeout must be reported before the signal-exit "skipped" check.
        if timed_out.is_set():
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
            update_state(worker_id, status="failed",
                         last_action=f"TIMEOUT ({elapsed}s)")
            return "failed:timeout", int((time.time() - start) * 1000)

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"{engine.name}_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        result = engine.extract_result(output)

        if result.status in ("applied", "expired", "captcha", "login_issue"):
            label = result.status.upper()
            add_event(f"[W{worker_id}] {label} ({elapsed}s): {_job_title(job)[:30]}")
            update_state(worker_id, status=result.status,
                         last_action=f"{label} ({elapsed}s)")
            return result.status, duration_ms

        if result.status == "failed:unknown":
            # Bare RESULT:FAILED with no reason — no event for it (matches
            # the original parser's path).
            return result.status, duration_ms

        if result.status == "failed:no_result_line":
            add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
            return result.status, duration_ms

        reason = result.status.split(":", 1)[1]
        add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
        update_state(worker_id, status="failed",
                     last_action=f"FAILED: {reason[:25]}")
        return result.status, duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        if watchdog is not None:
            watchdog.cancel()
        with _engine_lock:
            _engine_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _split_worker_limits(effective_limit: int, workers: int) -> list[int]:
    """Divide a finite job limit across workers (B1).

    With ``--limit 2 --workers 4`` two workers get 1 job and two get 0;
    callers must run zero-slice workers with ``continuous=False`` so they
    exit cleanly instead of polling the queue forever.
    """
    base = effective_limit // workers
    extra = effective_limit % workers
    return [base + (1 if i < extra else 0) for i in range(workers)]


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                continuous: bool = False,
                engine: str = "claude") -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (ignored when continuous=True).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Model id passed verbatim to the engine.
        dry_run: Don't click Submit.
        continuous: Poll the queue forever (B1: no longer inferred from
            ``limit == 0`` — a zero slice in non-continuous mode exits
            cleanly instead of running forever).
        engine: Engine name ("claude" | "droid").

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    engine_obj = make_engine(engine)

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, agent_label=engine_obj.name)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                          model=model, dry_run=dry_run,
                                          engine=engine_obj)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {_job_title(job)[:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms,
                            resume_version_id=job.get("resume_version_id"),
                            cover_version_id=job.get("cover_version_id"))
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         engine: str = "claude") -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to; with continuous=True the limit is
            ignored and the queue is polled forever.
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Model id passed verbatim to the engine.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        engine: Engine name ("claude" | "droid").
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    engine_obj = make_engine(engine)  # readable error for unknown names

    if engine_obj.name == "droid" and workers > 1:
        # Droid reads one global ~/.factory/mcp.json with this run's CDP port
        # baked in — parallel Droid workers stay a post-spike decision, not a
        # silent footgun.
        console.print(
            "[yellow]Droid runs one worker per machine this release "
            "(~/.factory/mcp.json is global); clamping workers to 1.[/yellow]"
        )
        workers = 1

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active engine processes to skip current jobs
            with _engine_lock:
                for wid, cproc in list(_engine_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _engine_lock:
                for wid, cproc in list(_engine_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    continuous=continuous,
                    engine=engine_obj.name,
                )
            else:
                # Multi-worker — distribute limit across workers (B1: a
                # zero slice runs non-continuous and exits immediately)
                limits = (_split_worker_limits(effective_limit, workers)
                          if effective_limit else [0] * workers)

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            continuous=continuous,
                            engine=engine_obj.name,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
