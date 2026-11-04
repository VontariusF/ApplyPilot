"""Onboarding interview agent: builds the profile through conversation.

One chat surface carries sign-in, the interview, and profile review
(spec D3). The agent loop runs through the local `claude` CLI in print
mode on the signed-in account, so a fresh user configures zero API keys;
when the CLI is unavailable, unsigned in, or a turn fails, the interview
falls back to the configured LLM provider (the A1 reversal, applied at
runtime). If neither backend exists, the portal shows setup instructions
rather than a dead end.

State lives in ~/.applypilot/onboarding-state.json (0600 via
write_secret_text — it holds resume text and preference answers), so a
portal restart never loses an in-flight interview.

Security boundary: the job-site password never enters the interview. The
agent prompt forbids asking for credentials, and the profile written on
confirm keeps personal.password empty (or preserves an existing value on
a redo) — the wizard's S2-hardened flow remains the only collector.

A1 note (validated 2026-11-04 against claude 2.1.197): the print-mode
envelope is a JSON object with `result`, `session_id`, and `is_error`;
an unsigned-in CLI exits 1 while still emitting that envelope. The
session-continuity path (--resume <session_id>) is accepted by the CLI
but could not be exercised live in this environment (no signed-in
account), so it is pinned by the scripted two-turn fake in tests and
protected at runtime by the LLM fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import yaml

from applypilot import config, engine_auth

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phases: gate -> interview -> review -> done
# ---------------------------------------------------------------------------

GATE = "gate"  # engine sign-in step (D1) — no interview yet
INTERVIEW = "interview"  # agent asks; user answers
REVIEW = "review"  # profile.md rendered; confirm or keep editing
DONE = "done"  # files written; the onboarding gate stops redirecting

# `claude -p` is an agentic CLI turn — it can think for a while, but a
# wedged CLI must not hold the portal's chat route forever.
TURN_TIMEOUT_SECONDS = 180.0

_PROFILE_KEYS = (
    "personal", "work_authorization", "availability", "compensation",
    "experience", "skills_boundary", "resume_facts", "eeo_voluntary",
)

# Roles in the transcript. "note" marks system-generated lines rendered
# as dim chat entries (backend fallbacks, resume uploads) — they are
# context for the agent, never agent output.
_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"
_ROLE_NOTE = "note"

_STATE_LOCK = threading.Lock()  # one agent turn at a time (single-user portal)


class OnboardingError(RuntimeError):
    """A user-facing onboarding failure (bad phase, no backend, agent error)."""


# ---------------------------------------------------------------------------
# Interview state (persisted)
# ---------------------------------------------------------------------------

@dataclass
class InterviewState:
    """Everything the interview needs to survive a portal restart."""

    phase: str = GATE
    backend: str = ""  # "claude" | "llm" — empty until the interview starts
    messages: list[dict] = field(default_factory=list)  # [{role, content}]
    claude_session_id: str | None = None  # print-mode --resume anchor
    profile_draft: dict | None = None  # agent-emitted, pre-confirm
    pending_context: str = ""  # resume text injected into the next agent turn
    error: str | None = None  # last user-facing failure, shown in the chat
    updated_at: str = ""


def load_state() -> InterviewState | None:
    """Load persisted interview state; None for a fresh (never-started) one."""
    path = config.ONBOARDING_STATE_PATH
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Onboarding state unreadable, starting fresh: %s", exc)
        return None
    known = {f for f in InterviewState.__dataclass_fields__}
    return InterviewState(**{k: v for k, v in raw.items() if k in known})


def save_state(state: InterviewState) -> None:
    """Persist state at 0600 (it carries resume text and preferences)."""
    state.updated_at = datetime.now(timezone.utc).isoformat()
    config.write_secret_text(
        config.ONBOARDING_STATE_PATH,
        json.dumps(asdict(state), indent=2, ensure_ascii=False),
    )


def clear_state() -> None:
    """Drop the persisted state (Redo interview)."""
    config.ONBOARDING_STATE_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The interview prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the ApplyPilot onboarding assistant. You interview the user one \
short exchange at a time and build their job-search profile.

Collect, in whatever order feels natural, confirming as you go:
- background: current or most recent role, years of experience, key skills
- target roles: the job titles to search for
- location and remote appetite: home base, and remote / hybrid / onsite
- salary: minimum acceptable annual salary and currency
- work authorization: legally authorized to work, and sponsorship needs

The user may paste or upload a resume. When resume text arrives in a \
message, read it and infer background, skills, education, and companies \
from it instead of asking again; confirm any inference that matters.

Rules:
- One or two short questions per reply, in plain sentences.
- NEVER ask for passwords, API keys, or other credentials. The job-site \
password is collected elsewhere by design. If the user volunteers one, \
tell them not to share it here and move on.
- Do not invent facts. Anything still unknown stays empty; the review \
screen lists missing fields as "add later".

When every item above is confirmed, end your reply with the complete \
profile in this exact fenced block, and nothing after it:

```json
{"_applypilot_profile": {"personal": {}, "work_authorization": {}, \
"availability": {}, "compensation": {}, "experience": {}, \
"skills_boundary": {}, "resume_facts": {}, "eeo_voluntary": {}}, \
"_applypilot_search": {"queries": ["example role"], \
"location": "Austin, TX", "remote": true}}
```

Use "" for unknown string fields and [] for unknown list fields. The \
personal.password field must always be ""."""

