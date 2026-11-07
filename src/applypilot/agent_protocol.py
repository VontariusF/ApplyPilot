"""The `applypilot agent` JSON protocol — onboarding and host-driven
pipeline stages for any host coding agent.

One Typer sub-app over existing service layers (onboarding, spec
art_A71wyYS4; pipeline scoring, spec art_IMViYTMU): every verb is a thin
serialization of a function the portal or CLI already calls, over the
same persisted state. The protocol adds no state of its own — the portal
and the protocol are two front doors over one truth, so an interview
started here can be finished there and vice versa.

Wire contract (the entire stdout surface):

- Every successful verb prints ONE pure-JSON document — the
  authoritative next payload — and exits 0. The host agent never
  re-derives the agenda: the payload says what phase we are in and what
  to do about it.
- Every failure prints {"error": {"code", "message", "details"}} and
  exits nonzero. No prose, no Rich markup, ever — the host agent parses
  stdout alone.
- Long text is redacted by default, because protocol output lands in
  the host chat's session logs (a second logging domain beyond the
  portal): the resume document travels as metadata only, the review
  payload's resume_facts section is withheld, and fenced code blocks in
  the last agent reply (the draft-extraction channel — its facts
  already surface as prefill chips and review fields) are replaced by a
  placeholder. --reveal opts back in per call on `next`, `review`, and
  every pipeline verb.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import typer

from applypilot import config, onboarding

agent_app = typer.Typer(
    name="agent",
    no_args_is_help=True,
    help="Machine-readable onboarding protocol for host coding agents.",
)


# ---------------------------------------------------------------------------
# Wire contract: emission, the error envelope, and failure classification
# ---------------------------------------------------------------------------

def _prepare() -> None:
    """Env + dirs — the slice of cli._bootstrap onboarding needs (it never
    touches the database, so no init_db here)."""
    from applypilot.config import ensure_dirs, load_env

    load_env()
    ensure_dirs()


def _prepare_db() -> None:
    """_prepare + init_db — the pipeline verbs (job-next/score) read and
    write the store, unlike the onboarding verbs, so the schema must be
    current (the scored_by migration rides along here too)."""
    _prepare()
    from applypilot.database import init_db

    init_db()


def _emit(payload: dict) -> None:
    """Write one pure-JSON document to stdout — nothing else ever prints."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _fail(code: str, message: str, details: dict | None = None) -> None:
    """The error envelope + nonzero exit — the only failure shape."""
    _emit({"error": {"code": code, "message": message, "details": details}})
    raise typer.Exit(code=1)


# Service-layer messages are stable user-facing strings (the portal chat
# shows the same text), so each maps to a protocol code the host agent can
# branch on. First match wins; anything unmatched stays onboarding_error.
_ERROR_CODE_RULES: tuple[tuple[str, str], ...] = (
    ("No interview backend", "provider_setup"),
    ("No LLM provider is configured", "provider_setup"),
    ("not signed in", "provider_setup"),
    ("interview backend", "provider_setup"),
    # Cross-surface contention on the single-session lock: another surface
    # holds the state lock mid-turn (spec art_A71wyYS4 — first wins,
    # second gets busy). Retrying once the other turn finishes is safe.
    ("already running on another surface", "busy"),
    ("interview is not active", "not_active"),
    ("nothing to edit", "nothing_to_edit"),
    ("no profile draft to confirm", "nothing_to_confirm"),
    ("no profile to review", "nothing_to_review"),
    ("no full name", "missing_full_name"),
    ("unknown question", "unknown_question"),
    ("isn't open right now", "question_not_open"),
    ("already answered", "already_answered"),
    ("one of the listed choices", "invalid_choice"),
    ("no resume content", "empty_resume"),
    ("nothing from your resume", "nothing_to_clear"),
    ("empty message", "empty_message"),
)


def _classify(message: str) -> str:
    for needle, code in _ERROR_CODE_RULES:
        if needle in message:
            return code
    return "onboarding_error"


def _verb(body: Callable[[], dict]) -> None:
    """Run one verb body; every failure exits through the JSON envelope.

    The protocol has no unhandled path: a service rejection classifies
    into a protocol code, a backend failure (RuntimeError — the claude
    subprocess or the LLM provider) is a retryable backend_error, and
    anything else is an internal_error. All on stdout, all nonzero-exit.
    """
    try:
        _emit(body())
    except typer.Exit:
        raise
    except onboarding.OnboardingError as exc:
        _fail(_classify(str(exc)), str(exc))
    except RuntimeError as exc:
        _fail("backend_error", f"{type(exc).__name__}: {exc}",
              {"exception_type": type(exc).__name__})
    except Exception as exc:  # the contract: every failure is JSON, never a traceback
        _fail("internal_error", f"{type(exc).__name__}: {exc}",
              {"exception_type": type(exc).__name__})


