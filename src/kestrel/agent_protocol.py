# SPDX-License-Identifier: AGPL-3.0-only
"""The `kestrel agent` JSON protocol — onboarding and host-driven
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
- The redaction line, named (audit F11): resume_facts — the resume's
  verbatim extractions — are withheld unless --reveal. REVIEW-phase
  review_fields pass in the clear by design: by that phase they are
  user-curated answers (edited in the portal or confirmed by the
  driver), not raw resume text, so they are log-safe while the facts
  they were extracted from stay gated.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn, Optional

import typer
from typer._click.exceptions import NoArgsIsHelpError  # typer 0.28 vendors click here; no public re-export
from typer.core import TyperGroup  # built on the vendored click

from kestrel import config, onboarding
from kestrel.database import DocumentClaimConflict, TERMINAL_APPLY_STATUSES
from kestrel.scoring.validator import COVER_WORD_HARD_CAP, COVER_WORD_TARGET

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
    from kestrel.config import ensure_dirs, load_env

    load_env()
    ensure_dirs()
    _keep_logs_off_stdout()


def _keep_logs_off_stdout() -> None:
    """Re-assert the wire contract against stream drift (dogfood finding 5).

    Verb bodies log operationally — PDF generation, discovery, the
    scheduler probe — through the shared logging root, which cli.py's
    basicConfig sends to stderr (logging's default stream). Nothing may
    share stdout with the JSON payload, so any root handler whose stream
    is stdout is re-pointed at stderr here, on every verb invocation.
    """
    for handler in logging.getLogger().handlers:
        if getattr(handler, "stream", None) is sys.stdout:
            handler.stream = sys.stderr


def _prepare_db() -> None:
    """_prepare + init_db — the pipeline verbs (job-next/score) read and
    write the store, unlike the onboarding verbs, so the schema must be
    current (the scored_by migration rides along here too)."""
    _prepare()
    from kestrel.database import init_db

    init_db()


def _emit(payload: dict) -> None:
    """Write one pure-JSON document to stdout — nothing else ever prints."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _fail(code: str, message: str, details: dict | None = None) -> None:
    """The error envelope + nonzero exit — the only failure shape."""
    _emit({"error": {"code": code, "message": message, "details": details}})
    raise typer.Exit(code=1)


class _ProtocolUsageGroup(TyperGroup):
    """Keep click/typer usage errors inside the JSON wire contract (audit F9).

    Usage errors — unknown option, missing option value, unknown command —
    are raised by the argument parser before any verb body runs, so _verb
    never sees them: they used to render Rich-boxed usage prose with
    nothing on stdout, the one wire-contract violation reachable through
    pure user error. Converting here fixes every verb at once.

    Typer 0.28 vendors its click under typer._click, and TyperGroup is
    built on it; the exception types have no public re-export, so the
    single private-module import is the NoArgsIsHelpError pass-through and
    everything else is caught as the public typer.exceptions.TyperException
    base (MissingParameter, NoSuchOption, UsageError all subclass it).

    A nested sub-app never runs its own main() — the parent group's main
    owns the process — so the conversion sits on make_context (group-level
    parsing) and invoke (leaf parsing + command resolution), and exits via
    SystemExit rather than typer.Exit: nothing above this layer converts a
    typer.Exit raised outside a verb body.

    Bare invocation keeps click's no_args_is_help help page (exit 2): a
    documented affordance for the human running `kestrel agent`, not a
    host path — the protocol's host always passes a verb.
    """

    def make_context(self, info_name, args, parent=None, **extra):
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except NoArgsIsHelpError:
            raise
        except typer.exceptions.TyperException as exc:
            _usage_fail(exc.format_message())

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except NoArgsIsHelpError:
            raise
        except typer.exceptions.TyperException as exc:
            _usage_fail(exc.format_message())


def _usage_fail(message: str) -> NoReturn:
    """Emit the invalid_usage envelope and exit 1 (audit F9).

    Runs outside any verb body — the parser raised before the callback —
    so this emits directly and raises SystemExit; _fail's typer.Exit has
    no handler above a nested group.
    """
    _emit({"error": {"code": "invalid_usage", "message": message, "details": None}})
    raise SystemExit(1)


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
    # Corrupt persisted interview state (audit F1): the file exists but
    # cannot be trusted — never silently treated as a fresh store.
    # Retrying the same verb cannot help; `agent redo` clears it.
    ("onboarding state file is corrupt", "state_corrupt"),
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
    except DocumentClaimConflict as exc:
        # A lost document-claim race (audit F2) is retryable by design —
        # before the RuntimeError clause would file it under backend_error.
        _fail("conflict", str(exc), {"retryable": True})
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
        # The wizard's agent-facing clarification ("bootcamps and
        # certificates count too") — context the host conductor reads
        # where the portal shows its ask-the-agent card (W0).
        "hint": spec.agent_hint,
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


def _provider_setup_needed(state: onboarding.InterviewState | None,
                           ready: dict) -> bool:
    """True only when the journey is blocked until a backend is set up.

    Host-driven journeys (backend "host") never are: the driving agent is
    the interview's intelligence, and a fresh or unstarted store defaults
    to one — `start`'s ambient choice runs host when no claude/provider
    backend is ready (W0). True keeps its one honest meaning: a
    claude/llm interview whose backends have all gone away (claude signed
    out, provider removed) — the chat channel is dead until setup
    returns, so the driver should surface the setup step.
    """
    if ready["can_start_any"]:
        return False
    return state is not None and state.backend in ("claude", "llm")


def _next_payload(state: onboarding.InterviewState | None, reveal: bool = False) -> dict:
    """The one authoritative payload every verb returns."""
    phase = state.phase if state is not None else onboarding.GATE
    ready = onboarding.readiness()
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
            "provider_setup": _provider_setup_needed(state, ready),
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

def _start(mode: str, backend_override: str | None = None) -> dict:
    ready = onboarding.readiness()
    if backend_override == "host":
        # F15: the driver's explicit zero-LLM request — deterministic and
        # cost-free even on a store with providers configured. start_intake
        # is backend-free already; start_interview("host") skips the
        # readiness gate and runs no kickoff turn.
        backend = "host"
    elif ready["can_start_any"]:
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
        # rule): start never wipes work, and neither --mode nor --backend
        # is re-applied.
        state = existing
    return _next_payload(state)


@agent_app.command("start")
def start_cmd(
    mode: str = typer.Option(
        onboarding.MODE_CHOICES, "--mode",
        help="Session mode for a NEWLY created interview: choices or agent. "
             "Ignored when an interview already exists (switch with agent-led).",
    ),
    backend: Optional[str] = typer.Option(
        None, "--backend",
        help="Force the interview backend for a NEWLY started interview. "
             "The only supported value is 'host' (alias of record for the "
             "zero-LLM journey): the driving agent supplies the intelligence, "
             "no kickoff turn, no provider calls — deterministic and cost-free "
             "even on a store with providers configured. Omit for the ambient "
             "choice (claude/llm when a provider is ready, host otherwise). "
             "An interview in progress resumes as-is either way.",
    ),
) -> None:
    """Open intake on a fresh store; run the kickoff once a resume is queued."""
    _prepare()
    if mode not in (onboarding.MODE_CHOICES, onboarding.MODE_AGENT):
        _fail("invalid_mode",
              f"--mode must be {onboarding.MODE_CHOICES!r} or {onboarding.MODE_AGENT!r}, got {mode!r}")
    if backend is not None and backend != "host":
        _fail("invalid_backend",
              f"--backend must be 'host' (the only forceable value — the "
              f"zero-LLM journey), got {backend!r}; omit the flag for the "
              "ambient backend choice",
              {"backend": backend})
    _verb(lambda: _start(mode, backend))


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


_RESUME_MAX_BYTES = 5 * 1024 * 1024  # the upload cap (audit F10): nothing
# downstream reads the file before this check, and a multi-GB --file used
# to be read fully into memory before any validation at all.

# Magic bytes of binary formats a text resume can never be (audit F10):
# zip containers (docx/odt/rtf-zip), images, gzip, native executables.
# The probe set mirrors common document/executable formats; any NUL byte
# in the head is disqualifying on its own — no text resume contains one.
_BINARY_HEAD_MAGICS: tuple[bytes, ...] = (
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
    b"\x1f\x8b", b"\x7fELF", b"MZ",
)


def _resume_head_is_binary(head: bytes) -> bool:
    """True when the first bytes mark a binary format (audit F10). Pure."""
    if b"\x00" in head:
        return True
    return any(head.startswith(magic) for magic in _BINARY_HEAD_MAGICS)


def _resume_upload(path: Path) -> onboarding.InterviewState:
    """Queue a resume upload with a type sniff and a size cap (audit F10).

    A file of ZIP bytes named fake.docx used to be read as text
    (errors="replace") and stored as THE resume — mangled byte-soup every
    downstream stage consumed, exit 0. Non-text/non-PDF is now a typed
    invalid_resume refusal, the size is capped before anything is read,
    and the PDF path keeps its existing honest extraction fallback
    (add_resume says so when the PDF yields no text).
    """
    size = path.stat().st_size
    if size > _RESUME_MAX_BYTES:
        _fail("invalid_resume",
              f"resume file is {size:,} bytes — the cap is "
              f"{_RESUME_MAX_BYTES // (1024 * 1024)} MB; export a smaller file",
              {"path": str(path), "size": size})
    with path.open("rb") as f:
        head = f.read(512)
    if path.suffix.lower() == ".pdf" or head.startswith(b"%PDF"):
        return onboarding.add_resume(pdf_bytes=path.read_bytes(), filename=path.name)
    if _resume_head_is_binary(head):
        _fail("invalid_resume",
              f"{path.name} is not a text resume or PDF (binary content) — "
              "upload the .txt export or a PDF",
              {"path": str(path)})
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
# The driving agent supplies the intelligence; Kestrel owns the queue
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

# F13/V3: the attempts-exhaustion caps the stage queues enforce
# (STAGE_CONDITIONS' COALESCE(tailor_attempts, 0) < cap / cover_attempts
# < cap — database.py). The write verbs must refuse a job the queue
# considers dead, so the two cannot drift: the same number, sourced from
# config.DEFAULTS — the single home for the cap (the literal 5 used to
# live in three places; see the wave-1 verification audit, finding V3).
_TAILOR_ATTEMPT_CAP = config.DEFAULTS["max_tailor_attempts"]
_COVER_ATTEMPT_CAP = config.DEFAULTS["max_cover_attempts"]

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
    f"3 short paragraphs, under {COVER_WORD_TARGET} words. Start with "
    "'Dear Hiring Manager,' and end with the sign-off name. No em or en "
    "dashes (auto-fixed on receipt). No meta-commentary. Validation runs "
    "in lenient mode: banned filler words are ignored, but LLM self-talk, "
    "a missing 'Dear' opening, and a length over "
    f"{COVER_WORD_HARD_CAP} words (the hard cap — every validation mode "
    "refuses longer letters and reports word_count) are hard errors."
)


