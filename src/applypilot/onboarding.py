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

The interview itself runs in one of two session modes. "choices" renders
a QuestionSpec registry as one guided question at a time (buttons, an
optional free-text input, depends_on skipping); each pick is stored in
state.answers and echoed to the agent as the user turn. "agent" hands
the agenda back to this free-form loop — the user can switch at any
step, and the final fenced-JSON extraction stays as the fallback path
for anything the wizard doesn't own (normalize_profile merges answers
first, agent JSON second; unanswered stays empty — PR #22's guard).

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

import io
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
# Phases: gate -> intake -> interview -> review -> done
# ---------------------------------------------------------------------------

GATE = "gate"  # engine sign-in step (D1) — no interview yet
INTAKE = "intake"  # resume upload step, offered before any question (resume-first)
INTERVIEW = "interview"  # agent asks; user answers
REVIEW = "review"  # profile.md rendered; confirm or keep editing
DONE = "done"  # files written; the onboarding gate stops redirecting

# Interview session modes. "choices" renders the wizard's question buttons
# (the portal owns the order); "agent" hands the agenda back to free-form
# chat. The user can flip between them at any step of the interview.
MODE_CHOICES = "choices"
MODE_AGENT = "agent"

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
_ROLE_SYSTEM = "system"
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
    # Guided-choice protocol: question id -> chosen Choice.value (or the
    # typed free text). An intentionally skipped question is present with
    # "" — answered-but-empty stays flagged on review (PR #22 guard).
    answers: dict[str, str] = field(default_factory=dict)
    mode: str = MODE_CHOICES  # guided buttons vs "let the agent ask"
    # Monotonic render version: bumped by every save, stamped into every
    # rendered surface, and used by the portal's SSE guard to drop pushes
    # that are older than what the DOM already shows (a push rendered
    # mid-POST can otherwise arrive after the POST's own fresher swap).
    seq: int = 0
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
    state.seq += 1
    state.updated_at = datetime.now(timezone.utc).isoformat()
    config.write_secret_text(
        config.ONBOARDING_STATE_PATH,
        json.dumps(asdict(state), indent=2, ensure_ascii=False),
    )


def clear_state() -> None:
    """Drop the persisted state (Redo interview)."""
    config.ONBOARDING_STATE_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The guided-question registry (spec: structured-choice onboarding)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    """One wizard button.

    `value` is stored verbatim in state.answers; `label` is what the
    button says — and the user turn the agent reads, so the agent sees
    prose exactly as in free-form chat.
    """

    value: str
    label: str


@dataclass(frozen=True)
class QuestionSpec:
    """One wizard question.

    `choices` render as buttons; `allow_free_text` adds a "something
    else" input typed per `free_text_kind` (None | "date" | "number" |
    "password" | "multiline" — password is supported by the contract but
    no question uses it: credentials never enter the interview). `maps_to`
    is a dotted path into the normalized profile ("personal.email") or
    the search meta ("search.location"); `depends_on` asks the question
    only when every parent's answer is in its comma-separated allowlist.
    `agent_hint` says what the "ask the agent" card should clarify.

    The resume is deliberately not a QuestionSpec: it is a file, handled
    by the existing /onboarding/resume route.
    """

    id: str
    prompt: str
    choices: tuple[Choice, ...] = ()
    allow_free_text: bool = False
    free_text_kind: str | None = None
    maps_to: str = ""
    depends_on: dict[str, str] | None = None
    agent_hint: str = ""


# The work-auth pair opens for any decided country: "*" matches any
# nonempty answer and "!Not sure yet" keeps the explicit undecided
# choice deferring the pair to the review step (the old six-value
# allowlist locked out typed countries like "Australia" and extracted
# ones alike, silently deferring the pair instead of asking it).
GATED_COUNTRY_GATE = "*,!Not sure yet"