# ---------------------------------------------------------------------------
# The next payload (one authoritative shape, spec art_A71wyYS4)
# ---------------------------------------------------------------------------

_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_REDACTED_DRAFT = "[draft extracted — fenced text redacted; rerun with --reveal]"


def _last_agent_reply(state: onboarding.InterviewState | None, reveal: bool) -> str | None:
    if state is None:
        return None
    reply = next((m.get("content") for m in reversed(state.messages)
                  if m.get("role") == "assistant"), None)
    if reply is None or reveal:
        return reply
    # Fenced blocks are the machine channel (the profile draft): their
    # facts surface as prefill chips and review fields, and the fenced
    # text itself is the bulk resume-derived payload the default view
    # redacts. The prose around it — the agent's actual voice — passes.
    return _FENCED_BLOCK.sub(_REDACTED_DRAFT, reply)


def _choice_rows(choices) -> list[dict]:
    # value/label objects, not bare labels: salary_band's value is the raw
    # number and work_authorized's is "yes" — the strings the answer verb
    # must echo back. The label is the human phrasing.
    return [{"value": c.value, "label": c.label} for c in choices]


def _current_question_payload(state: onboarding.InterviewState | None) -> dict | None:
    if state is None or state.phase != onboarding.INTERVIEW:
        return None
    spec = onboarding.current_question(state)
    if spec is None:
        return None
    resolved = onboarding.resolved_choices(spec, onboarding.effective_answers(state))
    chips = {row["spec"].id: row["value"] for row in onboarding.prefilled_answers(state)}
    return {
        "id": spec.id,
        "prompt": spec.prompt,
        "choices": _choice_rows(resolved),
        "prefilled": chips.get(spec.id),
        "allow_free_text": spec.allow_free_text,
        "free_text_kind": spec.free_text_kind,
    }


def _chips_payload(state: onboarding.InterviewState | None) -> list[dict]:
    if state is None:
        return []
    return [
        {
            "question_id": row["spec"].id,
            "prompt": row["spec"].prompt,
            "value": row["value"],
            "choices": _choice_rows(row["spec"].choices),
            "allow_free_text": row["spec"].allow_free_text,
        }
        for row in onboarding.prefilled_answers(state)
    ]


def _review_fields_payload(state: onboarding.InterviewState) -> list[dict]:
    rows = []
    for row in onboarding.review_fields(state):
        spec = row["spec"]
        rows.append({
            "id": spec.id,
            "prompt": spec.prompt,
            "value": row["value"],
            "choices": _choice_rows(spec.choices),
            "allow_free_text": spec.allow_free_text,
            "from_resume": row["from_resume"],
            "needs_answer": row["needs_answer"],
        })
    return rows


def _resume_text() -> str | None:
    """The resume document, or None when none is on disk."""
    if not config.RESUME_PATH.exists():
        return None
    return config.RESUME_PATH.read_text(encoding="utf-8", errors="replace")


def _resume_block(state: onboarding.InterviewState | None, reveal: bool) -> dict:
    text = _resume_text()
    block: dict = {
        "provided": bool(text and text.strip()),
        "chars": len(text) if text is not None else None,
        "pending": bool(state and state.pending_context),
    }
    if reveal and text is not None:
        block["text"] = text  # the only place the resume document may appear
    return block


def _scoring_resume_block(reveal: bool) -> dict:
    """Resume metadata for the pipeline payloads — the text itself only
    behind --reveal (stdout lands in host chat logs)."""
    text = _resume_text()
    block: dict = {
        "provided": bool(text and text.strip()),
        "chars": len(text) if text is not None else None,
    }
    if reveal and text is not None:
        block["text"] = text  # the only place the resume document may appear
    return block


def _resume_facts() -> dict | None:
    """The profile's resume_facts — --reveal only (personal data).

    None when the store has no profile yet: honestly absent, not an error.
    """
    try:
        return config.load_profile().get("resume_facts")
    except FileNotFoundError:
        return None


def _needs_resume(state: onboarding.InterviewState | None) -> bool:
    # True exactly when the protocol can act on one: intake with no resume
    # queued. (With no state at all, `start` must come first — a resume
    # upload has nothing to attach to yet.)
    return (state is not None and state.phase == onboarding.INTAKE
            and not state.pending_context)