KICKOFF_PROMPT = (
    "The user just opened onboarding. Greet them in one sentence and ask "
    "about their background."
)

_RESUME_CONTEXT_TMPL = (
    "The user just provided their resume ({label}). Text:\n\n"
    "---\n{resume_text}\n---\n\n"
    "(Acknowledge briefly, extract background/skills/companies, and continue "
    "the interview.)"
)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class InterviewBackendError(RuntimeError):
    """A turn failed (transport, auth, timeout, unparseable output)."""


def _claude_binary() -> str | None:
    """Where the claude CLI resolves; tests point this at a fake."""
    return shutil.which("claude")


def parse_claude_turn(stdout: str) -> tuple[str, str | None, bool]:
    """Parse one `claude -p --output-format json` envelope.

    Returns (result_text, session_id, is_error). The envelope is a single
    JSON object (verified against claude 2.1.197, 2026-11-04):
    {"type": "result", "result": "...", "session_id": "...", "is_error": false}.
    Tolerates banner lines around the payload; when no JSON parses, the raw
    text is treated as the result (a future envelope drift must not wedge
    an interview — the LLM fallback is the safety net either way).
    """
    candidate = stdout.strip()
    if not candidate.startswith("{"):
        candidate = ""
        depth = 0
        for i, ch in enumerate(stdout):
            if ch == "{":
                depth += 1
                if depth == 1:
                    start = i
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = stdout[start:i + 1]
                    try:
                        json.loads(candidate)
                        break
                    except json.JSONDecodeError:
                        candidate = ""
    if candidate:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            result = payload.get("result")
            session_id = payload.get("session_id")
            is_error = bool(payload.get("is_error"))
            text = result if isinstance(result, str) else ""
            if not text and is_error:
                text = "the CLI reported an error without details"
            return text, (str(session_id) if session_id else None), is_error
    return stdout.strip(), None, False