QUESTION_SPECS: tuple[QuestionSpec, ...] = (
    # -- Identity first: who the applications are for (the resume usually
    #    already knows most of this, and nothing below makes sense without it) --
    QuestionSpec(
        id="full_name",
        prompt="What's your full name?",
        allow_free_text=True,
        maps_to="personal.full_name",
        agent_hint="the name as it should appear on applications",
    ),
    QuestionSpec(
        id="education",
        prompt="What's your highest education?",
        choices=(Choice("Bachelor's", "Bachelor's"), Choice("Master's", "Master's"),
                 Choice("PhD", "PhD"), Choice("Self-taught", "Self-taught")),
        allow_free_text=True,
        maps_to="experience.education_level",
        agent_hint="bootcamps and certificates count too",
    ),
    QuestionSpec(
        id="experience_years",
        prompt="How many years have you worked in your field?",
        choices=(Choice("0–2", "0–2"), Choice("3–5", "3–5"),
                 Choice("6–10", "6–10"), Choice("10+", "10+")),
        maps_to="experience.years_of_experience_total",
        agent_hint="a rough band is fine",
    ),
    # -- Preferences --
    QuestionSpec(
        id="work_style",
        prompt="How do you want to work?",
        choices=(Choice("remote", "Remote"), Choice("hybrid", "Hybrid"),
                 Choice("onsite", "On-site")),
        maps_to="search.remote",
        agent_hint="the trade-offs of remote, hybrid, and onsite",
    ),
    # -- Location: captured before anything that depends on a country. The
    #    work-auth pair below gates on this answer (fail-closed), so a
    #    "Not sure yet" (or a typed country) defers those to the review step. --
    QuestionSpec(
        id="country",
        prompt="Which country will you be working from / applying to jobs in?",
        choices=(Choice("United States", "United States"), Choice("United Kingdom", "United Kingdom"),
                 Choice("Canada", "Canada"), Choice("European Union", "European Union"),
                 Choice("India", "India"), Choice("Other", "Other"),
                 Choice("Not sure yet", "Not sure yet")),
        allow_free_text=True,  # "Other" — the typed country is the answer
        maps_to="personal.country",
        agent_hint="where the applications will be filed — or 'not sure yet' to decide later",
    ),
    # -- Eligibility: the gating pair (the fields PR #22 guards), asked only
    #    once a country exists --
    QuestionSpec(
        id="work_authorized",
        prompt="Are you legally authorized to work in the country you're applying in?",
        choices=(Choice("yes", "Yes"), Choice("no", "No")),
        maps_to="work_authorization.legally_authorized_to_work",
        depends_on={"country": GATED_COUNTRY_GATE},
        agent_hint="what counts as work authorization and how it's usually proved",
    ),
    QuestionSpec(
        id="sponsorship",
        prompt="Will you now or in the future need visa sponsorship?",
        choices=(Choice("yes", "Yes"), Choice("no", "No"), Choice("not_sure", "Not sure")),
        maps_to="work_authorization.require_sponsorship",
        depends_on={"country": GATED_COUNTRY_GATE},
        agent_hint="what sponsorship means and when employers offer it",
    ),
    QuestionSpec(
        id="permit_type",
        prompt="Which best describes your right to work?",
        choices=(Choice("Citizen", "Citizen"),
                 Choice("Permanent resident", "Permanent resident"),
                 Choice("Open work permit", "Open work permit"),
                 Choice("H-1B", "H-1B")),
        allow_free_text=True,  # "something else…" — the typed text is the answer
        maps_to="work_authorization.work_permit_type",
        depends_on={"work_authorized": "yes"},
        agent_hint="visa and permit categories beyond the four listed",
    ),
    QuestionSpec(
        id="currency",
        prompt="Which currency should salary conversations use?",
        choices=(Choice("USD", "USD"), Choice("EUR", "EUR"),
                 Choice("GBP", "GBP"), Choice("CAD", "CAD")),
        allow_free_text=True,
        maps_to="compensation.salary_currency",
        agent_hint="any ISO currency code works",
    ),
    QuestionSpec(
        id="salary_band",
        prompt="What's the minimum annual salary you'd accept?",
        choices=(Choice("120000", "120k – 140k"), Choice("140000", "140k – 170k"),
                 Choice("170000", "170k – 200k"), Choice("200000", "200k+")),
        allow_free_text=True,
        free_text_kind="number",
        maps_to="compensation.salary_expectation",
        agent_hint="picking a floor you won't want to renegotiate later",
    ),
    QuestionSpec(
        id="start_date",
        prompt="When could you start?",
        choices=(Choice("Immediately", "Immediately"), Choice("In 2 weeks", "In 2 weeks"),
                 Choice("In 1 month", "In 1 month"), Choice("In 3 months", "In 3 months")),
        allow_free_text=True,
        free_text_kind="date",
        maps_to="availability.earliest_start_date",
        agent_hint="typical notice periods",
    ),
    # -- Search shape --
    QuestionSpec(
        id="target_roles",
        prompt="Which job titles should we search for? One per line.",
        allow_free_text=True,
        free_text_kind="multiline",
        maps_to="search.queries",
        agent_hint="narrowing titles so results stay relevant",
    ),
    QuestionSpec(
        id="location",
        prompt="What city are you based in?",
        allow_free_text=True,
        maps_to="search.location",
        depends_on={"work_style": "onsite,hybrid"},  # remote-only searches need no radius
        agent_hint="why the search needs a home base for hybrid or onsite roles",
    ),
    QuestionSpec(
        id="skills_languages",
        prompt="Which programming languages do you use?",
        allow_free_text=True,
        free_text_kind="multiline",
        maps_to="skills_boundary.programming_languages",
        agent_hint="languages only — frameworks and tools come next",
    ),
    QuestionSpec(
        id="skills_frameworks",
        prompt="Which frameworks or libraries do you use?",
        allow_free_text=True,
        free_text_kind="multiline",
        maps_to="skills_boundary.frameworks",
        agent_hint="frameworks only — languages and tools are separate questions",
    ),
    QuestionSpec(
        id="skills_tools",
        prompt="Which tools do you work with?",
        allow_free_text=True,
        free_text_kind="multiline",
        maps_to="skills_boundary.tools",
        agent_hint="tools only — languages and frameworks are separate questions",
    ),
    # -- Personal fields (review flags name/email/phone when empty) --
    QuestionSpec(
        id="email",
        prompt="What email address should applications use?",
        allow_free_text=True,
        maps_to="personal.email",
        agent_hint="an inbox you actually check",
    ),
    QuestionSpec(
        id="phone",
        prompt="What phone number should applications use?",
        allow_free_text=True,
        maps_to="personal.phone",
        agent_hint="formatting doesn't matter",
    ),
    QuestionSpec(
        id="city",
        prompt="What city do you live in?",
        allow_free_text=True,
        maps_to="personal.city",
        agent_hint="your home city, for forms that ask independently of the search",
    ),
    QuestionSpec(
        id="linkedin_url",
        prompt="Link your LinkedIn profile (optional)",
        allow_free_text=True,
        maps_to="personal.linkedin_url",
        agent_hint="skipping is fine",
    ),
    QuestionSpec(
        id="github_url",
        prompt="Link your GitHub profile (optional)",
        allow_free_text=True,
        maps_to="personal.github_url",
        agent_hint="skipping is fine",
    ),
    QuestionSpec(
        id="website_url",
        prompt="Link a personal website (optional)",
        allow_free_text=True,
        maps_to="personal.website_url",
        agent_hint="skipping is fine",
    ),
    QuestionSpec(
        id="portfolio_url",
        prompt="Link a portfolio site (optional)",
        allow_free_text=True,
        maps_to="personal.portfolio_url",
        agent_hint="skipping is fine",
    ),
)