def _progress(state: onboarding.InterviewState | None) -> dict:
    if state is None:
        return {"answered": 0, "remaining": 0}
    answered = sum(1 for spec in onboarding.QUESTION_SPECS if spec.id in state.answers)
    return {"answered": answered, "remaining": len(onboarding.remaining_questions(state))}


def _next_payload(state: onboarding.InterviewState | None, reveal: bool = False) -> dict:
    """The one authoritative payload every verb returns."""
    phase = state.phase if state is not None else onboarding.GATE
    payload = {
        "phase": phase,
        "mode": state.mode if state is not None else None,
        # The journey's backend: "host" when the driver supplies the
        # intelligence (zero LLM calls), "claude"/"llm" otherwise.
        "backend": state.backend if state is not None else None,
        "current_question": _current_question_payload(state),
        "prefill_chips": _chips_payload(state),
        "last_agent_reply": _last_agent_reply(state, reveal),
        "needs": {
            "resume": _needs_resume(state),
            "confirm": phase == onboarding.REVIEW,
            "provider_setup": not onboarding.readiness()["can_start_any"],
        },
        "progress": _progress(state),
        "resume": _resume_block(state, reveal),
    }
    if state is not None and phase == onboarding.REVIEW:
        payload["review_fields"] = _review_fields_payload(state)
        profile, _meta = onboarding.normalize_profile(state.profile_draft or {}, state.answers)
        payload["missing_review_fields"] = onboarding.missing_review_fields(profile)
        facts = profile.get("resume_facts")
        if reveal and facts:
            payload["resume_facts"] = facts
    if phase == onboarding.DONE:
        payload["written"] = {
            "profile": str(config.PROFILE_PATH),
            "profile_md": str(config.PROFILE_MD_PATH),
            "searches_yaml": str(config.SEARCH_CONFIG_PATH),
        }
    return payload


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def _start(mode: str) -> dict:
    ready = onboarding.readiness()
    if ready["can_start_any"]:
        backend = "claude" if ready["can_start_claude"] else "llm"
    else:
        # No interview backend: the driving host agent supplies the
        # intelligence instead (spec art_v7WOlPfz) — onboarding runs
        # end-to-end with zero LLM calls, free-form chat stays gated.
        backend = "host"
    existing = onboarding.load_state()
    if existing is None or existing.phase == onboarding.GATE:
        # Resume-first: intake before any agent turn; needs.resume says so.
        state = onboarding.start_intake()
    elif existing.phase == onboarding.INTAKE:
        state = onboarding.start_interview(backend)
        if (mode == onboarding.MODE_AGENT and state.phase == onboarding.INTERVIEW
                and state.mode != onboarding.MODE_AGENT):
            state = onboarding.set_agent_led(True)
    else:
        # An interview in progress resumes as-is (the portal's two-tabs
        # rule): start never wipes work, and --mode is not re-applied.
        state = existing
    return _next_payload(state)


@agent_app.command("start")
def start_cmd(
    mode: str = typer.Option(
        onboarding.MODE_CHOICES, "--mode",
        help="Session mode for a NEWLY created interview: choices or agent. "
             "Ignored when an interview already exists (switch with agent-led).",
    ),
) -> None:
    """Open intake on a fresh store; run the kickoff once a resume is queued."""
    _prepare()
    if mode not in (onboarding.MODE_CHOICES, onboarding.MODE_AGENT):
        _fail("invalid_mode",
              f"--mode must be {onboarding.MODE_CHOICES!r} or {onboarding.MODE_AGENT!r}, got {mode!r}")
    _verb(lambda: _start(mode))


@agent_app.command("next")
def next_cmd(
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text and the review payload's resume_facts "
             "(redacted by default — stdout lands in host chat logs).",
    ),
) -> None:
    """The authoritative next payload: phase, agenda, and what to do about it."""
    _prepare()
    _verb(lambda: _next_payload(onboarding.load_state(), reveal=reveal))


@agent_app.command("send")
def send_cmd(
    text: Optional[str] = typer.Option(None, "--text", help="Free-form message to the onboarding agent."),
) -> None:
    """One agent-led turn: send text; the reply rides back in the payload."""
    _prepare()
    if text is None:
        _fail("invalid_usage", "--text is required")
    _verb(lambda: _next_payload(onboarding.send_message(text)))