class ClaudePrintBackend:
    """Runs each interview turn through `claude -p` on the signed-in account.

    Turn 1 starts a session; later turns pass --resume <session_id> so the
    CLI's stored conversation carries the context (print-mode continuity).
    stdin is /dev/null: the CLI otherwise waits ~3s per turn for piped input.
    """

    name = "claude"

    def kickoff(self, state: InterviewState) -> str:
        return self._run(state, KICKOFF_PROMPT, resume=False)

    def send(self, state: InterviewState, user_text: str) -> str:
        return self._run(state, user_text, resume=state.claude_session_id is not None)

    def _run(self, state: InterviewState, prompt: str, resume: bool) -> str:
        binary = _claude_binary()
        if binary is None:
            raise InterviewBackendError("the claude CLI is not installed")
        argv = [binary, "-p", "--output-format", "json"]
        if resume and state.claude_session_id:
            argv += ["--resume", state.claude_session_id]
        argv.append(prompt)
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                argv, capture_output=True, text=True,
                timeout=TURN_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise InterviewBackendError(
                f"the claude CLI did not answer within {TURN_TIMEOUT_SECONDS:g}s"
            ) from exc
        except OSError as exc:
            raise InterviewBackendError(f"could not run the claude CLI: {exc}") from exc
        result, session_id, is_error = parse_claude_turn(proc.stdout or "")
        if proc.returncode != 0 or is_error:
            detail = (result or (proc.stderr or "").strip() or
                      f"exit code {proc.returncode}").strip()
            raise InterviewBackendError(f"the claude CLI failed: {detail}")
        if session_id:
            state.claude_session_id = session_id
        return result


class LLMBackend:
    """Runs the interview on the configured LLM provider (fallback path).

    Unlike print mode, this backend has no server-side session: the full
    transcript rides in state.messages and is sent every turn, so the
    conversation survives backend switches mid-interview. Backends never
    mutate the transcript — the service layer owns the appends; this
    backend only projects it into a provider-valid message list.
    """

    name = "llm"

    def _client(self):
        from applypilot import llm  # local: llm imports are env-sensitive

        return llm.get_client()

    def kickoff(self, state: InterviewState) -> str:
        # The synthetic kickoff prompt is not part of the transcript (it
        # would render as a phantom user bubble); the greeting is.
        return self._client().chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": _ROLE_USER, "content": KICKOFF_PROMPT},
        ])

    def send(self, state: InterviewState, user_text: str) -> str:
        return self._client().chat(self._send_list(state.messages))

    @staticmethod
    def _send_list(transcript: list[dict]) -> list[dict]:
        """Project the transcript into provider-valid roles.

        The system prompt rides first; "note" entries become user-role
        lines (providers have no note role) marked as system-generated.
        """
        out = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in transcript:
            if m["role"] == _ROLE_NOTE:
                out.append({"role": "user", "content": f"(system) {m['content']}"})
            elif m["role"] in (_ROLE_USER, _ROLE_ASSISTANT):
                out.append({"role": m["role"], "content": m["content"]})
        return out


# ---------------------------------------------------------------------------
# Profile extraction and normalization
# ---------------------------------------------------------------------------

_FENCED_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_profile_draft(reply: str) -> dict | None:
    """Pull the profile draft out of an agent reply, or None.

    The handshake is a fenced JSON block carrying `_applypilot_profile`
    (the agent's final reply per the system prompt). A wrapper-less block
    that already looks like the eight-key profile is also accepted — LLMs
    drift from instructed wrappers and the user-visible result is the same.
    """
    for match in _FENCED_BLOCK.finditer(reply or ""):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("_applypilot_profile"), dict):
            draft = dict(payload)
            search = payload.get("_applypilot_search")
            draft["_applypilot_search"] = search if isinstance(search, dict) else {}
            return draft
        if sum(1 for k in _PROFILE_KEYS if isinstance(payload.get(k), dict)) >= 3:
            return {"_applypilot_profile": payload, "_applypilot_search": {}}
    return None


def _yes_no(value) -> str:
    """Coerce boolean-ish agent output to the profile's Yes/No strings."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value or "").strip()
    if text.lower() in ("yes", "y", "true"):
        return "Yes"
    if text.lower() in ("no", "n", "false"):
        return "No"
    return text


def _as_str_list(value) -> list[str]:
    """Accept a list or a comma-separated string; return clean strings."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [s.strip() for s in str(value or "").split(",") if s.strip()]