QUESTIONS: dict[str, QuestionSpec] = {q.id: q for q in QUESTION_SPECS}


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
- The portal may also show guided questions with buttons; the user's picks \
reach you as short statements. Treat each as a confirmed answer, react \
briefly, and let the portal drive the question order.
- Keep your replies short.

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

    def kickoff(self, state: InterviewState, prompt: str | None = None) -> str:
        return self._run(state, prompt or KICKOFF_PROMPT, resume=False)

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

    def kickoff(self, state: InterviewState, prompt: str | None = None) -> str:
        # The synthetic kickoff prompt is not part of the transcript (it
        # would render as a phantom user bubble); the greeting is.
        return self._client().chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": _ROLE_USER, "content": prompt or KICKOFF_PROMPT},
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


# Profile serialization for wizard answers. The two gating yes/no fields
# accept only Yes/No: a skip, "Not sure", or any unparsed value serializes
# to nothing at all so the review screen keeps flagging it — a fabricated
# or guessed yes/no would be submitted to real ATS forms (PR #22 guard).
_GATED_PATHS = (
    "work_authorization.legally_authorized_to_work",
    "work_authorization.require_sponsorship",
)
_GATED_VALUE_MAP = {"yes": "Yes", "no": "No"}

# Free-text answers for list-shaped fields arrive newline- or comma-
# separated; join them the same way _as_str_list splits agent lists.
_LIST_TARGETS = (
    "search.queries",
    "skills_boundary.programming_languages",
    "skills_boundary.frameworks",
    "skills_boundary.tools",
)


def _answers_to_draft(answers: dict[str, str]) -> dict:
    """Shape the wizard's answers map as an agent-style draft.

    Field ownership: a question answered with a value is the wizard's —
    its serialized answer clobbers the agent's draft. A skipped (empty)
    answer owns its field only when the field is GATED (yes/no gating
    must stay "" — the agent's extraction may never fill a yes/no the
    user declined, PR #22 guard); a skipped non-gating field stays open
    for the agent to fill if it extracted the value from the resume.
    Questions never answered at all stay open the same way.
    """
    profile: dict[str, dict[str, str]] = {}
    search: dict = {}
    for spec in QUESTION_SPECS:
        if spec.id not in answers or not spec.maps_to:
            continue
        value = (answers.get(spec.id) or "").strip()
        if spec.maps_to == "search.remote":
            # "remote" is the only style that means true; hybrid/onsite
            # are commute-style searches, not remote ones.
            if value:
                search["remote"] = value == "remote"
            continue
        if spec.maps_to in _GATED_PATHS:
            value = _GATED_VALUE_MAP.get(value.lower(), "")  # skip/"not sure" → ""
        elif not value:
            continue
        if spec.maps_to in _LIST_TARGETS:
            value = ", ".join(part.strip() for part in re.split(r"[,\n]", value) if part.strip())
        section, field_name = spec.maps_to.split(".", 1)
        if spec.maps_to.startswith("search."):
            search[field_name] = value
        else:
            profile.setdefault(section, {})[field_name] = value
    return {"_applypilot_profile": profile, "_applypilot_search": search}


def _overlay_answers(draft: dict | None, answers: dict[str, str]) -> dict:
    """The wizard's answers overlaid on the agent's draft — answers win.

    The wizard is the source of truth; the agent's fenced-JSON extraction
    fills only the fields the wizard doesn't own (spec: answers merge
    first, agent JSON second).
    """
    if not answers:
        return draft if isinstance(draft, dict) else {}
    overlay = _answers_to_draft(answers)
    base = draft if isinstance(draft, dict) else {}
    agent_profile = base.get("_applypilot_profile") if isinstance(base.get("_applypilot_profile"), dict) else {
        k: v for k, v in base.items() if k != "_applypilot_search"
    }
    agent_search = base.get("_applypilot_search") if isinstance(base.get("_applypilot_search"), dict) else {}
    merged_profile = dict(agent_profile)
    for section, fields in overlay["_applypilot_profile"].items():
        merged_profile[section] = {**(agent_profile.get(section) or {}), **fields}
    return {
        "_applypilot_profile": merged_profile,
        "_applypilot_search": {**agent_search, **overlay["_applypilot_search"]},
    }