@agent_app.command("answer")
def answer_cmd(
    question_id: Optional[str] = typer.Option(
        None, "--question", help="current_question.id from the next payload."),
    choice: Optional[str] = typer.Option(
        None, "--choice", help="A choice VALUE from the question's choices."),
    free_text: Optional[str] = typer.Option(
        None, "--free-text", help="Typed answer, for allow_free_text questions."),
    skip: bool = typer.Option(
        False, "--skip", help="Record the question as intentionally empty (review flags it)."),
) -> None:
    """Answer the current guided question — exactly one of --choice/--free-text/--skip."""
    _prepare()
    if not question_id:
        _fail("invalid_answer", "--question is required")
    given = [flag for flag, present in (
        ("--choice", choice is not None),
        ("--free-text", free_text is not None),
        ("--skip", skip),
    ) if present]
    if len(given) != 1:
        _fail("invalid_answer", "provide exactly one of --choice, --free-text, or --skip",
              {"given": given})
    _verb(lambda: _next_payload(onboarding.record_answer(
        question_id, value=choice or "", free_text=free_text or "", skip=skip)))


def _resume_upload(path: Path) -> onboarding.InterviewState:
    if path.suffix.lower() == ".pdf":
        return onboarding.add_resume(pdf_bytes=path.read_bytes(), filename=path.name)
    return onboarding.add_resume(text=path.read_text(encoding="utf-8", errors="replace"),
                                 filename=path.name)


@agent_app.command("resume")
def resume_cmd(
    file: Optional[str] = typer.Option(
        None, "--file", help="Path to a text resume (or a text-extractable PDF)."),
) -> None:
    """Queue a resume for the next agent turn (accepted in intake or interview)."""
    _prepare()
    if not file:
        _fail("invalid_usage", "--file is required")
    path = Path(file)
    if not path.is_file():
        _fail("file_not_found", f"no readable resume file at {path}")
    _verb(lambda: _next_payload(_resume_upload(path)))


@agent_app.command("review")
def review_cmd(
    set_field: Optional[list[str]] = typer.Option(
        None, "--set", help="Review edit as field=value (repeatable; ids from review_fields)."),
    reveal: bool = typer.Option(
        False, "--reveal", help="Include resume_facts and the raw reply in the payload."),
) -> None:
    """Apply review-phase edits (phase stays REVIEW; confirm writes the files)."""
    _prepare()
    updates: dict[str, str] = {}
    for raw in set_field or []:
        field, sep, value = raw.partition("=")
        if not sep or not field.strip():
            _fail("invalid_set", f"--set expects field=value, got {raw!r}")
        field = field.strip()
        if field not in onboarding.QUESTIONS:
            _fail("unknown_field", f"unknown review field {field!r}",
                  {"valid_fields": sorted(onboarding.QUESTIONS)})
        updates[field] = value
    if not updates:
        _fail("invalid_usage", "provide at least one --set field=value")
    _verb(lambda: _next_payload(onboarding.update_answers(updates), reveal=reveal))


@agent_app.command("confirm")
def confirm_cmd() -> None:
    """Write profile.json / profile.md / searches.yaml — the only path out of REVIEW."""
    _prepare()

    def _confirm() -> dict:
        onboarding.confirm_profile()
        return _next_payload(onboarding.load_state())

    _verb(_confirm)


@agent_app.command("keep-editing")
def keep_editing_cmd() -> None:
    """Leave REVIEW back into the conversation (draft kept for context)."""
    _prepare()
    _verb(lambda: _next_payload(onboarding.keep_editing()))


@agent_app.command("redo")
def redo_cmd() -> None:
    """Wipe the interview state and start over (profile.json stays put)."""
    _prepare()
    _verb(lambda: _next_payload(onboarding.redo_interview()))


@agent_app.command("agent-led")
def agent_led_cmd(
    action: Optional[str] = typer.Argument(
        None, help='"on" hands the agenda to the agent; "off" returns to guided choices.'),
) -> None:
    """Switch the interview between agent-led chat and guided choices."""
    _prepare()
    if action not in ("on", "off"):
        _fail("invalid_action", f"expected 'on' or 'off', got {action!r}")
    _verb(lambda: _next_payload(onboarding.set_agent_led(action == "on")))


# ---------------------------------------------------------------------------
# Host-driven pipeline: scoring (spec art_IMViYTMU, T1) and the enrich,
# tailor, and cover seams (T2).
#
# The driving agent supplies the intelligence; ApplyPilot owns the queue
# contracts, validation, and persistence. No LLM call anywhere on these
# paths — and nothing here calls check_tier, the way `agent start` runs
# host onboarding with no provider configured.
# ---------------------------------------------------------------------------