def _str(d: dict, key: str) -> str:
    value = d.get(key)
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def normalize_profile(draft: dict) -> tuple[dict, dict]:
    """Fit the agent's draft into the wizard-shaped profile + search meta.

    Returns (profile, search_meta). The profile always carries all eight
    top-level keys with the wizard's field shape (the de-facto schema the
    pipeline depends on). password is always "" here — preserve_profile
    restores an existing value at write time. search_meta carries the
    discovery-facing bits (queries, location, remote) that profile.json
    itself does not hold.
    """
    p = draft.get("_applypilot_profile") if isinstance(draft.get("_applypilot_profile"), dict) else draft
    search_in = draft.get("_applypilot_search") if isinstance(draft.get("_applypilot_search"), dict) else {}
    personal = p.get("personal") if isinstance(p.get("personal"), dict) else {}
    auth = p.get("work_authorization") if isinstance(p.get("work_authorization"), dict) else {}
    availability = p.get("availability") if isinstance(p.get("availability"), dict) else {}
    comp = p.get("compensation") if isinstance(p.get("compensation"), dict) else {}
    exp = p.get("experience") if isinstance(p.get("experience"), dict) else {}
    skills = p.get("skills_boundary") if isinstance(p.get("skills_boundary"), dict) else {}
    facts = p.get("resume_facts") if isinstance(p.get("resume_facts"), dict) else {}

    profile = {
        "personal": {
            "full_name": _str(personal, "full_name"),
            "preferred_name": _str(personal, "preferred_name"),
            "email": _str(personal, "email"),
            "phone": _str(personal, "phone"),
            "city": _str(personal, "city"),
            "province_state": _str(personal, "province_state"),
            "country": _str(personal, "country"),
            "postal_code": _str(personal, "postal_code"),
            "address": _str(personal, "address"),
            "linkedin_url": _str(personal, "linkedin_url"),
            "github_url": _str(personal, "github_url"),
            "portfolio_url": _str(personal, "portfolio_url"),
            "website_url": _str(personal, "website_url"),
            "password": "",  # never collected here — wizard-only (S2)
        },
        "work_authorization": {
            # Gating questions: no defaults. A fabricated "Yes"/"No" would be
            # submitted to real ATS forms on the candidate's behalf (sweep A S2-1).
            "legally_authorized_to_work": _yes_no(auth.get("legally_authorized_to_work")),
            "require_sponsorship": _yes_no(auth.get("require_sponsorship")),
            "work_permit_type": _str(auth, "work_permit_type"),
        },
        "availability": {
            "earliest_start_date": _str(availability, "earliest_start_date"),
        },
        "compensation": {
            "salary_expectation": _str(comp, "salary_expectation"),
            "salary_currency": _str(comp, "salary_currency") or "USD",
            "salary_range_min": _str(comp, "salary_range_min"),
            "salary_range_max": _str(comp, "salary_range_max"),
        },
        "experience": {
            "years_of_experience_total": _str(exp, "years_of_experience_total"),
            "education_level": _str(exp, "education_level"),
            "current_title": _str(exp, "current_title"),
            "target_role": _str(exp, "target_role"),
        },
        "skills_boundary": {
            "programming_languages": _as_str_list(skills.get("programming_languages")),
            "frameworks": _as_str_list(skills.get("frameworks")),
            "tools": _as_str_list(skills.get("tools")),
        },
        "resume_facts": {
            "preserved_companies": _as_str_list(facts.get("preserved_companies")),
            "preserved_projects": _as_str_list(facts.get("preserved_projects")),
            "preserved_school": _str(facts, "preserved_school"),
            "real_metrics": _as_str_list(facts.get("real_metrics")),
        },
        "eeo_voluntary": {
            "gender": "Decline to self-identify",
            "race_ethnicity": "Decline to self-identify",
            "veteran_status": "Decline to self-identify",
            "disability_status": "Decline to self-identify",
        },
    }
    search_meta = {
        "queries": _as_str_list(search_in.get("queries")) or
                   ([profile["experience"]["target_role"]]
                    if profile["experience"]["target_role"] else []),
        "location": _str(search_in, "location") or
                    ", ".join(x for x in (profile["personal"]["city"],
                                          profile["personal"]["province_state"]) if x),
        "remote": bool(search_in.get("remote")),
    }
    return profile, search_meta


