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
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import (
    KIND_COVER_LETTER,
    KIND_RESUME,
    TERMINAL_APPLY_STATUSES,
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

# Jobs this process currently holds an apply lock on. The SIGTERM path
# (cancel_run SIGTERMs this whole process group) must release them:
# SystemExit is a BaseException, so worker_loop's `except Exception` /
# `except KeyboardInterrupt` cleanup never runs, and without explicit
# release a cancelled run's in-flight job stays locked until the stale
# window lapses (reliability sweep P2-3). The engine is being killed
# either way — there is no submission left to lose.
_in_flight_locks: set[str] = set()
_in_flight_guard = threading.Lock()


def _register_in_flight(url: str) -> None:
    with _in_flight_guard:
        _in_flight_locks.add(url)


def _unregister_in_flight(url: str) -> None:
    with _in_flight_guard:
        _in_flight_locks.discard(url)


def _sigterm_handler(signum, frame):
    """Exit on SIGTERM, releasing this process's in-flight apply locks."""
    with _in_flight_guard:
        in_flight = set(_in_flight_locks)
        _in_flight_locks.clear()
    for url in in_flight:
        try:
            release_lock(url)
        except Exception:
            logger.exception("SIGTERM cleanup: could not release lock for %s", url)
    sys.exit(0)


# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, _sigterm_handler)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

class AmbiguousJobURLError(ValueError):
    """F4: a --url lookup's fallback matched several distinct jobs.

    Raised instead of silently picking one — a wrong pick applies to the
    wrong posting with the wrong tailored resume.
    """


class ManualMarkConflict(RuntimeError):
    """A manual mark landed on a job a run currently owns (fix 5).

    Raised by mark_job; the portal turns it into a 409 with plain-language
    guidance instead of silently overwriting the run's state.
    """


def _resolve_target_rows(target_url: str, core: str, rows: list) -> dict | None:
    """F4: pick the job a --url lookup refers to, or refuse loudly.

    Exact matches (url or application_url equal to the target or its
    query-stripped core) win outright. Fallback rows are suffix-anchored
    matches; a single distinct normalized path is accepted, several are
    ambiguous and raise rather than guess.
    """
    if not rows:
        return None
    exact = [r for r in rows
             if target_url in (r["url"], r["application_url"])
             or core in (r["url"], r["application_url"])]
    if exact:
        # SQL ORDER BY url made the exact set deterministic.
        return dict(exact[0])
    # Trailing-slash variants of the same posting are one job, not ambiguity.
    cores = {
        (r["application_url"] or r["url"]).split("?")[0].rstrip("/") for r in rows
    }
    if len(cores) > 1:
        candidates = ", ".join(sorted(r["url"] for r in rows))
        raise AmbiguousJobURLError(
            f"--url {target_url!r} matched multiple jobs; "
            f"pass the exact URL of the intended one. Candidates: {candidates}"
        )
    return dict(rows[0])


def _queue_filters(params: list, min_score: int) -> str:
    """The queue's blocked-site/URL and manual-ATS WHERE tail, shared by
    acquire_job and derive_batch so the plan preview can never describe a
    batch the worker would refuse (count_apply_eligible documents the same
    sync rule — B8: the manual-ATS exclusion used to live only there and
    in acquire_job's post-selection check, so a plan could stage a job the
    worker then refused).

    Appends the leading filter params (stale cutoff, attempt cap, min_score)
    and then the blocked-site/URL and manual-ATS params to `params`, in
    SQL order.
    """
    params.extend([_stale_lock_cutoff(),
                   config.DEFAULTS["max_apply_attempts"],
                   min_score])
    blocked_sites, blocked_patterns = _load_blocked()
    site_clause = ""
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND site NOT IN ({placeholders})"
        params.extend(blocked_sites)
    url_clauses = ""
    if blocked_patterns:
        url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
        params.extend(blocked_patterns)
    # B8: the queue never picks a manual-ATS URL — the same
    # lowercased-containment match count_apply_eligible encodes in SQL and
    # config.is_manual_ats applies post-selection. Carrying it here keeps
    # derive_batch/count_eligible/acquire_job on one selection truth.
    ats_clauses = ""
    manual_domains = config.load_sites_config().get("manual_ats", [])
    if manual_domains:
        ats_clauses = " ".join(
            "AND COALESCE(application_url, url) NOT LIKE ?" for _ in manual_domains)
        params.extend(f"%{domain.lower()}%" for domain in manual_domains)
    return f"{site_clause} {url_clauses} {ats_clauses}"


def _queue_where(min_score: int) -> tuple[str, list]:
    """The apply queue's full WHERE clause and its binds — the one
    selection truth. acquire_job's non-target branch, derive_batch, and
    count_eligible all run this exact predicate; count_apply_eligible (the
    scheduler count) mirrors it and documents the sync rule.

    Exposes the predicate as data so exclusion diagnostics (S3) can run
    COUNT queries against the same queue shape.
    """
    params: list = []
    filter_clauses = _queue_filters(params, min_score)
    where = f"""
        WHERE tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed'
               OR (apply_status = 'in_progress' AND last_attempted_at IS NOT NULL
                   AND last_attempted_at <= ?))
          -- captcha is terminal by decision: a blocked job is never requeued
          AND COALESCE(apply_status, '') != 'captcha'
          AND (apply_attempts IS NULL OR apply_attempts < ?)
          AND fit_score >= ?
          {filter_clauses}
    """
    return where, params


def count_eligible(min_score: int = 7) -> int:
    """How many jobs the apply queue would run right now — the exact
    _queue_where predicate as a COUNT. Feeds the plan payload's
    eligible_total (S3): batch size under the cap is then comparable to
    the real queue size in one payload.
    """
    conn = get_connection()
    where, params = _queue_where(min_score)
    return conn.execute(
        f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]


def count_queue_exclusions(min_score: int = 7) -> dict[str, int]:
    """Per-reason counts of unapplied jobs the apply queue excludes (S3) —
    the plan payload's `excluded` diagnostics, so "why didn't it batch X?"
    is answerable from the payload alone.

    Buckets are independent, not a partition: a row failing two conditions
    counts in both. Statuses the plan vocabulary doesn't bucket here
    (expired, login_issue, manual, fresh in_progress locks) appear in
    neither bucket nor queue.
    """
    conn = get_connection()
    max_attempts = config.DEFAULTS["max_apply_attempts"]
    # Otherwise-ready-but-unapplied, without the score predicate — the
    # score_below_min bucket is defined against this (a fragment that
    # already required fit >= min would make the bucket a contradiction).
    ready_core = ("tailored_resume_path IS NOT NULL AND applied_at IS NULL "
                  "AND full_description IS NOT NULL "
                  "AND length(full_description) > 0")
    ready_scored = f"{ready_core} AND fit_score >= ?"
    counters: list[tuple[str, str, list]] = [
        ("not_tailored",
         "tailored_resume_path IS NULL AND applied_at IS NULL", []),
        ("score_below_min",
         f"{ready_core} AND (fit_score IS NULL OR fit_score < ?)",
         [min_score]),
        ("attempt_capped",
         f"{ready_scored} AND (apply_status IS NULL OR apply_status = 'failed') "
         "AND COALESCE(apply_attempts, 0) >= ?",
         [min_score, max_attempts]),
        ("captcha", "applied_at IS NULL AND apply_status = 'captcha'", []),
    ]

    blocked_sites, blocked_patterns = _load_blocked()
    blocked_conds, blocked_params = [], []
    if blocked_sites:
        blocked_conds.append(f"site IN ({','.join('?' * len(blocked_sites))})")
        blocked_params.extend(sorted(blocked_sites))
    if blocked_patterns:
        blocked_conds.extend("url LIKE ?" for _ in blocked_patterns)
        blocked_params.extend(blocked_patterns)
    if blocked_conds:
        counters.append((
            "blocked",
            f"{ready_scored} AND ({' OR '.join(blocked_conds)})",
            [min_score, *blocked_params]))

    manual_domains = config.load_sites_config().get("manual_ats", [])
    if manual_domains:
        ats_conds = " OR ".join(
            "COALESCE(application_url, url) LIKE ?" for _ in manual_domains)
        counters.append((
            "manual_ats",
            f"{ready_scored} AND ({ats_conds})",
            [min_score, *(f"%{d.lower()}%" for d in manual_domains)]))

    excluded: dict[str, int] = {}
    for name, condition, params in counters:
        excluded[name] = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {condition}", params).fetchone()[0]
    return excluded


# F8 (audit art_YLnPMn0i): host-path claim tokens. A `claim:`-prefixed UUID
# stamped into apply_task_id at lock time is a one-time ownership proof: only
# the holder of the token can report the outcome. The prefix is deliberately
# distinguishable from every pre-existing marker value — 'reclaimed',
# 'manual-applied'/'manual-failed', and real engine task ids — so old stores
# and the engine paths keep their exact prior behavior.
CLAIM_TOKEN_PREFIX = "claim:"


class ClaimMismatch(RuntimeError):
    """mark_result was called with a --claim that does not own the lock."""


def new_claim_token() -> str:
    """One-time claim token for a fresh host-path apply lock."""
    return f"{CLAIM_TOKEN_PREFIX}{uuid.uuid4()}"


def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, agent_label: str = "claude",
                allowed_urls: set[str] | None = None,
                claim_token: bool = False) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        agent_label: Engine name recorded in agent_id ("{label}-worker-{id}").
        allowed_urls: Gate 2 pin — when set (with no target_url), the queue
            is narrowed to exactly these URLs: a confirmed plan's batch is
            the batch that runs. Same transaction, same lock path.
        claim_token: Stamp a one-time `claim:<uuid>` ownership token into
            apply_task_id and return it as job["claim"] — the host protocol's
            apply-acquire path (audit F8). Engine paths leave it False and
            keep the exact prior apply_task_id behavior.

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
            # F4: exact rows win over the LIKE fallback, and the fallback is
            # suffix-anchored (%core or %core/%) so /jobs/123 can never match
            # the sibling /jobs/1234 the way an unanchored contains-LIKE did.
            suffix = f"%{escaped}"
            rows = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url IN (?, ?) OR application_url IN (?, ?)
                       OR url LIKE ? ESCAPE '\\' OR application_url LIKE ? ESCAPE '\\'
                       OR url LIKE ? ESCAPE '\\' OR application_url LIKE ? ESCAPE '\\')
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                ORDER BY CASE WHEN url IN (?, ?) OR application_url IN (?, ?)
                              THEN 0 ELSE 1 END, url
            """, (target_url, core, target_url, core,
                  suffix, suffix, suffix + "/%", suffix + "/%",
                  target_url, core, target_url, core)).fetchall()
            row = _resolve_target_rows(target_url, core, rows)
        else:
            # B4: an in_progress lock whose last_attempted_at predates the
            # stale cutoff is reclaimable (the owning worker crashed).
            # Gate 2: a confirmed batch pins acquisition to the planned
            # URLs — the pin only narrows the queue, never widens it.
            where, params = _queue_where(min_score)
            pin_clause = ""
            if allowed_urls is not None:
                pin_clause = f"AND url IN ({','.join('?' * len(allowed_urls))})"
                params.extend(sorted(allowed_urls))
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path,
                       apply_status
                FROM jobs
                {where}
                  {pin_clause}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Manual-ATS skip for explicit --url lookups: the queue branch now
        # excludes manual-ATS domains in SQL (B8, one selection truth with
        # derive_batch and count_apply_eligible), but a targeted lookup
        # bypasses the queue filters — the row is marked 'manual' so the
        # skip is visible rather than silent.
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
        # F8: the host path stamps a fresh one-time claim token on every
        # acquisition — a stale-lock reclaim included. The displaced
        # holder's token stops matching (its report is refused), the
        # host-path analogue of the engine path's 'reclaimed' marker below;
        # with an ownership token the lost-race write is prevented outright
        # instead of merely noted. Engine paths (claim_token=False) keep
        # the exact prior CASE behavior.
        claim = new_claim_token() if claim_token else None
        if claim_token:
            task_clause = "apply_task_id = ?"
            params: list[object] = [f"{agent_label}-worker-{worker_id}", now, claim]
        else:
            task_clause = ("apply_task_id = CASE WHEN apply_status = 'in_progress' "
                           "THEN 'reclaimed' ELSE apply_task_id END")
            # No placeholder in this clause — the engine path binds three
            # values, not four.
            params = [f"{agent_label}-worker-{worker_id}", now]
        # P2-1: reclaiming a stale lock marks the row, so the reclaiming
        # worker's result is distinguishable from a human mark when the
        # original worker finally reports (a lost race, not a conflict).
        # SQLite reads apply_status pre-UPDATE here, so the CASE fires only
        # on a genuine stale-lock reclaim.
        # B4: reclaiming a crashed worker's lock also consumes one of the
        # dead worker's attempts — SIGKILL/OS-crash deaths never reach
        # mark_result, so without this the reclaim→crash→reclaim cycle on
        # one job is unbounded and the attempt budget never applies. A
        # fresh claim leaves attempts untouched: the reported outcome
        # (mark_result) counts that attempt, so a normal failed cycle
        # costs exactly one, and the F-3 refund reverses a reclaim
        # increment on an environment failure.
        conn.execute(f"""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?,
                           apply_attempts = CASE WHEN apply_status = 'in_progress'
                                                 THEN COALESCE(apply_attempts, 0) + 1
                                                 ELSE apply_attempts END,
                           {task_clause}
            WHERE url = ?
        """, [*params, row["url"]])
        conn.commit()

        job = dict(row)
        if claim:
            job["claim"] = claim
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


def derive_batch(min_score: int = 7, limit: int = 1) -> list[dict]:
    """Read-only preview of the batch the apply queue would run (Gate 2).

    The queue's exact predicate (_queue_where — one selection truth with
    acquire_job's non-target branch and count_eligible), but a plain
    SELECT: no BEGIN IMMEDIATE, no lock, no write of any kind. Ordered the
    way the worker picks (fit_score DESC, url) and cut at the plan's batch
    size. B8: the predicate carries the manual-ATS exclusion, so the plan
    can no longer stage a job the worker refuses at acquire time.
    """
    conn = get_connection()
    where, params = _queue_where(min_score)
    rows = conn.execute(f"""
        SELECT url, title, site, application_url, tailored_resume_path,
               fit_score, location, full_description, cover_letter_path,
               apply_status
        FROM jobs
        {where}
        ORDER BY fit_score DESC, url
        LIMIT ?
    """, [*params, limit]).fetchall()
    return [dict(row) for row in rows]


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None,
                resume_version_id: int | None = None,
                cover_version_id: int | None = None,
                claim: str | None = None) -> str:
    """Record a worker's apply outcome with writer coordination (fix 5).

    On 'applied', records the apply-time snapshot (spec D3): the version ids
    of the documents the worker actually uploaded. None means the document
    had no version row (pre-history or history disabled) — stored as NULL.

    Ownership (audit F8): when the locked row's apply_task_id carries a
    `claim:<uuid>` token (stamped by acquire_job's claim_token path),
    `claim` must match it or ClaimMismatch is raised before any write —
    the token is one-time, cleared by the recording UPDATE. Rows holding
    anything else (NULL, 'reclaimed', 'manual-*' markers, engine task ids:
    old stores and engine paths) keep the exact pre-claim behavior — no
    ownership check exists for them, so they stay reportable as before.

    Coordination rule: the later writer wins only over matching state.

    - A manual mark recorded while this worker held the lock supersedes a
      matching worker result (worker applied + user marked applied): the
      user's applied_at and document snapshot stay, the stale lock is
      released, and the marker is cleared.
    - A mismatched manual mark (worker result against a mark of the other
      kind, or a manual mark on a lock the worker does not own) is a
      conflict: nothing is clobbered — the run's outcome is recorded as a
      visible `apply_conflict` note and the board keeps the manual state.

    Returns one of "recorded", "superseded", or "conflict" so callers can
    log what happened.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT apply_status, apply_task_id FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        conn.rollback()
        return "recorded"  # job vanished (e.g. removed mid-run) — nothing to write

    # F8: ownership check before any write (see the docstring). Claim-token
    # rows only exist on the host protocol's path; everything else — the
    # engine paths and pre-claim stores — is untouched by this branch.
    stored_claim = row["apply_task_id"]
    if (isinstance(stored_claim, str)
            and stored_claim.startswith(CLAIM_TOKEN_PREFIX)
            and stored_claim != claim):
        conn.rollback()
        raise ClaimMismatch(
            f"{url} is locked under a different claim token — re-acquire it "
            "with `agent apply-acquire` and report with the returned claim")

    # P2-1: a 'reclaimed' marker means a B4 stale-lock reclaim happened while
    # the reporting worker believed it still held the job. Its report is a
    # lost race against the reclaimer's own outcome — never evidence of a
    # human mark.
    reclaimed = row["apply_task_id"] == "reclaimed"

    marker = row["apply_task_id"]
    if marker is None:
        # Pre-marker row (this fork ran before coordination existed): fall
        # back to status-based detection. An in_progress row with no marker
        # is a plain run mid-flight — a later writer may legitimately record
        # over it.
        if row["apply_status"] == "applied":
            marker = "manual-applied"
        elif row["apply_status"] == "failed":
            marker = "manual-failed"
    elif not str(marker).startswith("manual-"):
        marker = None  # a real engine task id (or reclaim marker) — not a mark

    if marker in ("manual-applied", "manual-failed"):
        # A human recorded an outcome while this worker held the lock.
        if (status == "applied") == (marker == "manual-applied"):
            # Matching state: the user's record stands. Clear the marker and
            # settle the status; the manual timestamp and document snapshot
            # stay untouched.
            conn.execute("""
                UPDATE jobs SET apply_task_id = NULL, agent_id = NULL,
                               apply_status = CASE WHEN apply_status = 'in_progress'
                               THEN ? ELSE apply_status END
                WHERE url = ?
            """, (status, url))
            conn.commit()
            return "superseded"
        # Mismatch: the user says one thing, the worker another. Neither side
        # wins silently — surface the worker's outcome as a conflict note and
        # leave the manual record (including its timestamp) intact.
        note = (f"Run reported '{status}' after you marked it "
                f"{'applied' if marker == 'manual-applied' else 'failed'}"
                f" — kept your mark" + (f": {error}" if error else "."))
        conn.execute("""
            UPDATE jobs SET apply_conflict = ?,
                           apply_attempts = COALESCE(apply_attempts, 0) + 1,
                           agent_id = NULL, apply_duration_ms = ?
            WHERE url = ?
        """, (note, duration_ms, url))
        conn.commit()
        return "conflict"

    # Worker writes preserve a reclaim marker (a third writer must still
    # recognize the lost-race history) and, when the reclaim races a settled
    # outcome, record a neutral supersede note — not a user-blaming conflict.
    marker_to_keep = task_id if task_id is not None else ("reclaimed" if reclaimed else None)
    superseded_note = None
    if reclaimed and row["apply_status"] in ("applied", "failed", "captcha") and row["apply_status"] != status:
        superseded_note = (
            f"A concurrent run reported '{row['apply_status']}' after this job's "
            f"stale lock was reclaimed; the latest report ('{status}') won."
        )

    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, apply_conflict = ?,
                           agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           applied_resume_version_id = ?,
                           applied_cover_version_id = ?
            WHERE url = ?
        """, (now, superseded_note, duration_ms, marker_to_keep,
              resume_version_id, cover_version_id, url))
    else:
        attempts = _ATTEMPTS_BAN if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_conflict = ?,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", superseded_note, duration_ms, marker_to_keep, url))
    conn.commit()
    return "recorded"


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def _refund_attempt(url: str) -> None:
    """Step the attempt count back for an environment failure (F-3).

    Infra-class failures never consume an attempt (P1-1 discipline in the
    tailor/cover paths): the job stays fully retryable when the environment
    recovers. On a reclaimed claim this exactly undoes acquire_job's B4
    reclaim increment; on a fresh claim (acquire never incremented) it is
    deliberately lenient — a row left with prior failures gets one back.
    Bounded at 0 so a row that never held a count can't go negative.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_attempts = MAX(COALESCE(apply_attempts, 1) - 1, 0) "
        "WHERE url = ?",
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


def _daily_cap_reached(cap: int) -> bool:
    """True when today's application count has reached `cap` (P2-2).

    Read live at the moment of the check — not from the snapshot a run
    started with — so applications recorded by any path (a concurrent
    run, a manual mark) count toward the daily cap.
    """
    from applypilot.database import count_applications_on

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return count_applications_on(today) >= cap


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

    # Release the lock so the job stays available. --gen is a preview, not
    # an application: refund the attempt acquire_job consumed (B4) so a
    # debugging prompt never burns the retry budget.
    release_lock(job["url"])
    _refund_attempt(job["url"])

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
    # Writer coordination (spec reliability fix 5): a manual mark on a job a
    # run currently owns is refused — the run's own mark_result will record
    # the real outcome, and overwriting the in_progress row here is what
    # made board state and run reality diverge. `apply_task_id` doubles as
    # the manual-mark marker ("manual-applied" / "manual-failed") so the
    # worker can recognize a human outcome; normal worker writes and
    # reset_failed clear it back to NULL.
    row = conn.execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is not None and row["apply_status"] == "in_progress":
        conn.rollback()
        raise ManualMarkConflict(
            "A run is applying to this job right now — the card updates when "
            "it finishes. Its result will be recorded on the job; if it "
            "disagrees with your mark, the job shows the conflict."
        )
    if status == "applied":
        # Apply-time snapshot (spec D3): for a manual mark the best answer to
        # "what did I send them?" is the document the job pointed at when the
        # mark was made. NULL stays NULL for pre-history documents.
        doc_row = conn.execute(
            "SELECT tailored_resume_path, cover_letter_path FROM jobs WHERE url = ?",
            (url,),
        ).fetchone()
        resume_vid = cover_vid = None
        if doc_row is not None:
            resume_vid = resolve_version_id(conn, url, KIND_RESUME, doc_row["tailored_resume_path"])
            cover_vid = resolve_version_id(conn, url, KIND_COVER_LETTER, doc_row["cover_letter_path"])
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_task_id = 'manual-applied',
                           applied_resume_version_id = ?,
                           applied_cover_version_id = ?
            WHERE url = ?
        """, (now, resume_vid, cover_vid, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = ?, agent_id = NULL,
                           apply_task_id = 'manual-failed'
            WHERE url = ?
        """, (reason or "manual", _ATTEMPTS_BAN, url))
    conn.commit()


