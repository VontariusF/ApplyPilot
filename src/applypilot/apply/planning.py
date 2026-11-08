"""Gate 2/3 planning for `applypilot apply` (spec art_A71wyYS4).

The plan-then-confirm contract for externally driven batches:

- ``apply --plan`` derives the batch read-only (launcher.derive_batch —
  the same queue predicate acquire_job runs), prints it as ONE pure-JSON
  payload (job URLs, per-job estimates, daily-cap headroom, a plan hash),
  and stages the per-batch token in APP_DIR/apply-plan.json. The job
  store is untouched.
- ``apply --i-confirm <plan-hash>`` on a second invocation the same UTC
  day verifies the token against the staged plan and the live queue, and
  only then runs the batch — pinned to the planned URLs — through the
  launcher's own acquire/lock paths. The token is consumed on use: one
  plan, one batch, never replayed, and an edited or drifted job set
  refuses with a structured error.
- Externally driven batches default to the smallest automation preset's
  daily cap (automation.DAILY_CAP_CHOICES[0] == 5); only an explicit
  --daily-cap lifts it. The cap rides into launcher.main's live-from-DB
  guard, so concurrent protocol, portal, and manual starts share one
  accounting (PR #50 semantics) — this module adds no counting of its
  own.

The staged token lives next to the other app state (0600 via
config.write_secret_text) and carries job URLs, never credentials.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from applypilot import config
from applypilot.database import count_applications_on

# Gate 3: the default daily cap for externally driven batches — the
# smallest preset the portal's Automation page offers. An explicit
# --daily-cap on the planning invocation is the only way past it.
AGENT_DEFAULT_DAILY_CAP = 5

STAGED_PLAN_FILE = "apply-plan.json"


# ---------------------------------------------------------------------------
# Refusals — each carries the protocol error code the host agent branches on
# ---------------------------------------------------------------------------

class PlanError(RuntimeError):
    """A plan-then-confirm refusal (the CLI renders it as the JSON error
    envelope with this code and details)."""

    code = "plan_error"
    details: Optional[dict] = None

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details


class PlanEmptyError(PlanError):
    code = "plan_empty"


class PlanRequiredError(PlanError):
    code = "plan_required"


class PlanStaleError(PlanError):
    code = "plan_stale"


class PlanEditedError(PlanError):
    code = "plan_edited"


class PlanHashMismatchError(PlanError):
    code = "plan_hash_mismatch"


class PlanCapMismatchError(PlanError):
    code = "plan_cap_mismatch"


class PlanDriftError(PlanError):
    code = "plan_drift"


# ---------------------------------------------------------------------------
# Token mechanics: hash, staging, consumption
# ---------------------------------------------------------------------------

def utc_today() -> str:
    """Today's UTC date (YYYY-MM-DD) — the token's freshness scope."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def plan_path() -> Path:
    # Read at call time: tests repoint config.APP_DIR per fixture.
    return config.APP_DIR / STAGED_PLAN_FILE


def plan_hash(core: dict) -> str:
    """sha256 over the plan's canonical content — the per-batch token."""
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_core(day: str, min_score: int, cap: int, urls: list[str]) -> dict:
    """The hash-relevant plan content (estimates and run mechanics excluded)."""
    return {"day": day, "min_score": min_score, "cap": cap, "urls": urls}


def _estimate_seconds() -> int:
    # The run's per-job watchdog budget — an honest upper bound, not a guess.
    return int(config.DEFAULTS["apply_timeout"])


def stage_batch_plan(min_score: int, limit: Optional[int],
                     explicit_cap: Optional[int]) -> dict:
    """Derive the batch read-only, stage the token, return the payload.

    Nothing in the job store is written: the batch comes from a plain
    SELECT (launcher.derive_batch) and the only file touched is the token.

    B2: a None limit — the CLI's omitted --limit — sizes the batch to
    everything the daily cap allows, min(headroom, eligible_total),
    instead of silently truncating to the apply run's default of 1 (the
    apply RUN default is untouched). The payload carries limit_applied,
    eligible_total, and per-reason exclusion counts (S3), so truncation
    and exclusion are never silent — an operator reading the plan can
    tell "only one job is eligible" from "the batch was capped".
    """
    from applypilot.apply.launcher import (
        count_eligible,
        count_queue_exclusions,
        derive_batch,
    )

    cap, cap_source = ((explicit_cap, "explicit") if explicit_cap is not None
                       else (AGENT_DEFAULT_DAILY_CAP, "default"))
    day = utc_today()
    applied_today = count_applications_on(day)
    headroom = max(cap - applied_today, 0)
    eligible_total = count_eligible(min_score=min_score)
    excluded = count_queue_exclusions(min_score)
    # An explicit --limit is honored exactly, even when it over-requests
    # (derive_batch's LIMIT simply returns what exists).
    limit_applied = min(headroom, eligible_total) if limit is None else limit

    batch = derive_batch(min_score=min_score, limit=limit_applied)
    if not batch:
        raise PlanEmptyError(
            "Nothing to apply — no job passes the apply queue's filters. "
            "Run the discover/enrich/score/tailor stages first.",
            details={"eligible_total": eligible_total,
                     "headroom": headroom,
                     "requested_limit": limit,
                     "excluded": excluded})

    urls = [job["url"] for job in batch]
    digest = plan_hash(_plan_core(day, min_score, cap, urls))
    estimate = _estimate_seconds()

    payload = {
        "plan_hash": digest,
        "day": day,
        "commits_nothing": True,
        "batch_size": len(batch),
        "min_score": min_score,
        "cap": cap,
        "cap_source": cap_source,
        "applied_today": applied_today,
        "headroom": headroom,
        # B2/S3: batch sizing and exclusion diagnostics — the plan can
        # only say what is IN the batch, so it also says what is not,
        # and why.
        "limit_applied": limit_applied,
        "eligible_total": eligible_total,
        "excluded": excluded,
        "skipped_manual_ats": excluded.get("manual_ats", 0),
        "estimate_basis": f"apply_timeout={estimate}s per job "
                          "(the run's watchdog budget — an upper bound)",
        "jobs": [
            {
                "url": job["url"],
                "application_url": job.get("application_url"),
                "title": job.get("title"),
                "site": job.get("site"),
                "fit_score": job.get("fit_score"),
                "estimate_seconds": estimate,
            }
            for job in batch
        ],
        "next": f"applypilot apply --i-confirm {digest} "
                "(same flags as this --plan run, same UTC day)",
    }
    staged = {**_plan_core(day, min_score, cap, urls),
              "plan_hash": digest,
              "staged_at": datetime.now(timezone.utc).isoformat()}
    config.write_secret_text(plan_path(), json.dumps(staged, indent=2))
    return payload