def normalize_profile(draft: dict, answers: dict[str, str] | None = None) -> tuple[dict, dict]:
    """Fit the merged wizard+agent draft into the wizard-shaped profile.

    Returns (profile, search_meta). The profile always carries all eight
    top-level keys with the wizard's field shape (the de-facto schema the
    pipeline depends on). password is always "" here — preserve_profile
    restores an existing value at write time. search_meta carries the
    discovery-facing bits (queries, location, remote) that profile.json
    itself does not hold.

    The wizard's answers (state.answers) overlay the agent's draft: the
    wizard is the source of truth and the agent fills only what the
    wizard doesn't own. Unanswered questions serialize to "" and surface
    on review — no defaults (PR #22 guard).
    """
    merged = _overlay_answers(draft, answers or {})
    p = merged.get("_applypilot_profile") if isinstance(merged.get("_applypilot_profile"), dict) else merged
    search_in = merged.get("_applypilot_search") if isinstance(merged.get("_applypilot_search"), dict) else {}
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


# Questions the prefill bridge never fills even though the registry is
# not gated. currency rides normalize_profile's "USD" default: a stored
# "USD" is indistinguishable from an extracted one, so bridging it would
# record a currency preference the user never gave — the wizard keeps
# asking it (spec: currency must stay asked).
_NO_BRIDGE_IDS = frozenset({"currency"})


def draft_to_answers(profile_draft: dict | None, answers: dict[str, str]) -> dict[str, str]:
    """Map extracted draft values onto question answers — the prefill bridge.

    The reverse of normalize_profile's field walk: for every question the
    user has not answered whose maps_to path the extraction evidences, the
    draft value becomes the answer, so remaining_questions asks only the
    residual gaps. A preference question is bridged only on extraction
    evidence: work_style bridges solely on remote=True (False is the bool
    default, not an answer), and currency never bridges (_NO_BRIDGE_IDS).

    The fabrication guard is absolute here: gated fields (work
    authorization, sponsorship) are never prefilled from extraction — a
    resume claiming eligibility is a claim to confirm, not an answer to
    record. A pure-choice question (choices without allow_free_text)
    bridges only values its listed choices can represent — an extracted
    "6" for a banded years question stays unanswered so the wizard asks
    it. Extraction also never overwrites a recorded answer: questions
    already in `answers` are skipped, so the user's click always wins.
    """
    if not isinstance(profile_draft, dict) or not profile_draft:
        return {}
    wrapped = profile_draft.get("_applypilot_profile")
    sections = wrapped if isinstance(wrapped, dict) else profile_draft
    meta = profile_draft.get("_applypilot_search")
    meta = meta if isinstance(meta, dict) else {}
    filled: dict[str, str] = {}
    for spec in QUESTION_SPECS:
        if spec.id in answers or not spec.maps_to:
            continue
        if spec.maps_to in _GATED_PATHS or spec.id in _NO_BRIDGE_IDS:
            continue
        if spec.maps_to == "search.remote":
            if meta.get("remote") is True:
                filled[spec.id] = "remote"
            continue
        if spec.maps_to.startswith("search."):
            value = meta.get(spec.maps_to.split(".", 1)[1])
        else:
            section, field_name = spec.maps_to.split(".", 1)
            section_data = sections.get(section)
            value = section_data.get(field_name) if isinstance(section_data, dict) else None
        if isinstance(value, list):
            text = ", ".join(str(v).strip() for v in value if str(v).strip())
        else:
            text = str(value).strip() if value is not None else ""
        if not text:
            continue
        if spec.choices and not spec.allow_free_text and not any(c.value == text for c in spec.choices):
            # A pure-choice question cannot represent this value (an
            # extracted "6" for a banded years question): leave it
            # unanswered so the wizard asks it with its listed choices,
            # instead of planting an answer review can neither show nor
            # keep.
            continue
        filled[spec.id] = text
    return filled


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


# Fields whose emptiness the review screen flags as "needs answer" — the
# same set missing_review_fields names, keyed to the QuestionSpec that asks
# for them (spec id -> the profile path it feeds).
_REVIEW_FLAGGED_PATHS = frozenset({
    "work_authorization.legally_authorized_to_work",
    "work_authorization.require_sponsorship",
    "availability.earliest_start_date",
    "personal.full_name",
    "personal.email",
    "personal.phone",
    "experience.education_level",
})


