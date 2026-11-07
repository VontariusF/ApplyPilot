"""The `applypilot agent` JSON protocol — onboarding for any host coding agent.

One Typer sub-app over the existing onboarding service layer (spec
art_A71wyYS4): every verb is a thin serialization of a function the
portal already calls, over the same persisted state file and the same
lock. The protocol adds no state of its own — the portal and the
protocol are two front doors over one truth, so an interview started
here can be finished there and vice versa.

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
  placeholder. --reveal opts back in per call on `next` and `review`.
"""

from __future__ import annotations

import json
import re
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


def _resume_block(state: onboarding.InterviewState | None, reveal: bool) -> dict:
    text = None
    if config.RESUME_PATH.exists():
        text = config.RESUME_PATH.read_text(encoding="utf-8", errors="replace")
    block: dict = {
        "provided": bool(text and text.strip()),
        "chars": len(text) if text is not None else None,
        "pending": bool(state and state.pending_context),
    }
    if reveal and text is not None:
        block["text"] = text  # the only place the resume document may appear
    return block


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