def missing_review_fields(profile: dict) -> list[str]:
    """Human names for fields the review screen shows as "add later"."""
    missing = []
    auth = profile["work_authorization"]
    availability = profile["availability"]
    if not auth["legally_authorized_to_work"]:
        missing.append("Work authorization (yes/no)")
    if not auth["require_sponsorship"]:
        missing.append("Sponsorship needed (yes/no)")
    if not availability["earliest_start_date"]:
        missing.append("Earliest start date")
    if not profile["personal"]["full_name"]:
        missing.append("Full name")
    if not profile["personal"]["email"]:
        missing.append("Email address")
    if not profile["personal"]["phone"]:
        missing.append("Phone number")
    if not profile["experience"]["education_level"]:
        missing.append("Education level")
    return missing


def render_profile_md(profile: dict, search_meta: dict) -> str:
    """The human-readable review artifact the chat shows before confirm."""
    personal = profile["personal"]
    comp = profile["compensation"]
    exp = profile["experience"]
    auth = profile["work_authorization"]
    lines = [
        "**Here's the profile I'll save — profile.md**",
        "",
        f"- Name: {personal['full_name'] or '_(add later)_'}"
        f"{' (' + personal['preferred_name'] + ')' if personal['preferred_name'] else ''}",
        f"- Email: {personal['email'] or '_(add later)_'}",
        f"- Background: {exp['current_title'] or '_(not given)_'}"
        f"{', ' + exp['years_of_experience_total'] + ' yrs' if exp['years_of_experience_total'] else ''}",
        f"- Targets: {', '.join(search_meta.get('queries') or [exp['target_role']]) or '_(not given)_'}",
        f"- Base: {search_meta.get('location') or personal['city'] or '_(not given)_'}",
        f"- Remote: {'open to remote' if search_meta.get('remote') else 'onsite/hybrid only'}",
        f"- Salary: {comp['salary_expectation'] or '_(not given)_'} {comp['salary_currency']}".strip(),
        f"- Work authorization: {auth['legally_authorized_to_work'] or '_(needs answer)_'}, "
        f"sponsorship needed: {auth['require_sponsorship'] or '_(needs answer)_'}",
        f"- Education: {exp['education_level'] or '_(add later)_'}",
    ]
    skills = profile["skills_boundary"]
    flat = sum((skills[k] for k in ("programming_languages", "frameworks", "tools")), [])
    if flat:
        lines.append(f"- Skills: {', '.join(flat)}")
    facts = profile["resume_facts"]
    if facts["preserved_companies"] or facts["preserved_school"]:
        kept = ", ".join(facts["preserved_companies"] + ([facts["preserved_school"]] if facts["preserved_school"] else []))
        lines.append(f"- Preserved from resume: {kept}")
    return "\n".join(lines) + "\n"


def render_searches_yaml(search_meta: dict) -> str:
    """searches.yaml generated from the interview's discovery answers.

    Same consumer shape the wizard writes (defaults/locations/queries);
    yaml.safe_dump keeps values quoted and parseable whatever the user typed.
    """
    location = search_meta.get("location") or "Remote"
    payload = {
        "defaults": {
            "location": location,
            # Remote-only searches need no radius; else a modest local one.
            "distance": 0 if search_meta.get("remote") else 25,
            "hours_old": 72,
            "results_per_site": 50,
        },
        "locations": [
            {"location": location, "remote": bool(search_meta.get("remote"))},
        ],
        "queries": [
            {"query": q, "tier": min(i + 1, 3)}
            for i, q in enumerate(search_meta.get("queries") or ["Software Engineer"])
        ],
    }
    header = (
        "# ApplyPilot search configuration\n"
        "# Generated by the onboarding interview — edit freely to refine your search.\n\n"
    )
    return header + yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Readiness (which backend can the interview run on?)
# ---------------------------------------------------------------------------