def review_fields(state: InterviewState) -> list[dict]:
    """Rows for the review form: every question with its effective answer
    (recorded answers over draft prefill — the same precedence
    remaining_questions uses, so a skipped prefilled question shows its
    extracted value instead of a blank) and whether the field it feeds is
    still empty in the draft (the 'needs answer' chips — same fields
    missing_review_fields flags)."""
    draft = state.profile_draft or {}
    prefilled = draft_to_answers(draft, state.answers)
    effective = {**prefilled, **state.answers}

    def draft_value(path: str) -> str:
        if path.startswith("search."):
            value = draft.get("_applypilot_search", {}).get(path.split(".", 1)[1], "")
            return ", ".join(value) if isinstance(value, list) else value
        section, field = path.split(".", 1)
        section_data = draft.get(section)
        return section_data.get(field, "") if isinstance(section_data, dict) else ""

    rows = []
    for spec in QUESTION_SPECS:
        if spec.maps_to is None:
            continue
        rows.append({
            "spec": spec,
            "value": effective.get(spec.id, ""),
            "needs_answer": spec.maps_to in _REVIEW_FLAGGED_PATHS and not draft_value(spec.maps_to),
        })
    return rows


def update_answers(updates: dict[str, str]) -> InterviewState:
    """Save review-form edits back into the answers map (phase stays REVIEW).

    The review form shows the same widget set the wizard rendered, so the
    validation mirrors record_answer: choice questions accept their listed
    values — plus any off-list value where allow_free_text (the wizard
    accepts typed answers too, so an extracted "BSc" saves) — an empty
    selection clears the answer and stays flagged, and typed text only
    where allow_free_text. Unknown field names
    are ignored — never stored. No agent turn runs: editing at review is
    a quiet portal-side correction. The draft re-normalizes from the
    merged answers, and answers win over anything the agent extracted,
    so a cleared gating answer serializes to "" and stays flagged — the
    fabrication guard still holds.
    """
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != REVIEW:
            raise OnboardingError("there is no profile to review")
        for key, value in updates.items():
            spec = QUESTIONS.get(key)
            if spec is None:  # not a wizard question — ignore, never store
                continue
            value = (value or "").strip()
            if spec.choices:
                if (value and not any(c.value == value for c in spec.choices)
                        and not spec.allow_free_text):
                    raise OnboardingError("that question takes one of the listed choices")
                # allow_free_text choice questions take off-list values
                # (the wizard accepts typed answers too) — an extracted
                # "BSc" or "Australia" renders selected and saves as-is.
                state.answers[spec.id] = value
            elif spec.allow_free_text:
                state.answers[spec.id] = value
            # neither choices nor free text: not editable — ignore
        state.error = None
        profile, meta = normalize_profile(state.profile_draft or {}, state.answers)
        state.profile_draft = {**profile, "_applypilot_search": meta}
        save_state(state)
        return state


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