# The flag validates against what is implemented so a driver gets a typed
# refusal instead of a silently wrong queue.
_STAGES = ("score", "enrich", "tailor", "cover")

# Stage -> the queue it drains (a database.STAGE_CONDITIONS key).
_STAGE_QUEUES = {
    "score": "pending_score",
    "enrich": "pending_detail",
    "tailor": "pending_tailor",
    "cover": "pending_cover",
}

# The scorer's description window: the LLM sees the first 6000 chars of
# full_description (scorer.score_job), so the host agent judges a job on
# the same window only when the excerpt matches it.
_DESCRIPTION_EXCERPT_CAP = 6000

_ENRICH_INSTRUCTIONS = (
    "Capture the job's full description text (responsibilities, "
    "requirements, every section) and, when the posting has one, the "
    "apply-button URL. Pass the description with --full-description; "
    "the verb runs the same persistence write as the enrichment "
    "fallback tier and moves the job into the score queue."
)

_COVER_REQUIREMENTS = (
    "3 short paragraphs, under 250 words. Start with 'Dear Hiring "
    "Manager,' and end with the sign-off name. No em or en dashes "
    "(auto-fixed on receipt). No meta-commentary. Validation runs in "
    "lenient mode: banned filler words are ignored, but LLM self-talk "
    "and a missing 'Dear' opening are hard errors."
)


def _pipeline_progress(conn=None) -> dict:
    """The progress block every pipeline payload carries: the counts a
    host loop needs to steer and terminate, for every stage at once.
    pending_detail and detail_errors are the stranded-jobs signals (jobs
    with no captured description never enter the score queue; they need
    enrich first, and the payload keeps that visible instead of silent)."""
    from applypilot.database import get_stats

    stats = get_stats(conn)
    return {
        "total": stats["total"],
        "with_description": stats["with_description"],
        "scored": stats["scored"],
        "unscored": stats["unscored"],
        "score_distribution": stats["score_distribution"],
        "tailored": stats["tailored"],
        "untailored_eligible": stats["untailored_eligible"],
        "tailor_exhausted": stats["tailor_exhausted"],
        "with_cover_letter": stats["with_cover_letter"],
        "cover_exhausted": stats["cover_exhausted"],
        # Stranded-jobs signals — present in every payload, every stage.
        "pending_detail": stats["pending_detail"],
        "detail_errors": stats["detail_errors"],
    }


def _job_block(job: dict) -> dict:
    """The wire shape of one pending job — the stage's inputs and nothing else."""
    desc = job.get("full_description") or ""
    return {
        "url": job["url"],
        "title": job.get("title"),
        "site": job.get("site"),
        "location": job.get("location"),
        "salary": job.get("salary"),
        "fit_score": job.get("fit_score"),
        "description_excerpt": desc[:_DESCRIPTION_EXCERPT_CAP],
        "description_chars": len(desc),
        "excerpt_truncated": len(desc) > _DESCRIPTION_EXCERPT_CAP,
    }


def _revealed_profile() -> dict:
    """The profile slice the tailor stage needs to produce validator-passing
    data: skills boundary, preserved facts, and the education level. The
    personal block is deliberately excluded — the assembler injects the
    header from the profile itself, so the driver never needs it."""
    profile = config.load_profile()
    return {
        key: profile[key]
        for key in ("skills_boundary", "resume_facts", "experience")
        if profile.get(key)
    }


def _stage_payload(stage: str, job: dict | None, reveal: bool) -> dict:
    """The authoritative payload for a pipeline stage: the next pending
    job (null when the queue is drained — the loop's termination signal),
    the stage's contract for the driver, progress, and resume metadata."""
    payload: dict = {
        "stage": stage,
        "job": _job_block(job) if job is not None else None,
        "progress": _pipeline_progress(),
        "resume": _scoring_resume_block(reveal),
    }
    if stage == "score":
        from applypilot.scoring.scorer import SCORE_PROMPT

        payload["rubric"] = SCORE_PROMPT
    elif stage == "enrich":
        payload["instructions"] = _ENRICH_INSTRUCTIONS
    elif stage == "tailor":
        from applypilot.scoring.tailor import TAILOR_DATA_TEMPLATE

        payload["data_template"] = TAILOR_DATA_TEMPLATE
        # Host mode runs the local validator only — the LLM judge is
        # skipped (lenient validation), so banned filler words are not
        # checked but fabrication and structure are. Validation failures
        # come back retryable, carrying the errors to avoid.
        payload["validation"] = {"mode": "lenient", "retryable_errors": True}
    elif stage == "cover":
        payload["requirements"] = _COVER_REQUIREMENTS
    if reveal:
        # The driver authors the materials and judges fit: the profile's
        # facts ride the payload only behind --reveal — every stage.
        facts = _resume_facts()
        if facts:
            payload["resume_facts"] = facts
        if stage == "tailor":
            try:
                payload["profile"] = _revealed_profile()
            except FileNotFoundError:
                pass  # no profile yet: the tailor verb's envelope will say so
    return payload