def provider_ready() -> bool:
    """True when a fallback LLM provider is configured (no client built)."""
    from applypilot import llm

    try:
        llm._detect_provider()
        return True
    except RuntimeError:
        return False


def readiness() -> dict:
    """The step-1 gate picture: engine states and which start paths exist."""
    claude = engine_auth.claude_status()
    llm_ready = provider_ready()
    return {
        "claude": claude,
        "llm_ready": llm_ready,
        "can_start_claude": claude.state == engine_auth.SIGNED_IN,
        "can_start_llm": llm_ready,
        "can_start_any": claude.state == engine_auth.SIGNED_IN or llm_ready,
    }


# ---------------------------------------------------------------------------
# Service: the interview lifecycle (portal routes call these)
# ---------------------------------------------------------------------------

def _note(state: InterviewState, text: str) -> None:
    state.messages.append({"role": _ROLE_NOTE, "content": text})


def _agent_turn(state: InterviewState, user_text: str, prompt_for_claude: str) -> str:
    """Run one agent turn on the active backend, with the A1 fallback.

    The transcript already holds the user's message; the backend only
    computes the reply (the service appends it). When the claude backend
    fails mid-interview and a provider is configured, the interview
    switches to it and the turn retries there — the user sees a note, not
    an error. prompt_for_claude lets callers enrich the CLI prompt
    (resume context) without polluting the transcript.
    """
    if state.backend == "claude":
        try:
            return ClaudePrintBackend().send(state, prompt_for_claude)
        except InterviewBackendError as exc:
            if not provider_ready():
                raise OnboardingError(str(exc)) from exc
            log.warning("Onboarding: claude backend failed (%s); falling back to the LLM provider", exc)
            _note(state, "Claude Code became unavailable — continuing with the configured provider.")
            state.backend = "llm"
            state.claude_session_id = None
            return LLMBackend().send(state, user_text)
    return LLMBackend().send(state, user_text)


def _after_reply(state: InterviewState, reply: str) -> None:
    """Post-reply bookkeeping: draft extraction and phase transition.

    The transcript is the service layer's job; this only reacts to the
    reply's content.
    """
    draft = extract_profile_draft(reply)
    if draft is not None:
        profile, meta = normalize_profile(draft)
        state.profile_draft = {**profile, "_applypilot_search": meta}
        state.phase = REVIEW


def start_interview(mode: str) -> InterviewState:
    """Open (or resume) an interview on the chosen backend.

    mode "claude" requires the signed-in gate (D1); "llm" is the explicit
    skip to provider-key mode. The gate phase never reaches here unlocked.
    An existing live state resumes as-is — two tabs (or the post-sign-in
    refresh) must not wipe an interview in progress; only a cleared state
    (first run, or after redo_interview) starts fresh.
    """
    with _STATE_LOCK:
        existing = load_state()
        if existing is not None:
            return existing
        ready = readiness()
        if mode == "claude":
            if not ready["can_start_claude"]:
                raise OnboardingError("Claude Code is not signed in yet — finish sign-in first.")
            backend_name = "claude"
        elif mode == "llm":
            if not ready["can_start_llm"]:
                raise OnboardingError(
                    "No LLM provider is configured — add one on the Settings page first."
                )
            backend_name = "llm"
        else:
            raise OnboardingError(f"unknown interview mode {mode!r}")

        state = InterviewState(phase=INTERVIEW, backend=backend_name)
        if backend_name == "claude":
            try:
                reply = ClaudePrintBackend().kickoff(state)
            except InterviewBackendError as exc:
                # A1 runtime reversal: sign-in looked green but print mode
                # failed — the provider takes over when one exists.
                if not provider_ready():
                    raise OnboardingError(str(exc)) from exc
                log.warning("Onboarding kickoff: claude failed (%s); using the LLM provider", exc)
                _note(state, "Claude Code didn't answer — continuing with the configured provider.")
                state.backend = "llm"
                reply = LLMBackend().kickoff(state)
        else:
            reply = LLMBackend().kickoff(state)
        state.messages.append({"role": _ROLE_ASSISTANT, "content": reply})
        _after_reply(state, reply)
        save_state(state)
        return state