def reset_failed() -> int:
    """Reset failed jobs so they can be retried.

    Stale in_progress locks (B4: a crashed worker never released its job)
    are cleared too; fresh locks are left alone.

    Terminal states are skipped (B6/M4):
      - captcha: terminal by decision (the board is actively defending —
        the application never happened), so this sweep must not silently
        re-enqueue it;
      - expired / login_issue: the launcher classifies them permanent and
        the apply-result vocabulary records them terminal — an expired
        posting can never be applied to, and re-running it burns an
        attempt and a browser session before reporting expired again;
      - manual: the user's own terminal mark (manual-ATS skips, manual
        fail marks) — a sweep must not silently undo a human decision.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    protected = ["applied", "in_progress", "manual", *TERMINAL_APPLY_STATUSES]
    marks = ",".join("?" * len(protected))
    cursor = conn.execute(f"""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL,
                       apply_task_id = NULL
        WHERE (apply_status IS NOT NULL AND apply_status NOT IN ({marks}))
          OR (apply_status = 'in_progress' AND last_attempted_at IS NOT NULL
              AND last_attempted_at <= ?)
    """, (*protected, _stale_lock_cutoff()))
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _coordination_note(outcome: str, job: dict) -> str:
    """Plain-language run-log line for a coordinated (non-clobbered) result."""
    title = _job_title(job)[:30]
    if outcome == "superseded":
        return (f"Your manual mark on '{title}' stands — the run agreed with "
                "it and kept it")
    return (f"Run finished on '{title}' but conflicts with your manual mark — "
            "the job shows both")


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

# F-3: consecutive Chrome launch failures before the run stops loudly. Same
# shape as the pipeline's B6 consecutive-error cap (_MAX_ERROR_PASSES = 3):
# an environment problem fails every job identically, so churning the queue
# teaches nothing — the user fixes the environment and starts again.
_MAX_LAUNCH_FAILURES = 3

# B4: consecutive engine-signal deaths (the worker loop's "skipped"
# outcome) before the run stops loudly. The skipped branch used to release
# the lock and continue without counting toward the limit, so a
# deterministic engine death (OOM-kill on a heavy page) re-acquired the
# same queue-head job forever. Same shape as _MAX_LAUNCH_FAILURES and the
# pipeline's _MAX_ERROR_PASSES; the acquire-side attempt increment bounds
# the same loop when the run is restarted.
_MAX_CONSECUTIVE_SKIPS = 3

# M6: the terminal-failure attempts sentinel. mark_result's permanent
# branch and the manual-fail mark pin attempts to this so the queue
# predicate (apply_attempts < max_apply_attempts, a configurable cap
# currently 5) keeps the row out even if a later sweep touches attempts —
# a named constant instead of a magic 99 surviving visibly in row
# updates. A dedicated apply_banned flag would be cleaner; that needs a
# schema-touching PR.
_ATTEMPTS_BAN = 99


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
                engine: str = "claude",
                daily_cap: int | None = None,
                allowed_urls: set[str] | None = None) -> tuple[int, int]:
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
        daily_cap: Max applications per UTC day this run must respect
            (P2-2). None disables the check (manual CLI runs without a
            cap). Re-read live before every job so applications made by
            any path since the run started count.
        allowed_urls: Gate 2 pin (confirmed plan) — acquisition is
            narrowed to exactly these URLs.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    jobs_done = 0
    empty_polls = 0
    launch_failures = 0  # F-3: consecutive Chrome launch failures (env class)
    consecutive_skips = 0  # B4: consecutive engine-signal deaths (poison job)
    port = BASE_CDP_PORT + worker_id
    engine_obj = make_engine(engine)

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break
        if daily_cap is not None and not target_url and _daily_cap_reached(daily_cap):
            # P2-2: the cap is re-read live before every job — applications
            # made by any path since this run started count toward it. A job
            # already mid-flight when the cap trips still completes (at most
            # `workers` over), since a submitted application cannot be
            # un-submitted.
            add_event(f"[W{worker_id}] Daily cap reached ({daily_cap} applications today) — stopping")
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, agent_label=engine_obj.name,
                          allowed_urls=allowed_urls)
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

        # P2-3: register the lock so the SIGTERM handler (cancel_run kills
        # this whole process group) can release it — SystemExit skips the
        # except/finally cleanup below otherwise.
        _register_in_flight(job["url"])

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            try:
                chrome_proc = launch_chrome(worker_id, port=port, headless=headless)
            except Exception as e:
                # F-3: a launch-phase failure is an environment problem
                # (no Chrome profile on a fresh machine, missing binary,
                # unwritable dirs) — the same class as a provider outage
                # (P1-1): not the job's fault, so the attempt acquire
                # burned is refunded and the job stays fully retryable.
                # Bounded like the pipeline's consecutive-error cap (B6):
                # an environment problem fails every job the same way, so
                # stop the run loudly instead of churning the queue.
                logger.exception("Worker %d Chrome launch failed", worker_id)
                add_event(f"[W{worker_id}] Chrome launch failed: {str(e)[:40]}")
                release_lock(job["url"])
                _refund_attempt(job["url"])
                launch_failures += 1
                update_state(worker_id, last_action="chrome launch failed")
                if launch_failures >= _MAX_LAUNCH_FAILURES or target_url:
                    add_event(
                        f"[W{worker_id}] Stopping: Chrome will not launch "
                        f"({launch_failures} consecutive failures — fix the "
                        "environment, then start the run again)")
                    break
                continue
            launch_failures = 0

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                          model=model, dry_run=dry_run,
                                          engine=engine_obj)

            if result == "skipped":
                # Engine process died by signal. B4: this branch used to
                # release the lock and continue without counting toward the
                # limit — the job stayed queue-head and a deterministic
                # engine death re-acquired the same job forever (the
                # acquire-side attempt increment now bounds it too). Three
                # consecutive deaths stop the run loudly instead.
                release_lock(job["url"])
                consecutive_skips += 1
                add_event(f"[W{worker_id}] Skipped: {_job_title(job)[:30]}")
                if consecutive_skips >= _MAX_CONSECUTIVE_SKIPS:
                    add_event(
                        f"[W{worker_id}] Stopping: the engine died "
                        f"{consecutive_skips} consecutive times on the same "
                        "queue head (a poison job or a dying engine — fix "
                        "the environment, then start the run again)")
                    break
                continue
            elif result == "applied":
                outcome = mark_result(job["url"], "applied", duration_ms=duration_ms,
                                      resume_version_id=job.get("resume_version_id"),
                                      cover_version_id=job.get("cover_version_id"))
                if outcome != "recorded":
                    add_event(f"[W{worker_id}] {_coordination_note(outcome, job)}")
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                if result == "captcha":
                    # Engine scanned RESULT:CAPTCHA: a distinct terminal
                    # status, not a failure. permanent=True keeps the
                    # attempts ban (99) as belt-and-braces terminal.
                    outcome = mark_result(job["url"], "captcha", "captcha",
                                          permanent=True,
                                          duration_ms=duration_ms)
                else:
                    outcome = mark_result(job["url"], "failed", reason,
                                          permanent=_is_permanent_failure(result),
                                          duration_ms=duration_ms)
                if outcome != "recorded":
                    add_event(f"[W{worker_id}] {_coordination_note(outcome, job)}")
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
            # A completed outcome (applied or reported-failed) breaks the
            # consecutive-skip streak — only back-to-back signal deaths
            # trip the B4 cap.
            consecutive_skips = 0

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
            _unregister_in_flight(job["url"])
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
         engine: str = "claude", daily_cap: int | None = None,
         allowed_urls: set[str] | None = None) -> None:
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
        daily_cap: Max applications per UTC day this run must respect
            (P2-2); None disables the check. Portal preset starts pass
            the configured cap so mid-run drift cannot exceed it.
        allowed_urls: Gate 2 pin (confirmed plan) — the run draws only
            from these URLs; continuous polling is refused with a pin.
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
        if allowed_urls is not None:
            raise ValueError("a pinned batch (confirmed plan) cannot run continuously")
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
                    daily_cap=daily_cap,
                    allowed_urls=allowed_urls,
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
                            daily_cap=daily_cap,
                            allowed_urls=allowed_urls,
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