def _job_next(stage: str, reveal: bool) -> dict:
    from applypilot.database import get_connection, get_jobs_by_stage

    conn = get_connection()
    jobs = get_jobs_by_stage(conn=conn, stage=_STAGE_QUEUES[stage], limit=1)
    return _stage_payload(stage, jobs[0] if jobs else None, reveal)


@agent_app.command("job-next")
def job_next_cmd(
    stage: str = typer.Option(
        "score", "--stage",
        help="Which pipeline queue to drain: score, enrich, tailor, or cover.",
    ),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text and resume_facts (redacted by "
             "default — stdout lands in host chat logs).",
    ),
) -> None:
    """The next pending job for a stage, with the rubric and progress."""
    _prepare_db()
    if stage not in _STAGES:
        _fail("invalid_stage",
              f"--stage must be one of {', '.join(_STAGES)} (implemented so far)",
              {"stage": stage, "implemented": list(_STAGES)})
    _verb(lambda: _job_next(stage, reveal))


def _validated_host_score(score_raw: str, reasoning: str | None) -> int:
    """An integer 1-10 with non-empty reasoning, else the invalid_score
    envelope — the same contract the LLM parser enforces (score clamped
    and range-checked, reasoning mandatory in the stored blob)."""
    raw = str(score_raw).strip()
    if not raw.isdigit():
        _fail("invalid_score", f"--score must be an integer 1-10, got {score_raw!r}",
              {"score": score_raw})
    value = int(raw)
    if not 1 <= value <= 10:
        _fail("invalid_score", f"--score must be 1-10 (the rubric's scale), got {value}",
              {"score": score_raw})
    if not (reasoning and reasoning.strip()):
        _fail("invalid_score",
              "--reasoning is required — an unjustified score is not persisted",
              {"score": score_raw})
    return value