def load_staged_plan() -> dict | None:
    """The staged token, or None when nothing is staged."""
    path = plan_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PlanEditedError(
            "the staged plan file is unreadable — run "
            "'applypilot apply --plan' again",
            details={"path": str(path), "reason": str(exc)})


def consume_staged_plan() -> None:
    """Burn the token — the plan runs once, or not at all."""
    plan_path().unlink(missing_ok=True)


def verify_confirm(given_hash: str, explicit_cap: Optional[int],
                   min_score: Optional[int], limit: Optional[int]) -> dict:
    """Check a --i-confirm token against the staged plan and the live queue.

    Refusal order is most-truthful-first: nothing staged → stale (wrong
    UTC day) → edited on disk → wrong token → cap disagreement → flag
    disagreement → drift (the live queue no longer matches the plan).
    On success returns {"urls", "cap", "min_score"} for the pinned run.

    B3: an OMITTED --min-score (None) inherits the staged plan's threshold
    — the natural confirm (no flags repeated) must not drift-refuse just
    because the CLI's default (7) rode along when the plan staged a
    different one. An explicitly given value must still agree.

    The live-set comparison re-derives with the STAGED selection, never
    the confirm invocation's flags — the natural confirm (no flags
    repeated) must compare the planned batch against today's queue
    apples to apples, instead of collapsing to the apply default of one
    job and refusing every multi-job plan.
    """
    from applypilot.apply.launcher import derive_batch

    stored = load_staged_plan()
    if stored is None:
        raise PlanRequiredError(
            "no plan is staged — run 'applypilot apply --plan' first, then "
            "confirm with the plan hash it printed.")
    today = utc_today()
    if stored.get("day") != today:
        raise PlanStaleError(
            f"the staged plan is from {stored.get('day')!r}, not today "
            f"({today}) — plan again; tokens do not cross UTC days.",
            details={"staged_day": stored.get("day"), "today": today})
    core = _plan_core(stored.get("day"), stored.get("min_score"),
                      stored.get("cap"), stored.get("urls", []))
    if plan_hash(core) != stored.get("plan_hash"):
        raise PlanEditedError(
            "the staged plan file was edited after it was written — plan "
            "again and confirm the fresh hash.")
    if given_hash != stored.get("plan_hash"):
        raise PlanHashMismatchError(
            "--i-confirm does not match the staged plan's hash — rerun "
            "'applypilot apply --plan' and confirm the hash it prints.",
            details={"given": given_hash, "staged": stored.get("plan_hash")})
    if explicit_cap is not None and explicit_cap != stored.get("cap"):
        raise PlanCapMismatchError(
            f"--daily-cap {explicit_cap} disagrees with the staged plan's "
            f"cap {stored.get('cap')} — plan again with the cap you want.",
            details={"staged_cap": stored.get("cap"),
                     "given_cap": explicit_cap})
    planned_urls = list(stored.get("urls", []))
    planned_min = stored.get("min_score")
    # The confirm contract is "same flags as the --plan run": a selection
    # flag that disagrees with the plan describes a different batch. B3:
    # an omitted flag (None) inherits the staged value instead.
    if min_score is not None and min_score != planned_min:
        raise PlanDriftError(
            "--min-score differs from the staged plan's selection — plan "
            "again; a confirmed batch is the batch that runs.",
            details={"staged_min_score": planned_min,
                     "given_min_score": min_score})
    if limit is not None and limit != len(planned_urls):
        raise PlanDriftError(
            "--limit differs from the staged plan's batch size — plan "
            "again; a confirmed batch is the batch that runs.",
            details={"staged_batch_size": len(planned_urls),
                     "given_limit": limit})
    current_urls = [job["url"] for job in
                    derive_batch(min_score=planned_min,
                                 limit=len(planned_urls))]
    if current_urls != planned_urls:
        raise PlanDriftError(
            "the eligible job set changed since this plan was staged — "
            "plan again; a confirmed batch is the batch that runs.",
            details={"planned": planned_urls, "current": current_urls})
    return {"urls": planned_urls, "cap": stored["cap"],
            "min_score": planned_min}
