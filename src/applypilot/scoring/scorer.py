"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords_lines: list[str] = []
    reasoning_lines: list[str] = []
    # Continuation lines belong to the last marker seen — models wrap
    # KEYWORDS/REASONING prose across lines routinely, so accumulate
    # until the next marker instead of overwriting per line.
    current: list[str] | None = None

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            current = None
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords_lines = [line.replace("KEYWORDS:", "").strip()]
            current = keywords_lines
        elif line.startswith("REASONING:"):
            reasoning_lines = [line.replace("REASONING:", "").strip()]
            current = reasoning_lines
        elif current is not None:
            current.append(line)

    keywords = " ".join(part for part in keywords_lines if part)
    reasoning = "\n".join(reasoning_lines).strip() if reasoning_lines else response

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def _response_is_malformed(response: str, parsed: dict) -> bool:
    """True when the response is missing a required marker or an usable score.

    A valid response carries all three SCORE:/KEYWORDS:/REASONING: markers and
    a 1-10 score. Anything else would previously fall through as fit_score=0
    (missing markers) or the raw response text — both silently persisted.
    """
    lowered = response.lower()
    if not all(marker in lowered for marker in ("score:", "keywords:", "reasoning:")):
        return True
    return not 1 <= parsed["score"] <= 10


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title") or "?", e)
        # E: errored results get stored as the job's score_error by run_scoring
        # (fit_score stays NULL) instead of silently re-queueing forever.
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}", "error": str(e)}

    parsed = _parse_score_response(response)
    if not _response_is_malformed(response, parsed):
        return parsed

    # E: malformed responses (missing SCORE:/KEYWORDS:/REASONING:) retry once
    # with a fresh call before failing visibly, so one bad completion doesn't
    # poison the job's score state.
    log.warning(
        "Malformed score response for '%s' — missing/invalid SCORE:/KEYWORDS:/REASONING:; retrying once",
        job.get("title") or "?",
    )
    try:
        response = client.chat(messages, max_tokens=512, temperature=0.2)
    except Exception as e:
        log.error("LLM error on scoring retry for '%s': %s", job.get("title") or "?", e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error on retry: {e}", "error": str(e)}

    parsed = _parse_score_response(response)
    if not _response_is_malformed(response, parsed):
        return parsed

    error = (
        "Malformed LLM response after retry: missing or unreadable "
        f"SCORE:/KEYWORDS:/REASONING: markers. Response began: {response[:200]!r}"
    )
    log.error("Score parse FAILED for '%s': %s", job.get("title") or "?", error)
    return {"score": 0, "keywords": "", "reasoning": error, "error": error}


def _scored_by_provider() -> str:
    """The provenance label for LLM-written scores (spec art_IMViYTMU).

    The provider name the client advertises, or the generic 'llm' when
    the client carries no name (test fakes, custom client classes). Host
    protocol scores write 'host' instead; rows from before the column
    exist keep NULL.
    """
    return getattr(get_client(), "provider_name", "") or "llm"


def resume_sha256(resume_text: str) -> str:
    """Bind a score to the resume it was judged against (spec Layer 1).

    The hex digest of resume.txt's text, stamped into
    jobs.scored_resume_hash at persist time; a later mismatch (or a
    pre-binding NULL) marks the score stale for the rerank flow.
    """
    return hashlib.sha256(resume_text.encode("utf-8")).hexdigest()


def persist_score(conn, url: str, score: int, keywords: str, reasoning: str,
                  scored_by: str, when: str,
                  resume_hash: str | None = None) -> None:
    """Write one job's score — the single UPDATE shared by the LLM path
    and the host protocol (spec art_IMViYTMU: no parallel write path).

    Clears any prior score_error; `scored_by` stamps provenance ('host'
    for protocol writes, the provider name for LLM ones). The stored
    blob is `keywords\\nreasoning` — split_score_reasoning depends on
    that shape. `resume_hash` binds the score to the resume text it was
    judged against (None = unbound, treated as stale). Callers commit.
    """
    conn.execute(
        "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
        "score_error = NULL, scored_by = ?, scored_resume_hash = ? WHERE url = ?",
        (score, f"{keywords}\n{reasoning}", when, scored_by, resume_hash, url),
    )


def _record_score_errors(conn, urls: list[str], error: str) -> None:
    """Persist a scoring failure per job so the portal can show it (E).

    fit_score stays NULL, so the job remains in the pending-score queue —
    but 'scoring failed' is now visible and distinguishable from 'not
    scored yet'. A later successful score clears score_error.
    """
    for u in urls:
        conn.execute("UPDATE jobs SET score_error = ? WHERE url = ?", (error, u))
    conn.commit()


def run_scoring(limit: int = 0, rescore: bool = False, url: str | None = None) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        url: Score exactly this job URL (must have a full description),
            regardless of its current score state.

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    conn = get_connection()

    if url:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE url = ? AND full_description IS NOT NULL", (url,)
        ).fetchall()
        if not jobs:
            # Targeted rescore of a missing or description-less job: fail
            # visibly (exit code on the CLI) but persist nothing. An
            # undescribed row's truthful state is pending detail, not
            # "scoring failed" — stamping score_error here used to put a
            # score_failed count on a row outside every described-row
            # bucket and break the S2 identity (F1).
            exists = conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone()
            if exists:
                log.error(
                    "Rescore target %s has no full description yet — "
                    "enrich the job first, then rescore.", url,
                )
            else:
                log.error("Rescore target not found: %s", url)
            return {"scored": 0, "errors": 1, "elapsed": 0.0, "distribution": []}
    elif rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    try:
        resume_text = RESUME_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        # E: a missing resume is a failed state on every targeted job — not a
        # silent skip and not a crash. It stays visible until fixed and a
        # successful score clears it.
        error = (
            f"Resume file not found at {RESUME_PATH}. "
            "Run 'applypilot init' to add your resume, then rescore."
        )
        _record_score_errors(conn, [job["url"] for job in jobs], error)
        log.error("Scoring stopped: %s", error)
        return {"scored": 0, "errors": len(jobs), "elapsed": 0.0, "distribution": []}

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result.get("error"):
            errors += 1
            # E: errored jobs keep fit_score NULL so they reappear in the
            # pending-score queue; the failure is also stored as score_error
            # so the portal can distinguish it from 'not scored yet'.
            log.error(
                "Score FAILED for '%s' (%s): %s",
                job.get("title") or "?", job["url"], result["error"],
            )
            results.append(result)
            continue

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], (job.get("title") or "?")[:60],
        )

    # Write scores to DB — successful scores clear any prior score_error (E)
    # and stamp provenance (spec art_IMViYTMU): the provider name for
    # LLM-written scores, 'host' for protocol-written ones; errored jobs
    # get the failure recorded instead.
    now = datetime.now(timezone.utc).isoformat()
    scored_by = _scored_by_provider()
    resume_hash = resume_sha256(resume_text)
    for r in results:
        if r.get("error"):
            conn.execute(
                "UPDATE jobs SET score_error = ? WHERE url = ?", (r["error"], r["url"])
            )
            continue
        persist_score(conn, r["url"], r["score"], r["keywords"], r["reasoning"],
                      scored_by, now, resume_hash=resume_hash)
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