def _host_score(url: str, score_raw: str, keywords: str | None,
                reasoning: str | None, rescore: bool, reveal: bool) -> dict:
    """Validate and persist one host score, then serve the next payload.

    The write goes through scorer.persist_score — the same UPDATE the
    LLM path uses (spec: no parallel schema for host data) — stamped
    scored_by='host'. A failed verb never writes: validation errors are
    envelopes only.
    """
    from applypilot.database import get_connection, get_jobs_by_stage
    from applypilot.scoring.scorer import persist_score

    score = _validated_host_score(score_raw, reasoning)

    conn = get_connection()
    row = conn.execute(
        "SELECT fit_score, full_description FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is None or not row["full_description"]:
        _fail("not_scoreable",
              f"no scoreable job at {url} — it must exist and hold a captured "
              "full_description (drain pending_detail with enrich first)",
              {"url": url})
    if row["fit_score"] is not None and not rescore:
        _fail("already_scored",
              f"{url} already carries a fit score — pass --rescore to overwrite it",
              {"url": url, "fit_score": row["fit_score"]})

    persist_score(
        conn, url, score,
        keywords=(keywords or "").strip(),
        reasoning=(reasoning or "").strip(),
        scored_by="host",
        when=datetime.now(timezone.utc).isoformat(),
    )
    conn.commit()

    jobs = get_jobs_by_stage(conn=conn, stage=_STAGE_QUEUES["score"], limit=1)
    return _stage_payload("score", jobs[0] if jobs else None, reveal)


@agent_app.command("score")
def score_cmd(
    url: Optional[str] = typer.Option(
        None, "--url", help="Job URL from the job-next payload."),
    score: Optional[str] = typer.Option(
        None, "--score", help="Fit score, integer 1-10 (the rubric rides the job-next payload)."),
    keywords: Optional[str] = typer.Option(
        None, "--keywords", help="Comma-separated ATS keywords matched from the description."),
    reasoning: Optional[str] = typer.Option(
        None, "--reasoning", help="2-3 sentences justifying the score. Required."),
    rescore: bool = typer.Option(
        False, "--rescore", help="Overwrite an existing score (LLM-path parity)."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text and resume_facts in the returned next payload."),
) -> None:
    """Persist a host score for one job; the payload serves the next pending job."""
    _prepare_db()
    if not url:
        _fail("invalid_usage", "--url is required")
    if score is None:
        _fail("invalid_usage", "--score is required")
    _verb(lambda: _host_score(url, score, keywords, reasoning, rescore, reveal))


def _load_job(conn, url: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    return dict(zip(row.keys(), row))


def _load_profile_or_fail() -> dict:
    try:
        return config.load_profile()
    except FileNotFoundError:
        _fail("no_profile",
              "no profile.json — run the onboarding protocol (`agent start`) first")


def _written_block(result: dict) -> dict:
    return {
        "path": result["path"],
        "pdf_path": result["pdf_path"],
        "version_id": result["version_id"],
    }


# ---------------------------------------------------------------------------
# Enrich (the extract_with_llm fallback tier, host-driven)
# ---------------------------------------------------------------------------

def _host_enrich(url: str, full_description: str | None,
                 application_url: str | None, reveal: bool) -> dict:
    """Persist one description capture, then serve the next enrich payload.

    The write goes through detail.persist_description — the same UPDATE the
    scrape loop runs for a fallback-tier capture (spec: no parallel schema
    for host data) — so the job enters the score queue exactly as if the
    LLM tier had succeeded.
    """
    from applypilot.database import get_connection, get_jobs_by_stage
    from applypilot.enrichment.detail import persist_description

    desc = (full_description or "").strip()
    if not desc:
        _fail("invalid_description",
              "--full-description is required — an empty capture never "
              "moves a job into the score queue")

    conn = get_connection()
    job = _load_job(conn, url)
    if job is None:
        _fail("unknown_job", f"no job at {url}", {"url": url})
    if job["full_description"]:
        _fail("already_enriched",
              f"{url} already holds a captured full_description — "
              "re-capture is not a protocol verb",
              {"url": url})

    persist_description(conn, url, desc, application_url,
                        job.get("title") or "")

    jobs = get_jobs_by_stage(conn=conn, stage=_STAGE_QUEUES["enrich"], limit=1)
    return _stage_payload("enrich", jobs[0] if jobs else None, reveal)


@agent_app.command("enrich")
def enrich_cmd(
    url: Optional[str] = typer.Option(
        None, "--url", help="Job URL from the job-next --stage enrich payload."),
    full_description: Optional[str] = typer.Option(
        None, "--full-description",
        help="The full description text captured from the posting page."),
    application_url: Optional[str] = typer.Option(
        None, "--application-url",
        help="The apply-button URL, when the posting has one."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text and resume_facts in the returned next payload."),
) -> None:
    """Persist a captured description for one stranded job; it enters the score queue."""
    _prepare_db()
    if not url:
        _fail("invalid_usage", "--url is required")
    _verb(lambda: _host_enrich(url, full_description, application_url, reveal))


# ---------------------------------------------------------------------------
# Tailor (local validator + assembler; the LLM judge is skipped)
# ---------------------------------------------------------------------------

def _host_tailor(url: str, data_raw: str, reveal: bool) -> dict:
    """Validate, assemble, and persist one host-tailored resume, then serve
    the next tailor payload.

    Host mode runs the local validator in lenient mode and skips the LLM
    judge (the `run --validation lenient` contract): structure, fabrication,
    preserved companies/school, and LLM self-talk are enforced; banned
    filler words are not. A failed validation persists nothing and returns
    a retryable envelope carrying the validator's errors as avoid-notes —
    the tailor.py retry-loop vocabulary, one loop level out.
    """
    from applypilot.database import get_connection, get_jobs_by_stage
    from applypilot.scoring.tailor import (
        assemble_resume_text,
        extract_json,
        persist_tailored,
    )
    from applypilot.scoring.validator import validate_json_fields

    profile = _load_profile_or_fail()

    try:
        data = extract_json(data_raw)
    except ValueError:
        _fail("invalid_resume_data",
              "--data is not the tailored-resume JSON object — return to "
              "the data_template shape and retry",
              {"errors": ["No valid JSON found in --data"], "retryable": True})

    conn = get_connection()
    job = _load_job(conn, url)
    if job is None:
        _fail("unknown_job", f"no job at {url}", {"url": url})
    if not job["full_description"] or job["fit_score"] is None:
        _fail("not_tailorable",
              f"{url} must be scored with a captured description before "
              "tailoring (drain pending_detail with enrich, then score)",
              {"url": url})
    if job["tailored_resume_path"]:
        _fail("already_tailored",
              f"{url} already carries a tailored resume",
              {"url": url, "path": job["tailored_resume_path"]})

    validation = validate_json_fields(data, profile, mode="lenient")
    if not validation["passed"]:
        _fail("invalid_resume_data",
              "the tailored resume data failed validation — fix the errors "
              "and retry (nothing was persisted)",
              {"errors": validation["errors"],
               "warnings": validation["warnings"],
               "avoid_notes": validation["errors"],
               "retryable": True})

    # Same report shape the LLM loop writes to _REPORT.json, with the
    # judge honestly skipped rather than absent.
    report = {
        "attempts": 1,
        "validator": validation,
        "judge": {"verdict": "SKIPPED", "passed": True, "issues": "none"},
        "status": "approved",
        "validation_mode": "host",
    }
    result = persist_tailored(conn, job, assemble_resume_text(data, profile), report)

    jobs = get_jobs_by_stage(conn=conn, stage=_STAGE_QUEUES["tailor"], limit=1)
    payload = _stage_payload("tailor", jobs[0] if jobs else None, reveal)
    payload["written"] = _written_block(result)
    return payload


@agent_app.command("tailor")
def tailor_cmd(
    url: Optional[str] = typer.Option(
        None, "--url", help="Job URL from the job-next --stage tailor payload."),
    data: Optional[str] = typer.Option(
        None, "--data", help="The tailored resume as a JSON object (the "
        "data_template shape; validated leniently, judge skipped)."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text, resume_facts, and the profile slice "
             "the validator checks against in the returned next payload."),
) -> None:
    """Persist a host-tailored resume for one scored job."""
    _prepare_db()
    if not url:
        _fail("invalid_usage", "--url is required")
    if not data:
        _fail("invalid_usage", "--data is required")
    _verb(lambda: _host_tailor(url, data, reveal))


# ---------------------------------------------------------------------------
# Cover letter (local validate_cover_letter; no LLM anywhere)
# ---------------------------------------------------------------------------

def _host_cover(url: str, text: str, reveal: bool) -> dict:
    """Sanitize, validate, and persist one host cover letter, then serve
    the next cover payload. sanitize_text runs first — the same auto-fixes
    (em dashes, smart quotes) the LLM path applies before validating."""
    from applypilot.database import get_connection, get_jobs_by_stage
    from applypilot.scoring.cover_letter import persist_cover_letter
    from applypilot.scoring.validator import sanitize_text, validate_cover_letter

    letter = sanitize_text(text)
    validation = validate_cover_letter(letter, mode="lenient")
    if not validation["passed"]:
        _fail("invalid_cover_letter",
              "the cover letter failed validation — fix the errors and "
              "retry (nothing was persisted)",
              {"errors": validation["errors"],
               "warnings": validation["warnings"],
               "avoid_notes": validation["errors"],
               "retryable": True})

    conn = get_connection()
    job = _load_job(conn, url)
    if job is None:
        _fail("unknown_job", f"no job at {url}", {"url": url})
    if not job["full_description"] or job["fit_score"] is None:
        _fail("not_coverable",
              f"{url} must be scored with a captured description before a "
              "cover letter is written",
              {"url": url})
    if not job["tailored_resume_path"]:
        _fail("not_coverable",
              f"{url} has no tailored resume — cover letters attach to "
              "tailored materials (run the tailor stage first)",
              {"url": url})
    if job["cover_letter_path"]:
        _fail("already_covered",
              f"{url} already carries a cover letter",
              {"url": url, "path": job["cover_letter_path"]})

    result, _final = persist_cover_letter(conn, job, letter,
                                          validation_mode="lenient")

    jobs = get_jobs_by_stage(conn=conn, stage=_STAGE_QUEUES["cover"], limit=1)
    payload = _stage_payload("cover", jobs[0] if jobs else None, reveal)
    payload["written"] = _written_block(result)
    return payload


@agent_app.command("cover")
def cover_cmd(
    url: Optional[str] = typer.Option(
        None, "--url", help="Job URL from the job-next --stage cover payload."),
    text: Optional[str] = typer.Option(
        None, "--text", help="The cover letter text (starts with 'Dear "
        "Hiring Manager,'; validated leniently)."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text and resume_facts in the returned next payload."),
) -> None:
    """Persist a host cover letter for one tailored job."""
    _prepare_db()
    if not url:
        _fail("invalid_usage", "--url is required")
    if not text:
        _fail("invalid_usage", "--text is required")
    _verb(lambda: _host_cover(url, text, reveal))