def _pipeline_progress(conn=None) -> dict:
    """The progress block every pipeline payload carries: the counts a
    host loop needs to steer and terminate, for every stage at once.
    pending_detail and detail_errors are the stranded-jobs signals (jobs
    with no captured description never enter the score queue; they need
    enrich first, and the payload keeps that visible instead of silent)."""
    from kestrel.database import get_stats

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
        from kestrel.scoring.scorer import SCORE_PROMPT

        payload["rubric"] = SCORE_PROMPT
        # Spec Layer 3: a served job that already carries a score can only
        # be re-judged with --rescore (the already_scored refusal guards
        # the verb) — the payload says so instead of leaving the host to
        # discover it on a refused write.
        if job is not None and job.get("fit_score") is not None:
            payload["rescore_required"] = True
    elif stage == "enrich":
        payload["instructions"] = _ENRICH_INSTRUCTIONS
    elif stage == "tailor":
        from kestrel.scoring.tailor import TAILOR_DATA_TEMPLATE

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
    from kestrel.database import get_connection, get_jobs_by_stage, get_rerank_jobs

    conn = get_connection()
    job = None
    if stage == "score":
        # Spec Layer 3: `jobs rerank` marks a re-judgment set; job-next
        # serves exactly those first (newest mark first) before falling
        # back to the ordinary pending_score queue.
        marked = get_rerank_jobs(conn=conn, limit=1)
        if marked:
            job = marked[0]
    if job is None:
        jobs = get_jobs_by_stage(conn=conn, stage=_STAGE_QUEUES[stage], limit=1)
        job = jobs[0] if jobs else None
    return _stage_payload(stage, job, reveal)


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
                reasoning: str | None, rescore: bool, reveal: bool,
                prev_score: int | None = None) -> dict:
    """Validate and persist one host score, then serve the next payload.

    The write goes through scorer.persist_score — the same UPDATE the
    LLM path uses (spec: no parallel schema for host data) — stamped
    scored_by='host'. A failed verb never writes: validation errors are
    envelopes only.
    """
    from kestrel.database import get_connection, get_jobs_by_stage
    from kestrel.scoring.scorer import (
        PrevScoreMismatch,
        persist_score,
        read_resume_text,
        resume_sha256,
    )

    score = _validated_host_score(score_raw, reasoning)

    # Resume binding (spec Layer 1): stamp the sha256 of the current
    # resume.txt so the rerank flow can tell which scores went stale. A
    # missing resume leaves the score unbound (NULL) — the same
    # treated-as-stale class as pre-binding rows. Read through the
    # canonical reader (scorer.read_resume_text) so this hash can never
    # diverge from the one `jobs filter` compares against (audit
    # Finding 5): same strictness, same text, same hash.
    resume_hash = None
    resume_text = read_resume_text(config.RESUME_PATH)
    if resume_text is not None:
        resume_hash = resume_sha256(resume_text)

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

    try:
        persist_score(
            conn, url, score,
            keywords=(keywords or "").strip(),
            reasoning=(reasoning or "").strip(),
            scored_by="host",
            when=datetime.now(timezone.utc).isoformat(),
            resume_hash=resume_hash,
            prev_score=prev_score,
        )
        conn.commit()
    except PrevScoreMismatch as exc:
        # Spec Layer 3: the pass-through disagrees with the stored score —
        # the host judged against a stale payload. Nothing persisted.
        _fail("prev_score_mismatch", str(exc), {"url": url, "prev_score": prev_score})

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
    prev_score: Optional[int] = typer.Option(
        None, "--prev-score",
        help="The fit_score this judgment saw in the job-next payload — "
             "rerank movement reporting (spec Layer 3). Refused as a typed "
             "prev_score_mismatch envelope when it no longer matches the "
             "stored score; nothing persists."),
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
    _verb(lambda: _host_score(url, score, keywords, reasoning, rescore, reveal,
                              prev_score=prev_score))


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

    B7: an explicitly non-URL --application-url is a typed refusal, never
    a silent write — junk sentinels ('None'/'n/a') are exactly how the B1
    pollution class entered the store. An omitted or empty flag means
    "no URL captured" and preserves whatever the row already carries.
    """
    from kestrel.database import get_connection, get_jobs_by_stage
    from kestrel.enrichment.detail import persist_description

    desc = (full_description or "").strip()
    if not desc:
        _fail("invalid_description",
              "--full-description is required — an empty capture never "
              "moves a job into the score queue")

    if application_url is not None:
        candidate = application_url.strip()
        if candidate and not candidate.lower().startswith(("http://", "https://")):
            _fail("invalid_application_url",
                  f"--application-url {application_url!r} is not an http(s) "
                  "URL — pass the apply-button URL, or omit the flag when "
                  "the posting exposes none",
                  {"url": url, "given": application_url})

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
    from kestrel.database import get_connection, get_jobs_by_stage
    from kestrel.scoring.tailor import (
        assemble_resume_text,
        extract_json,
        persist_tailored,
    )
    from kestrel.scoring.validator import validate_json_fields

    profile = _load_profile_or_fail()

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
    # F13: the queue predicate excludes an attempts-exhausted job from
    # pending_tailor — the write verb must refuse it for the same reason
    # instead of silently writing (and incrementing attempts again on) a
    # job the LLM path's retry budget considers dead.
    if (job["tailor_attempts"] or 0) >= _TAILOR_ATTEMPT_CAP:
        _fail("stage_exhausted",
              f"{url} exhausted its {job['tailor_attempts']} tailor "
              "attempts — the pending_tailor queue excludes it; reset the "
              "attempts (portal board) to retry",
              {"url": url, "stage": "tailor",
               "attempts": job["tailor_attempts"],
               "cap": _TAILOR_ATTEMPT_CAP})

    # F14/V1: the job checks above come first — the same order as cover.
    # Parsing --data before them let an unknown-URL + malformed-data
    # mistake answer invalid_resume_data where cover answered unknown_job.
    try:
        data = extract_json(data_raw)
    except ValueError:
        _fail("invalid_resume_data",
              "--data is not the tailored-resume JSON object — return to "
              "the data_template shape and retry",
              {"errors": ["No valid JSON found in --data"], "retryable": True})
    if not isinstance(data, dict):
        # F6: 'null'/scalars/arrays parse fine but are not the resume-data
        # object — a typed refusal here replaces the internal_error a
        # TypeError deeper in validation ("NoneType is not iterable",
        # "sequence item 0: expected str instance") would raise.
        _fail("invalid_resume_data",
              f"--data must be the tailored-resume JSON object, got "
              f"{type(data).__name__} — return to the data_template shape and retry",
              {"errors": [f"Expected a JSON object, got {type(data).__name__}"],
               "retryable": True})

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
    """Check the job, then sanitize, validate, and persist one host cover
    letter, then serve the next cover payload.

    The job checks come first (audit F14 — the same order as tailor: you
    cannot fix the letter for a job you cannot write to), then sanitize_text
    runs — the same auto-fixes (em dashes, smart quotes) the LLM path
    applies before validating."""
    from kestrel.database import get_connection, get_jobs_by_stage
    from kestrel.scoring.cover_letter import persist_cover_letter
    from kestrel.scoring.validator import sanitize_text, validate_cover_letter

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
    # F13: mirror the pending_cover queue predicate — an attempts-exhausted
    # job is refused by the write verb for the same reason it is invisible
    # to job-next, instead of being written (and pushed further past the
    # cap) behind the caller's back.
    if (job["cover_attempts"] or 0) >= _COVER_ATTEMPT_CAP:
        _fail("stage_exhausted",
              f"{url} exhausted its {job['cover_attempts']} cover attempts "
              "— the pending_cover queue excludes it; reset the attempts "
              "(portal board) to retry",
              {"url": url, "stage": "cover",
               "attempts": job["cover_attempts"],
               "cap": _COVER_ATTEMPT_CAP})

    letter = sanitize_text(text)
    validation = validate_cover_letter(letter, mode="lenient")
    if not validation["passed"]:
        _fail("invalid_cover_letter",
              "the cover letter failed validation — fix the errors and "
              "retry (nothing was persisted)",
              {"errors": validation["errors"],
               "warnings": validation["warnings"],
               "avoid_notes": validation["errors"],
               # Surfaced (audit F7) so a too-long refusal can show the
               # driver exactly how far over the cap the letter is.
               "word_count": validation["word_count"],
               "retryable": True})

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

# ---------------------------------------------------------------------------
# Apply bookkeeping (the launcher's acquire/mark seams, host-driven, T3)
#
# These two verbs are bookkeeping ONLY: the per-job lock and the outcome
# record. The driving agent performs the real browser work itself (its own
# Chrome/Playwright tooling against the live board) — no verb here launches
# an engine, opens a browser, or touches a job board. The plan-then-confirm
# guardrail (PR #54) is required upstream of any real submission and is
# neither staged nor consumed here: these verbs never plan, never pin a
# batch, and never lift the 5/day external default.
# ---------------------------------------------------------------------------

# The PR #57 status vocabulary (engines.scan_result's RESULT markers plus
# failed:<reason> collapsed to failed) — the only outcomes apply-result
# records.
_APPLY_STATUSES = ("applied", "prepared", "failed", "expired", "captcha",
                   "login_issue")

# The apply-status vocabulary's terminal set — canonical in database.py
# (one source of truth: reset_failed's sweep reads the same tuple, so the
# protocol's permanent recording and the sweep's carve-out cannot drift).
_TERMINAL_APPLY_STATUSES = TERMINAL_APPLY_STATUSES


def _apply_progress() -> dict:
    """The cap block every apply verb carries: the external-driver daily
    cap (planning.AGENT_DEFAULT_DAILY_CAP, 5 — not liftable at the verb,
    by the guardrail), live-from-DB applied count, and the headroom."""
    from kestrel.apply.planning import AGENT_DEFAULT_DAILY_CAP
    from kestrel.database import count_applications_on

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    applied_today = count_applications_on(today)
    return {
        "cap": AGENT_DEFAULT_DAILY_CAP,
        "applied_today": applied_today,
        "headroom": max(AGENT_DEFAULT_DAILY_CAP - applied_today, 0),
    }


def _apply_guardrails() -> dict:
    """The guardrail block: what these verbs deliberately do NOT do."""
    return {
        "plan_then_confirm": (
            "required upstream of any real submission — 'kestrel apply "
            "--plan' / '--i-confirm <hash>' stages, pins, and consumes the "
            "batch token; these verbs neither stage nor consume it"),
        "browser_work": (
            "the driving agent performs the real submission with its own "
            "Chrome/Playwright tooling — no verb launches an engine or "
            "opens a browser"),
        "submission": (
            "an apply-result 'applied' records YOUR completed submission — "
            "the verb never submits anything itself"),
    }


def _locked_urls(conn) -> list[str]:
    """URLs under a fresh (non-stale) in_progress lock — the queue_busy
    signal distinguishing 'other loops hold the work' from 'queue empty'."""
    from kestrel.apply.launcher import _stale_lock_cutoff

    rows = conn.execute(
        "SELECT url FROM jobs WHERE apply_status = 'in_progress' "
        "AND last_attempted_at IS NOT NULL AND last_attempted_at > ?",
        (_stale_lock_cutoff(),),
    ).fetchall()
    return [row["url"] for row in rows]


def _host_apply_acquire(min_score_raw: str | None, reveal: bool) -> dict:
    """Take the per-job apply lock on the next eligible job.

    A thin wrapper over acquire_job (the launcher's own BEGIN IMMEDIATE
    lock path, agent_label='host') with the live daily-cap recheck in
    front of it — the same live-from-DB guard the worker run runs, so
    applications recorded by any path count. Refusals are envelopes:
    cap_reached, queue_busy (fresh locks hold every candidate), or
    no_eligible_job — never a fabricated job.
    """
    from kestrel.apply.launcher import acquire_job
    from kestrel.database import get_connection

    min_score = _validated_min_score(min_score_raw, default=7)

    if _daily_cap_reached_planning_cap():
        _fail("cap_reached",
              "the daily application cap is reached — no lock is taken; "
              "real submissions are also human-gated upstream by the "
              "plan-then-confirm loop",
              {"apply": _apply_progress()})

    conn = get_connection()
    # F8: claim_token=True stamps a one-time ownership token into
    # apply_task_id; apply-result must echo it back (--claim) to report.
    job = acquire_job(min_score=min_score, worker_id=0, agent_label="host",
                      claim_token=True)
    if job is None:
        locked = _locked_urls(conn)
        if locked:
            _fail("queue_busy",
                  "every candidate is under a fresh in_progress lock — "
                  "retry when the lock holders report or go stale",
                  {"locked": locked, "apply": _apply_progress()})
        _fail("no_eligible_job",
              "no job passes the apply queue's filters (tailored, scored "
              f">= {min_score}, unlocked, under the attempt limit) — run "
              "the discover/enrich/score/tailor stages first",
              {"min_score": min_score, "apply": _apply_progress()})

    # The full row (salary included — acquire_job's SELECT omits it), so
    # the payload shape matches the job-next stages.
    row = _load_job(conn, job["url"])
    payload: dict = {
        "stage": "apply",
        "job": _job_block(row),
        "application_url": job.get("application_url"),
        "resume_path": job.get("tailored_resume_path"),
        "cover_letter_path": job.get("cover_letter_path"),
        "acquired_by": "host-worker-0",
        # F8: the one-time ownership token — echo it as --claim on
        # apply-result; without it the report is refused (not_claim_owner).
        "claim": job.get("claim"),
        "apply": _apply_progress(),
        "progress": _pipeline_progress(conn),
        "resume": _scoring_resume_block(reveal),
        "guardrails": _apply_guardrails(),
    }
    if reveal:
        # The driver judges and submits: the profile's facts ride the
        # payload only behind --reveal, as on every stage payload.
        facts = _resume_facts()
        if facts:
            payload["resume_facts"] = facts
    return payload


def _daily_cap_reached_planning_cap() -> bool:
    """The live daily-cap recheck against the external-driver default."""
    from kestrel.apply.launcher import _daily_cap_reached
    from kestrel.apply.planning import AGENT_DEFAULT_DAILY_CAP

    return _daily_cap_reached(AGENT_DEFAULT_DAILY_CAP)


def _validated_min_score(min_score_raw: str | None, default: int) -> int:
    if min_score_raw is None:
        return default
    raw = str(min_score_raw).strip()
    # ^\d+$ — digits only (audit F5). The old lstrip("-") check admitted
    # negative integers ("-5" → "5"), contradicting this function's own
    # error message and silently disabling the queue's quality gate: the
    # fit_score >= ? predicate would admit every scored job in the store.
    if not re.fullmatch(r"\d+", raw):
        _fail("invalid_min_score",
              f"--min-score must be a non-negative integer, got {min_score_raw!r}",
              {"min_score": min_score_raw})
    return int(raw)


def _host_apply_result(url: str, status: str, reason: str | None,
                       min_score_raw: str | None, claim: str | None) -> dict:
    """Record one apply outcome through mark_result and release the lock.

    The URL must be under this loop's lock (apply-acquire took it):
    apply-result is the worker's own report path, not a mark override —
    human corrections go through the portal board or `apply
    --mark-applied/--mark-failed`, which carry the launcher's own
    coordination rules and stay deliberately tokenless. Since audit F8 the
    lock carries a one-time claim token: --claim must echo the token
    apply-acquire returned, and a wrong or missing token is refused with
    not_claim_owner before any write. Locks without a token (old stores,
    engine paths, stale reclaims by other paths) keep the pre-claim
    behavior — reportable as before, no check enforced. A refused outcome
    writes nothing.

    The vocabulary's `prepared` is a checkpoint, not an outcome: the
    driver reports it when materials are ready and the run stops at the
    human-confirmation gate. It keeps the lock and claim token — the
    final report (applied/failed/...) with the same --claim consumes the
    token and releases the lock.
    """
    from kestrel.apply.launcher import (
        ClaimMismatch,
        derive_batch,
        mark_result,
        resolve_version_id,
    )
    from kestrel.database import (
        KIND_COVER_LETTER,
        KIND_RESUME,
        get_connection,
    )

    if status not in _APPLY_STATUSES:
        _fail("invalid_status",
              f"--status must be one of {', '.join(_APPLY_STATUSES)} "
              "(the PR #57 vocabulary), got " + repr(status),
              {"status": status, "valid": list(_APPLY_STATUSES)})
    min_score = _validated_min_score(min_score_raw, default=7)

    conn = get_connection()
    job = _load_job(conn, url)
    if job is None:
        _fail("unknown_job", f"no job at {url}", {"url": url})
    if job["apply_status"] not in ("in_progress", "prepared"):
        _fail("not_acquired",
              f"{url} is not under an apply lock — acquire it with "
              "`agent apply-acquire` first; apply-result records outcomes "
              "for acquired jobs only",
              {"url": url, "apply_status": job["apply_status"]})

    # The apply-time snapshot (spec D3): the document versions the job's
    # materials point at — resolved at result time the way a manual mark
    # resolves them. NULL stays NULL for pre-history documents.
    try:
        outcome = mark_result(
            url, status,
            error=(reason or status) if status in _TERMINAL_APPLY_STATUSES else reason,
            permanent=status in _TERMINAL_APPLY_STATUSES,
            resume_version_id=resolve_version_id(
                conn, url, KIND_RESUME, job["tailored_resume_path"]),
            cover_version_id=resolve_version_id(
                conn, url, KIND_COVER_LETTER, job["cover_letter_path"]),
            claim=claim,
        )
    except ClaimMismatch as exc:
        _fail("not_claim_owner",
              f"{url} is not yours to report — apply-result must echo the "
              "one-time claim token apply-acquire returned (--claim); the "
              "lock holder's own claim records its outcome, every other "
              "loop's report is refused (no state was changed)",
              {"url": url, "hint": str(exc)})

    # F12: the lookahead applies the same live daily-cap recheck the next
    # apply-acquire runs in front of acquire_job — when the cap binds it
    # never advertises a job the next call would refuse with cap_reached;
    # cap_reached says which constraint the empty list reflects. The
    # entries share _job_block with apply-acquire and the stage payloads:
    # one job shape across the whole apply stage family.
    cap_reached = _daily_cap_reached_planning_cap()
    next_rows = [] if cap_reached else derive_batch(min_score=min_score, limit=1)
    return {
        "stage": "apply",
        "recorded": {
            "url": url,
            "status": status,
            "outcome": outcome,  # recorded | superseded | conflict — honest coordination
            "reason": reason,
            "terminal": status in _TERMINAL_APPLY_STATUSES,
        },
        "apply": _apply_progress(),
        "next_eligible": [_job_block(next_job) for next_job in next_rows],
        "cap_reached": cap_reached,
        "progress": _pipeline_progress(conn),
        "guardrails": _apply_guardrails(),
    }


@agent_app.command("apply-acquire")
def apply_acquire_cmd(
    min_score: Optional[str] = typer.Option(
        None, "--min-score",
        help="Minimum fit_score to apply to (default 7 — the apply queue's default)."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include the resume text and resume_facts (redacted by "
             "default — stdout lands in host chat logs)."),
) -> None:
    """Take the per-job apply lock on the next eligible job (bookkeeping only).

    The payload carries a one-time claim token at job.claim — echo it
    back as `apply-result --claim` to report the outcome; a wrong or
    missing token is refused (not_claim_owner) and the lock stays held.
    """
    _prepare_db()
    _verb(lambda: _host_apply_acquire(min_score, reveal))


@agent_app.command("apply-result")
def apply_result_cmd(
    url: Optional[str] = typer.Option(
        None, "--url", help="Job URL from the apply-acquire payload."),
    status: Optional[str] = typer.Option(
        None, "--status",
        help="One of applied, prepared, failed, expired, captcha, "
             "login_issue (the PR #57 vocabulary; captcha is terminal). "
             "prepared: materials are ready and the run stopped at the "
             "human-confirmation gate — it does NOT count toward the "
             "daily cap, keeps the lock and claim, and the same --claim "
             "reports the final outcome later."),
    claim: Optional[str] = typer.Option(
        None, "--claim",
        help="The one-time ownership token apply-acquire returned (audit "
             "F8). Required for locks acquired with a claim token; a wrong "
             "or missing token is refused (not_claim_owner) before any "
             "write. Tokenless locks (old stores, engine paths) report as "
             "before."),
    reason: Optional[str] = typer.Option(
        None, "--reason", help="Failure reason (stored as apply_error)."),
    min_score: Optional[str] = typer.Option(
        None, "--min-score",
        help="Selection for the returned next-eligible summary (default 7)."),
) -> None:
    """Record one apply outcome and release the lock (bookkeeping only)."""
    _prepare_db()
    if not url:
        _fail("invalid_usage", "--url is required")
    if not status:
        _fail("invalid_usage", "--status is required")
    _verb(lambda: _host_apply_result(url, status, reason, min_score, claim))


# ---------------------------------------------------------------------------
# Hiring leads (hiring-leads spec art_u0YGUqpk §5): discover and list.
#
# The verbs discover and list — they never send anything. No LinkedIn
# credential is read, no linkedin.com page is fetched, no outbound
# message exists anywhere on these paths (spec locked decisions); the
# guardrails block rides every payload so the host can hold the code to
# the statement. Personal data (lead names, profile URLs) is redacted
# by default because protocol stdout lands in host chat logs; --reveal
# opts back in per call, the same contract as the resume text.
# ---------------------------------------------------------------------------

# The mask for --reveal-gated personal data (the spec §5 payload shape).
_LEADS_REDACTED = "--reveal-gated--"


def _leads_guardrails() -> dict:
    """The guardrails block: what Kestrel deliberately does not do, plus
    the daily discovery-cap usage the host needs to pace its runs."""
    from kestrel.discovery.hiring_leads import discovery_runs_used

    cap = config.leads_daily_discovery_cap()
    used = discovery_runs_used()
    return {
        "sends_nothing": True,
        "credentials_stored": "none",
        "profile_pages_fetched": False,
        "daily_discovery_cap": cap,
        "discovery_used_today": used,
        "discovery_remaining_today": max(0, cap - used),
    }


def _lead_block(row: dict, reveal: bool) -> dict:
    """The wire shape of one lead — identity redacted unless --reveal."""
    return {
        "profile_url": row["profile_url"] if reveal else _LEADS_REDACTED,
        "full_name": row["full_name"] if reveal else _LEADS_REDACTED,
        "title": row["title"],
        "company_key": row["company_key"],
        "source": row["source"],
        "evidence_url": row["evidence_url"],
        "status": row["last_status"],
    }


def _validated_leads_limit(limit_raw: str | None) -> int | None:
    if limit_raw is None:
        return None
    if not re.fullmatch(r"\d+", limit_raw.strip()):
        _fail("invalid_limit", f"--limit must be a non-negative integer, "
              f"got {limit_raw!r}", {"limit": limit_raw})
    return int(limit_raw)


def _validated_min_fit(min_fit_raw: str | None) -> float | None:
    if min_fit_raw is None:
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", min_fit_raw.strip()):
        _fail("invalid_min_fit", f"--min-fit must be a non-negative number, "
              f"got {min_fit_raw!r}", {"min_fit": min_fit_raw})
    return float(min_fit_raw)


def _leads_discover(company_key: str | None, min_fit: float | None,
                    limit: int | None, reveal: bool) -> dict:
    """Run discovery (spec §4 entrypoint) and report the new leads."""
    from kestrel.database import hiring_leads_rows
    from kestrel.discovery.hiring_leads import (
        HiringLeadsError,
        LeadsRefusal,
        discover_hiring_leads,
    )

    before = {row["profile_url"]
              for row in hiring_leads_rows(company_key=company_key)}
    try:
        result = discover_hiring_leads(company_key=company_key,
                                       min_fit=min_fit, limit=limit)
    except LeadsRefusal as exc:
        # The typed refusals surface verbatim: code, fix-naming message,
        # details — plus the guardrails block, which rides every payload.
        _fail(exc.code, str(exc), {**exc.details,
                                   "guardrails": _leads_guardrails()})
    except HiringLeadsError as exc:
        # A source-level failure that is not one of the five refusals —
        # still an envelope, never a stack trace.
        _fail("source_error", str(exc), {"guardrails": _leads_guardrails()})

    cap = result["guardrails"]["daily_discovery_cap"]
    used = result["guardrails"]["discovery_used_today"]
    new_rows = [row for row in hiring_leads_rows(company_key=company_key)
                if row["profile_url"] not in before]
    return {
        "verb": "leads-discover",
        "sources_requested": result["sources_requested"],
        "sources_used": result["sources_used"],
        "retired_sources": result["retired_sources"],
        "companies_scanned": result["companies_scanned"],
        "companies_without_domain": result["companies_without_domain"],
        "scored_jobs_without_company": result["scored_jobs_without_company"],
        "discovered": result["discovered"],
        "skipped_existing": result["skipped_existing"],
        "leads": [_lead_block(row, reveal) for row in new_rows],
        "guardrails": {**result["guardrails"],
                       "discovery_remaining_today": max(0, cap - used)},
        "next": "Show the person the Leads board, or `kestrel-agent "
                "leads-list` — the connection itself is always the "
                "person's own action; Kestrel never sends anything.",
    }


def _leads_list(company_key: str | None, status: str | None,
                reveal: bool) -> dict:
    """The stored-lead queue document, filtered by company/status."""
    from kestrel.database import LEAD_STATUSES, hiring_leads_rows

    if status is not None and status not in LEAD_STATUSES:
        _fail("invalid_status",
              f"--status must be one of {', '.join(LEAD_STATUSES)}",
              {"status": status, "valid": list(LEAD_STATUSES)})
    rows = hiring_leads_rows(company_key=company_key, status=status)
    return {
        "verb": "leads-list",
        "count": len(rows),
        "leads": [_lead_block(row, reveal) for row in rows],
        "guardrails": _leads_guardrails(),
        "next": "Show the person the Leads board, or `kestrel-agent "
                "leads-list --reveal` for the profile links to open "
                "yourself in your own browser.",
    }


@agent_app.command("leads-discover")
def leads_discover_cmd(
    company: Optional[str] = typer.Option(
        None, "--company",
        help="Discover for one company key only; omit for every high-match "
             "company (default threshold: leads.yaml min_company_fit)."),
    min_fit: Optional[str] = typer.Option(
        None, "--min-fit",
        help="High-match threshold override for this run (default: "
             "leads.yaml min_company_fit)."),
    limit: Optional[str] = typer.Option(
        None, "--limit",
        help="Max companies scanned this run (provider spend), highest "
             "fit first."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include lead names and profile URLs (redacted by default — "
             "stdout lands in host chat logs)."),
) -> None:
    """Discover hiring leads at high-match companies — metadata only.

    Reads search-result metadata through the configured sources; never
    fetches a LinkedIn page, stores a LinkedIn credential, or sends
    anything. The daily discovery cap (leads.yaml) paces provider spend.
    """
    _prepare_db()
    _verb(lambda: _leads_discover(
        company, _validated_min_fit(min_fit),
        _validated_leads_limit(limit), reveal))


@agent_app.command("leads-list")
def leads_list_cmd(
    company: Optional[str] = typer.Option(
        None, "--company", help="Filter by company key."),
    status: Optional[str] = typer.Option(
        None, "--status",
        help="Filter by lead status: discovered, dismissed, or connected."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help="Include lead names and profile URLs (redacted by default — "
             "stdout lands in host chat logs)."),
) -> None:
    """List stored hiring leads — the queue the person works down."""
    _prepare_db()
    _verb(lambda: _leads_list(company, status, reveal))