def send_message(text: str) -> InterviewState:
    """Advance the interview with one user message; flips to review on the draft."""
    text = (text or "").strip()
    if not text:
        raise OnboardingError("empty message")
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != INTERVIEW:
            raise OnboardingError("the interview is not active")
        state.messages.append({"role": _ROLE_USER, "content": text})
        prompt = text
        if state.pending_context:
            prompt = f"{state.pending_context}\n\nUser's message: {text}"
            state.pending_context = ""
        try:
            reply = _agent_turn(state, text, prompt)
        except RuntimeError as exc:
            # The user's message stays in the transcript; the chat shows the
            # failure and the composer stays live for a retry.
            state.error = str(exc)
            save_state(state)
            raise
        state.error = None
        state.messages.append({"role": _ROLE_ASSISTANT, "content": reply})
        _after_reply(state, reply)
        save_state(state)
        return state


def add_resume(text: str | None = None, pdf_bytes: bytes | None = None,
               filename: str = "resume.txt") -> InterviewState:
    """Store an uploaded/pasted resume and queue it for the next agent turn.

    Text (paste or .txt upload) becomes resume.txt; a PDF upload lands in
    resume.pdf — its text is not extracted here (parity with the wizard,
    which copies the PDF and asks for a txt separately), so the interview
    reads text resumes.
    """
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != INTERVIEW:
            raise OnboardingError("the interview is not active")
        if pdf_bytes is not None:
            config.RESUME_PDF_PATH.write_bytes(pdf_bytes)
            if os.name == "posix":
                os.chmod(config.RESUME_PDF_PATH, 0o600)
            _note(state, f"Resume uploaded: {filename} (PDF saved; the agent reads text resumes — paste or upload a .txt for it to read).")
        elif text:
            config.write_secret_text(config.RESUME_PATH, text)
            label = filename if filename else "pasted resume"
            state.pending_context = _RESUME_CONTEXT_TMPL.format(
                label=label, resume_text=text[:20_000],
            )
            _note(state, f"Resume provided: {label} ({len(text):,} characters).")
        else:
            raise OnboardingError("no resume content")
        save_state(state)
        return state


def confirm_profile() -> dict:
    """Write profile.json + profile.md + searches.yaml; the gate lifts.

    The interview never collects the job-site password: a redo preserves
    the existing profile's password rather than wiping it, and a fresh
    confirm writes "" (the wizard's S2 flow remains the only collector).
    """
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != REVIEW or not state.profile_draft:
            raise OnboardingError("there is no profile draft to confirm")
        draft = state.profile_draft
        profile, meta = normalize_profile(draft)
        if config.PROFILE_PATH.exists():
            try:
                existing = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
                old_password = (existing.get("personal") or {}).get("password")
                if old_password:
                    profile["personal"]["password"] = old_password
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Onboarding confirm: existing profile unreadable (%s)", exc)

        config.write_secret_text(
            config.PROFILE_PATH,
            json.dumps(profile, indent=2, ensure_ascii=False),
        )
        config.write_secret_text(
            config.PROFILE_MD_PATH,
            render_profile_md(profile, meta),
        )
        config.SEARCH_CONFIG_PATH.write_text(render_searches_yaml(meta), encoding="utf-8")

        state.phase = DONE
        state.error = None
        save_state(state)
        return profile


def keep_editing() -> InterviewState:
    """Back from review into the conversation (draft kept for context)."""
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != REVIEW:
            raise OnboardingError("nothing to edit")
        state.phase = INTERVIEW
        save_state(state)
        return state


def redo_interview() -> InterviewState:
    """Wipe the interview state and start over (profile.json stays put)."""
    clear_state()
    ready = readiness()
    mode = "claude" if ready["can_start_claude"] else "llm"
    return start_interview(mode)
