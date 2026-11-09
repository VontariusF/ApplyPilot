"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import json
import logging
import re
import time
from pathlib import Path

from kestrel.config import COVER_LETTER_DIR, RESUME_PATH, history_enabled, load_profile
from kestrel.database import (
    KIND_COVER_LETTER,
    claim_document_version,
    get_connection,
    get_jobs_by_stage,
)
from kestrel.llm import get_client, is_infra_error
from kestrel.scoring import docs
from kestrel.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

STRUCTURE: 3 short paragraphs. Under 250 words. Every sentence must earn its place.

PARAGRAPH 1 (2-3 sentences): Open with a specific thing YOU built that solves THEIR problem. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 (3-4 sentences): Pick 2 achievements from the resume that are MOST relevant to THIS job. Use numbers. Frame as solving their problem, not listing your accomplishments.{projects_hint}{metrics_hint}

PARAGRAPH 3 (1-2 sentences): One specific thing about the company from the job description (a product, a technical challenge, a team structure). Then close. "Happy to walk through any of this in more detail." or "Let's discuss." Nothing else.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.

Sign off: just "{sign_off_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    dear_idx = text.lower().find("dear")
    if dear_idx > 0:
        return text[dear_idx:]
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_client()
    cl_prompt_base = _build_cover_letter_prompt(profile)

    for attempt in range(max_retries + 1):
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=1024, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
        letter = _strip_preamble(letter)  # remove any "Here is the letter:" prefix

        validation = validate_cover_letter(letter, mode=validation_mode)
        if validation["passed"]:
            return letter

        avoid_notes.extend(validation["errors"])
        # Warnings never block — only hard errors trigger a retry
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    return letter  # last attempt even if failed


# ── Persistence (shared by the LLM loop and the host verb) ───────────────

def persist_cover_letter(conn, job: dict, letter: str,
                         validation_mode: str = "normal") -> tuple[dict, dict]:
    """Write the cover letter and stamp the jobs row — the single
    persistence path for a validated letter (spec art_IMViYTMU: the host
    verb writes through the same files and UPDATE as the LLM loop).

    Claim-first (audit F2): the letter is validated (pure function), then
    the version row and the jobs pointer are reserved inside one BEGIN
    IMMEDIATE transaction BEFORE the file is written — the loser of a
    concurrent-writer race gets DocumentClaimConflict and writes nothing.
    Writes the letter file to the claimed path, generates the PDF
    best-effort. Returns (result dict, final validation).
    """
    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    prefix = docs.output_prefix(job)

    # Record the validation outcome of the letter actually returned —
    # it may be the last retry, which can have failed validation.
    final_validation = validate_cover_letter(letter, mode=validation_mode)
    validation_status = "approved" if final_validation["passed"] else "failed_validation"

    # History on (D1/D2): every generation is a new version at a
    # _v{N} stem reserved by the claim (the pre-history legacy file is
    # backfilled as version 1 inside the claim transaction and never
    # touched). History off: the exact pre-history overwrite behavior
    # at the bare prefix.
    history_on = history_enabled()

    def _path_for(version: int) -> str:
        if history_on:
            return str(COVER_LETTER_DIR / f"{docs.versioned_stem(prefix, version)}_CL.txt")
        return str(COVER_LETTER_DIR / f"{prefix}_CL.txt")

    claim = claim_document_version(
        conn, job["url"], KIND_COVER_LETTER,
        path_for=_path_for,
        legacy_path=COVER_LETTER_DIR / f"{prefix}_CL.txt" if history_on else None,
        validation_status=validation_status,
        pointer_column="cover_letter_path",
        stamp_column="cover_letter_at",
        attempts_column="cover_attempts",
        history_on=history_on,
    )

    cl_path = Path(claim.file_path)
    cl_path.write_text(letter, encoding="utf-8")

    # Generate PDF (best-effort)
    pdf_path = None
    try:
        from kestrel.scoring.pdf import convert_to_pdf
        pdf_path = str(convert_to_pdf(cl_path))
    except Exception:
        log.debug("PDF generation failed for %s", cl_path, exc_info=True)

    result = {
        "url": job["url"],
        "path": str(cl_path),
        "pdf_path": pdf_path,
        "title": job["title"],
        "site": job["site"],
        "status": validation_status,
        "version_id": claim.version_id,
    }
    return result, final_validation


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 7, limit: int = 20,
                      validation_mode: str = "normal",
                      url: str | None = None) -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        url:             Exact job URL — generate for this job only
                         (single-job runs).

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    # Fetch jobs that have tailored resumes but no cover letter yet — the
    # same queue condition the host cover verb drains (STAGE_CONDITIONS
    # "pending_cover"), so both front doors see identical eligibility.
    jobs = get_jobs_by_stage(conn=conn, stage="pending_cover",
                             min_score=min_score, limit=limit, url=url)

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    for job in jobs:
        completed += 1
        try:
            letter = generate_cover_letter(resume_text, job, profile,
                                           validation_mode=validation_mode)

            # One persistence path: files, version row, jobs pointer, and
            # the attempt counter — shared with the host cover verb.
            result, _final_validation = persist_cover_letter(
                conn, job, letter, validation_mode=validation_mode)
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] | %.1f jobs/min | %s",
                completed, len(jobs), rate * 60, (result.get("title") or "untitled")[:40],
            )
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "pdf_path": None, "error": str(e), "version_id": None,
                # P1-1: infra failures (provider down/timeout/5xx) must not
                # burn the job's attempt budget — classified here, exempted
                # in the persist loop below.
                "error_class": "infra" if is_infra_error(e) else "content",
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), (job.get("title") or "untitled")[:40], e)

    # Persist to DB: increment attempt counter for content-level failures.
    # Successes were persisted inline by persist_cover_letter (path,
    # pointer, attempt counter, version row). P1-1: infra-classified errors
    # (provider down, timed out, 5xx) do not burn the attempt budget — the
    # job stays fully retryable when the provider recovers, instead of one
    # outage exhausting every queued job's attempts.
    saved = sum(1 for r in results if r.get("path"))
    for r in results:
        if r.get("path"):
            continue  # already stamped by persist_cover_letter
        if r.get("error_class") == "infra":
            continue  # provider-down: stay fully retryable, no attempt burned
        conn.execute(
            "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
            (r["url"],),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