def _agent_turn(state: InterviewState, user_text: str, prompt_for_claude: str,
                directive: str | None = None,
                context_for_provider: str | None = None) -> str:
    """Run one agent turn on the active backend, with the A1 fallback.

    The transcript already holds the user's message; the backend only
    computes the reply (the service appends it). When the claude backend
    fails mid-interview and a provider is configured, the interview
    switches to it and the turn retries there — the user sees a note, not
    an error. prompt_for_claude lets callers enrich the CLI prompt
    (resume context) without polluting the transcript; directive is a
    synthetic instruction (mode switches) that reaches the backend but
    is never appended to the transcript; context_for_provider is the
    LLM-provider counterpart of prompt_for_claude — unrecorded context
    (the pending resume) that _llm_turn prepends to the request, since
    it rebuilds its messages from the transcript, which only carries the
    upload's filename/size note.
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
            return _llm_turn(state, directive, context_for_provider)
    return _llm_turn(state, directive, context_for_provider)


def _llm_turn(state: InterviewState, directive: str | None = None,
              context: str | None = None) -> str:
    """One LLMBackend turn: the projected transcript, plus unrecorded
    extras — a context block riding after the system message (the pending
    resume; the transcript itself only carries the upload's
    filename/size note) and a trailing
    directive when the caller supplied one (mode switches)."""
    client = LLMBackend()._client()
    messages = LLMBackend._send_list(state.messages)
    if context:
        # Unrecorded user-side input rides AFTER the system message,
        # never before it: strict OpenAI-compatible custom/local
        # endpoints reject (or fail to apply) a mid-conversation system
        # role, so reordering would break the onboarding instructions
        # for every resume-backed turn (Codex P2 on 24969c0).
        at = 1 if messages and messages[0].get("role") == _ROLE_SYSTEM else 0
        messages.insert(at, {"role": _ROLE_USER, "content": context})
    if directive:
        messages.append({"role": _ROLE_USER, "content": directive})
    return client.chat(messages)


def _strip_gated_claims(draft: dict) -> dict:
    """A wizard-path extraction has no authority over the gated pair.

    The resume-first kickoff (and any extraction while the wizard walks
    its agenda) reads a document, not the user — _GATED_PATHS stays
    absolute (spec art_ti3XJuNr): work authorization and sponsorship are
    set by a wizard answer or, in agent-led chat, the user's own prose —
    never by an extraction the wizard didn't ask about. Returns a copy;
    the caller's draft is untouched.
    """
    stripped = json.loads(json.dumps(draft))  # deep copy: callers reuse the dict
    profile = stripped.get("_applypilot_profile")
    auth = profile.get("work_authorization") if isinstance(profile, dict) else None
    if isinstance(auth, dict):
        auth.pop("legally_authorized_to_work", None)
        auth.pop("require_sponsorship", None)
    return stripped


def _after_reply(state: InterviewState, reply: str, allow_review: bool = True) -> bool:
    """Post-reply bookkeeping: draft extraction and phase transition.

    The transcript is the service layer's job; this only reacts to the
    reply's content. The wizard's answers overlay the agent's fenced-JSON
    draft — the wizard is the source of truth (PR #22 guard: the agent
    only fills what the wizard doesn't own). Returns True when the reply
    carried a draft — the caller decides what that retires (the pending
    resume context) and what it must not end (the wizard's agenda).

    allow_review=False for the kickoff and guided-answer turns: there the
    agenda is not the agent's to finish — an extraction mid-walk feeds the
    prefill bridge (remaining_questions then asks only the gaps) instead
    of yanking the wizard to review, and its gated claims are stripped
    (_strip_gated_claims). Free-form chat finishes on the draft as before,
    claims included — there the extraction paraphrases the user's own words.
    """
    draft = extract_profile_draft(reply)
    if draft is not None:
        if not allow_review:
            draft = _strip_gated_claims(draft)
        profile, meta = normalize_profile(draft, state.answers)
        state.profile_draft = {**profile, "_applypilot_search": meta}
        if allow_review:
            state.phase = REVIEW
        return True
    return False


def start_interview(mode: str) -> InterviewState:
    """Open (or resume) an interview on the chosen backend.

    mode "claude" requires the signed-in gate (D1); "llm" is the explicit
    skip to provider-key mode. The gate phase never reaches here unlocked.
    An existing live state resumes as-is — two tabs (or the post-sign-in
    refresh) must not wipe an interview in progress; only a cleared state
    (first run, or after redo_interview) starts fresh. The exception is
    INTAKE: starting the interview is the transition out of the intake
    step, carrying the uploaded resume's pending context into the kickoff
    turn so its reply extracts the draft before the first question.
    """
    with _STATE_LOCK:
        existing = load_state()
        if existing is not None and existing.phase != INTAKE:
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

        if existing is not None:  # INTAKE — the resume-first entry
            state = existing
        else:
            state = InterviewState(phase=INTERVIEW, backend=backend_name)
        state.backend = backend_name
        state.phase = INTERVIEW
        kickoff_prompt = KICKOFF_PROMPT
        if state.pending_context:
            kickoff_prompt = f"{state.pending_context}\n\n{KICKOFF_PROMPT}"
        if backend_name == "claude":
            try:
                reply = ClaudePrintBackend().kickoff(state, kickoff_prompt)
            except InterviewBackendError as exc:
                # A1 runtime reversal: sign-in looked green but print mode
                # failed — the provider takes over when one exists.
                if not provider_ready():
                    raise OnboardingError(str(exc)) from exc
                log.warning("Onboarding kickoff: claude failed (%s); using the LLM provider", exc)
                _note(state, "Claude Code didn't answer — continuing with the configured provider.")
                state.backend = "llm"
                reply = LLMBackend().kickoff(state, kickoff_prompt)
        else:
            reply = LLMBackend().kickoff(state, kickoff_prompt)
        state.messages.append({"role": _ROLE_ASSISTANT, "content": reply})
        # The kickoff never finishes the interview on its own: an extracted
        # draft prefills the agenda (gaps-only), it does not end it — the
        # wizard still asks the gated and preference questions. The pending
        # resume context is spent only when the reply actually extracted a
        # draft; otherwise it stays for the next turn (the persisted upload
        # note holds just the filename and size — this is the only copy of
        # the resume text in the agent's reach).
        if _after_reply(state, reply, allow_review=False):
            state.pending_context = ""
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
        context = state.pending_context
        if context:
            prompt = f"{context}\n\nUser's message: {text}"
            state.pending_context = ""
        try:
            reply = _agent_turn(state, text, prompt, context_for_provider=context)
        except RuntimeError as exc:
            # The user's message stays in the transcript; the chat shows the
            # failure and the composer stays live for a retry. The context
            # goes back in play too: a failed turn extracted nothing, so
            # persisting the spent state here would drop the only copy of
            # the resume.
            if context:
                state.pending_context = context
            state.error = str(exc)
            save_state(state)
            raise
        state.error = None
        state.messages.append({"role": _ROLE_ASSISTANT, "content": reply})
        # A turn that consumed pending resume context is document-informed:
        # its extraction is treated like the kickoff's (gated claims
        # stripped, agenda kept for the bridge), not like a paraphrase of
        # the user's own words. Pure chat turns keep the draft-to-review
        # finish.
        if not _after_reply(state, reply, allow_review=not context) and context:
            # No draft this turn: keep the pending context (the resume) in
            # play for the next one — a spent context that extracted nothing
            # would leave later turns with no access to the resume text.
            state.pending_context = context
        save_state(state)
        return state


def start_intake() -> InterviewState:
    """Open the intake step: resume upload before any question.

    Resume-first onboarding's first screen (spec art_ti3XJuNr): a state
    in the INTAKE phase accepts /onboarding/resume uploads and nothing
    else — no backend, no agent turns. Starting the interview
    (start_interview) is the transition out. An existing live state in a
    later phase resumes as-is (two tabs must not wipe work in progress);
    a fresh or gate-phase store opens intake.
    """
    with _STATE_LOCK:
        existing = load_state()
        if existing is not None and existing.phase != GATE:
            return existing
        state = InterviewState(phase=INTAKE)
        save_state(state)
        return state


def remaining_questions(state: InterviewState) -> list[QuestionSpec]:
    """The agenda still open: unanswered specs whose depends_on is met.

    Gaps-only asking (resume-first): questions the extraction draft can
    already answer are not asked — draft_to_answers maps state.profile_draft
    onto the open ids, and a recorded answer always beats extraction.
    Dependency gating evaluates against the effective answer set, so an
    extracted country opens the work-auth pair like a clicked one would.
    """
    prefilled = draft_to_answers(state.profile_draft, state.answers)
    effective = {**prefilled, **state.answers}
    open_specs = []
    for spec in QUESTION_SPECS:
        if spec.id in state.answers or spec.id in prefilled:
            continue
        if _depends_met(spec, effective):
            open_specs.append(spec)
    return open_specs


def _depends_met(spec: QuestionSpec, answers: dict[str, str]) -> bool:
    """True when every depends_on parent's answer satisfies its gate
    ("a,b" = either exact value; "*" = any nonempty answer; "!x" = an
    excluded value — combinable, e.g. "*,!Not sure yet"). An unanswered
    parent fails closed: the child waits until the parent settles, so
    the agenda never asks about a permit before it knows the user is
    authorized."""
    for parent, allowed in (spec.depends_on or {}).items():
        value = answers.get(parent, "")
        if not value:
            return False
        tokens = allowed.split(",")
        # Exclusions win before any wildcard or exact match.
        if any(token.startswith("!") and value == token[1:] for token in tokens):
            return False
        if not any(token == "*" or value == token for token in tokens):
            return False
    return True


def current_question(state: InterviewState) -> QuestionSpec | None:
    """The question the wizard shows now, or None when the agenda is done.

    Registry order is the agenda. Answered, skipped, and prefilled-from-
    extraction questions don't repeat; a question whose depends_on isn't
    met by the effective answers so far
    is skipped silently — its parent governs whether it makes sense at
    all, and the agent-led mode can still collect it in prose.
    """
    open_specs = remaining_questions(state)
    return open_specs[0] if open_specs else None


def record_answer(question_id: str, value: str = "", free_text: str = "",
                  skip: bool = False) -> InterviewState:
    """Record one guided answer and advance the agenda (the portal owns order).

    The chosen button appends its LABEL as the user turn (the agent reads
    prose, exactly as in free-form chat) and stores the VALUE in
    state.answers; free text stores what the user typed. A skip records
    the question as intentionally empty — it never becomes a profile
    value, so review keeps flagging it (PR #22 guard). The agent
    acknowledgement runs after the answer is persisted: a backend failure
    costs the acknowledgement, never the user's click.
    """
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != INTERVIEW:
            raise OnboardingError("the interview is not active")
        spec = QUESTIONS.get(question_id)
        if spec is None:
            raise OnboardingError("unknown question")

        if skip:
            state.answers[spec.id] = ""
            _note(state, f"Skipped \"{spec.prompt}\" — left empty on purpose (the review screen lists it as add-later).")
            label = ""
        else:
            typed = (free_text or "").strip()
            if typed:
                if not spec.allow_free_text:
                    raise OnboardingError("that question takes one of the listed choices")
                state.answers[spec.id] = typed
                label = typed
            else:
                choice = next((c for c in spec.choices if c.value == (value or "").strip()), None)
                if choice is None:
                    raise OnboardingError("pick one of the listed choices")
                state.answers[spec.id] = choice.value
                label = choice.label
            # kind="choice" marks wizard answers: the UI renders them as the
            # clicked button / filled widget, not as a chat bubble — typed
            # messages (no kind) always stay in the transcript.
            state.messages.append({"role": _ROLE_USER, "content": label, "kind": "choice"})

        if current_question(state) is None:
            # Agenda complete: the wizard's answers (plus anything the agent
            # extracted along the way) become the review draft.
            profile, meta = normalize_profile(state.profile_draft or {}, state.answers)
            state.profile_draft = {**profile, "_applypilot_search": meta}
            state.phase = REVIEW
        save_state(state)

        if label:
            prompt = label
            context = state.pending_context
            if context:
                prompt = f"{context}\n\nUser's message: {label}"
                state.pending_context = ""
            try:
                reply = _agent_turn(state, prompt, prompt, context_for_provider=context)
            except RuntimeError as exc:
                # The user's answer stays recorded; the chat shows the
                # failure and the next render carries the error banner.
                # The context goes back in play too: a failed ack turn
                # extracted nothing, so persisting the spent state here
                # would drop the only copy of the resume.
                if context:
                    state.pending_context = context
                state.error = str(exc)
                save_state(state)
                raise
            state.error = None
            state.messages.append({"role": _ROLE_ASSISTANT, "content": reply})
            if not _after_reply(state, reply, allow_review=False) and context:
                # Same retention rule as send_message: a draftless turn
                # keeps the pending resume in play for the next one.
                state.pending_context = context
        save_state(state)
        return state


_AGENT_LED_DIRECTIVE = (
    "The user switched to agent-led questions — you run the interview in "
    "your own words again. Ask about what's still missing: {fields}"
)

_BACK_TO_CHOICES_DIRECTIVE = (
    "The user switched back to the guided questions; the portal shows them "
    "one at a time. Acknowledge in one short sentence."
)


def set_agent_led(enabled: bool) -> InterviewState:
    """'Let the agent ask' — hand the agenda back to free-form chat.

    The switch persists before the agent hears about it, so a backend
    failure costs one acknowledgement, never the mode change. Returning
    to buttons resumes the agenda at the first unanswered question —
    nothing already answered is asked again.
    """
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase != INTERVIEW:
            raise OnboardingError("the interview is not active")
        if (state.mode == MODE_AGENT) == enabled:
            return state  # idempotent — double-clicks and stale tabs
        state.mode = MODE_AGENT if enabled else MODE_CHOICES
        state.error = None
        save_state(state)
        if enabled:
            remaining = ", ".join(spec.prompt.strip("?") for spec in remaining_questions(state))
            directive = _AGENT_LED_DIRECTIVE.format(fields=remaining or "anything the profile still needs")
        else:
            directive = _BACK_TO_CHOICES_DIRECTIVE
        # The pending resume rides along (context_for_provider): the
        # transcript only carries the upload's filename/size note, so
        # without it the provider could emit a fenced draft from the
        # directive alone — and clearing the context then would
        # permanently lose the only copy of the resume (Codex P2 on
        # ece4654). With the context supplied, a consuming draft has
        # actually earned the spend. The Claude CLI gets the context
        # embedded in its prompt text (the resumed session has no other
        # channel for content attached since the kickoff — Codex P2 on
        # f679e54).
        context = state.pending_context
        prompt_for_claude = directive
        if context:
            prompt_for_claude = f"{context}\n\nDirective: {directive}"
        try:
            reply = _agent_turn(state, directive, prompt_for_claude, directive=directive,
                                context_for_provider=context)
        except RuntimeError as exc:
            state.error = str(exc)
            save_state(state)
            raise
        state.messages.append({"role": _ROLE_ASSISTANT, "content": reply})
        # The switch turn is directive-driven — any draft it emits
        # paraphrases the agenda, never the user's own words, so it
        # finishes like the kickoff (allow_review=False). When the
        # draftless kickoff left pending resume context in play, the
        # draft is also document-informed: its gated claims are
        # stripped (_strip_gated_claims) and the agenda is kept for the
        # bridge instead of jumping to review (Codex P1 on 31060ea).
        if _after_reply(state, reply, allow_review=False) and context:
            state.pending_context = ""
        save_state(state)
        return state


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract the text of a PDF resume (pypdf); "" when nothing readable.

    Failures (encrypted, scanned, malformed) are logged and surface to
    the user as the "paste the text instead" note — never as a stack
    trace, and never as invented content.
    """
    try:
        from pypdf import PdfReader  # lazy: keeps portal startup light
    except ImportError as exc:  # pragma: no cover - pypdf ships as a dep
        log.warning("PDF resume extraction unavailable: %s", exc)
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises several parse-error variants
        log.warning("PDF resume unreadable: %s", exc)
        return ""
    return "\n".join(pages).strip()


def add_resume(text: str | None = None, pdf_bytes: bytes | None = None,
               filename: str = "resume.txt") -> InterviewState:
    """Store an uploaded/pasted resume and queue it for the next agent turn.

    Text (paste or .txt upload) becomes resume.txt. A PDF upload lands
    in resume.pdf AND has its text extracted (same bytes, one read) into
    resume.txt, so the interview and the whole downstream pipeline read
    it like any text resume; when nothing readable comes out (scanned or
    encrypted), the note asks for the text instead — nothing invented.

    Accepted in INTAKE as well as INTERVIEW: the intake step collects
    the resume before any question is asked (resume-first, spec
    art_ti3XJuNr) — the pending context rides the interview's kickoff
    turn, whose reply extracts the draft the prefill bridge consumes.
    """
    with _STATE_LOCK:
        state = load_state()
        if state is None or state.phase not in (INTAKE, INTERVIEW):
            raise OnboardingError("the interview is not active")
        if pdf_bytes is not None:
            config.RESUME_PDF_PATH.write_bytes(pdf_bytes)
            if os.name == "posix":
                os.chmod(config.RESUME_PDF_PATH, 0o600)
            extracted = extract_pdf_text(pdf_bytes)
            if extracted:
                config.write_secret_text(config.RESUME_PATH, extracted)
                state.pending_context = _RESUME_CONTEXT_TMPL.format(
                    label=filename, resume_text=extracted[:20_000],
                )
                _note(state, f"Resume uploaded: {filename} (PDF) — text extracted for the agent ({len(extracted):,} characters).")
            else:
                _note(state, f"Resume uploaded: {filename} (PDF saved, but its text couldn't be read — paste or upload a .txt and the agent will read that).")
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
        profile, meta = normalize_profile(draft, state.answers)
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
