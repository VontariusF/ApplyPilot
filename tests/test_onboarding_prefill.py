"""Resume-first prefill bridge (spec art_ti3XJuNr, parts 1-3).

The intake step accepts a resume before any question; the extracted
draft maps onto answers for every non-gated question (draft_to_answers),
so remaining_questions asks only the residual gaps. The fabrication
guard is absolute: gated fields (work authorization, sponsorship) are
never prefilled from extraction, and a recorded answer always beats it.
"""

import json
import re

from applypilot import config, onboarding

from tests.test_onboarding_choices import _qid, _walk

# An agent-style fenced-JSON draft claiming everything a rich resume
# would yield — INCLUDING gated work-auth claims the resume has no
# authority to answer (the PR #22 fabrication guard).
RAW_DRAFT = {
    "_applypilot_profile": {
        "personal": {
            "full_name": "Jane Doe", "email": "jane@example.com",
            "phone": "+1 555 0100", "city": "Austin", "country": "United States",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Yes", "require_sponsorship": "No",
            "work_permit_type": "Citizen",
        },
        "availability": {"earliest_start_date": "Immediately"},
        "compensation": {"salary_expectation": "140000", "salary_currency": "USD"},
        "experience": {
            "education_level": "Bachelor's", "years_of_experience_total": "6",
            "current_title": "Data Engineer", "target_role": "Data Engineer",
        },
        "skills_boundary": {
            "programming_languages": ["Python", "SQL"], "frameworks": ["FastAPI"],
        },
        "resume_facts": {}, "eeo_voluntary": {},
    },
    "_applypilot_search": {
        "queries": ["Data Engineer"], "location": "Austin, TX", "remote": True,
    },
}

DRAFT_REPLY = "Read your resume.\n\n```json\n" + json.dumps(RAW_DRAFT) + "\n```"


def seeded_state(answers: dict[str, str] | None = None) -> onboarding.InterviewState:
    """An interview state whose profile_draft holds the normalized draft."""
    profile, meta = onboarding.normalize_profile(RAW_DRAFT, answers or {})
    return onboarding.InterviewState(
        answers=dict(answers or {}),
        profile_draft={**profile, "_applypilot_search": meta},
    )


# ---------------------------------------------------------------------------
# The bridge: draft_to_answers

def test_gap_count_drops_to_the_residual_questions():
    """With a full extraction, the agenda asks only what extraction may
    not answer: the gated pair, currency (normalize_profile's "USD"
    default is indistinguishable from extraction, so currency never
    bridges), and experience_years (the draft's raw "6" cannot represent
    a listed band, so the wizard asks the banded question)."""
    state = seeded_state()
    open_ids = [q.id for q in onboarding.remaining_questions(state)]
    assert open_ids == [
        "experience_years", "work_authorized", "sponsorship", "currency",
        "skills_tools", "linkedin_url", "github_url", "website_url",
        "portfolio_url",
    ]


def test_gated_fields_stay_empty_when_the_draft_claims_work_auth():
    """A resume saying 'authorized, no sponsorship' is a claim to confirm,
    not an answer to record — _GATED_PATHS is absolute."""
    filled = onboarding.draft_to_answers(RAW_DRAFT, {})
    assert "work_authorized" not in filled
    assert "sponsorship" not in filled
    assert "permit_type" in filled  # non-gated: bridges freely

    state = seeded_state()
    open_ids = [q.id for q in onboarding.remaining_questions(state)]
    assert "work_authorized" in open_ids and "sponsorship" in open_ids


def test_recorded_answer_beats_extraction():
    """Extraction never overwrites a recorded answer — the user's click
    wins at the agenda AND in the normalized profile (precedence of
    normalize_profile, answers over draft)."""
    answers = {"full_name": "User Typed", "city": "Boston"}
    filled = onboarding.draft_to_answers(RAW_DRAFT, answers)
    assert "full_name" not in filled and "city" not in filled
    assert filled["email"] == "jane@example.com"  # untouched fields still bridge

    profile, _ = onboarding.normalize_profile(RAW_DRAFT, answers)
    assert profile["personal"]["full_name"] == "User Typed"
    assert profile["personal"]["city"] == "Boston"
    assert profile["personal"]["email"] == "jane@example.com"


def test_bridge_prefills_only_on_extraction_evidence():
    """A preference bridges only when the draft evidences it: remote=True
    bridges work_style, remote=False (the bool default) does not; an
    empty/absent draft fills nothing; currency never bridges."""
    assert onboarding.draft_to_answers(RAW_DRAFT, {})["work_style"] == "remote"

    offsite = {**RAW_DRAFT, "_applypilot_search": {"queries": [], "location": "", "remote": False}}
    filled = onboarding.draft_to_answers(offsite, {})
    assert "work_style" not in filled  # False is the default, not an answer
    assert "currency" not in filled  # normalize's USD default is not evidence

    assert onboarding.draft_to_answers(None, {}) == {}
    assert onboarding.draft_to_answers({}, {}) == {}


def test_bridge_only_plants_representable_choice_values():
    """A pure-choice question bridges only values its listed choices can
    represent: the draft's raw "6" never plants itself as the banded
    years answer (review could neither show nor keep it — Codex P2 on
    58ba3be), while an allow_free_text question bridges its off-list
    text ("BSc") because the wizard accepts typed answers too."""
    offlist = {**RAW_DRAFT, "_applypilot_profile": {
        **RAW_DRAFT["_applypilot_profile"],
        "experience": {**RAW_DRAFT["_applypilot_profile"]["experience"],
                       "education_level": "BSc"}}}
    filled = onboarding.draft_to_answers(offlist, {})
    assert "experience_years" not in filled  # unrepresentable: wizard asks
    assert filled["education"] == "BSc"  # free-text choice: bridges as typed


# ---------------------------------------------------------------------------
# The fabrication guard under resume-first extraction (Codex review of #41)

def test_offlist_extracted_country_still_asks_the_gated_pair():
    """An extracted country outside the choice list ("Australia") must
    not lock the work-auth pair out of the agenda: the pair opens for
    any decided country, so its values are confirmed, never assumed."""
    draft = {**RAW_DRAFT, "_applypilot_profile": {
        **RAW_DRAFT["_applypilot_profile"],
        "personal": {**RAW_DRAFT["_applypilot_profile"]["personal"], "country": "Australia"}}}
    profile, meta = onboarding.normalize_profile(draft, {})
    state = onboarding.InterviewState(profile_draft={**profile, "_applypilot_search": meta})
    open_ids = [q.id for q in onboarding.remaining_questions(state)]
    assert "work_authorized" in open_ids and "sponsorship" in open_ids
    # The wizard's own typed country hits the same gate:
    state.answers["country"] = "Australia"
    open_ids = [q.id for q in onboarding.remaining_questions(state)]
    assert "work_authorized" in open_ids and "sponsorship" in open_ids

def test_pending_context_survives_until_extraction(portal_client, fake_llm, monkeypatch):
    """The pending resume context is the only copy of the resume the
    agent can reach: a draftless kickoff or chat turn keeps it for the
    next turn — and the provider actually receives it on those turns
    (the llm backend rebuilds its request from the transcript, which
    only carries the upload's filename/size note); extraction spends it."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # kickoff: no draft

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})
    state = onboarding.load_state()
    assert "MARKER-RESUME-7788" in state.pending_context  # retained

    fake_llm.respond("Thanks — anything else?")  # chat turn: still no draft
    client.post("/onboarding/send", data={"text": "I also like hiking"})
    state = onboarding.load_state()
    assert "MARKER-RESUME-7788" in state.pending_context  # still retained
    # The provider got it: the context rides the request, unrecorded.
    assert any("MARKER-RESUME-7788" in m["content"] for m in fake_llm.calls[-1])

    fake_llm.respond(DRAFT_REPLY)  # extraction lands
    client.post("/onboarding/send", data={"text": "that's everything"})
    state = onboarding.load_state()
    assert state.pending_context == ""  # spent
    assert state.profile_draft["personal"]["full_name"] == "Jane Doe"


def test_spent_context_echo_still_strips_gated_claims(portal_client, fake_llm, monkeypatch):
    """After the pending context is spent, the backend can still echo
    resume-derived authorization claims — Claude from its session
    memory, the provider from the earlier unstripped assistant reply in
    the transcript — so a later chat-turn draft cannot be trusted as
    the user's own prose (Codex P1 on 1f1b505). Every extraction is
    behind the gate: the draft still finishes to review, but the gated
    pair arrives empty for the wizard to ask."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # kickoff: no draft

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})

    fake_llm.respond(DRAFT_REPLY)  # first extraction spends the context
    client.post("/onboarding/send", data={"text": "that's everything"})
    assert onboarding.load_state().pending_context == ""

    fake_llm.respond(DRAFT_REPLY)  # the memory-echo turn: no context delivered
    client.post("/onboarding/send", data={"text": "yes, submit it"})
    state = onboarding.load_state()
    assert state.phase == onboarding.REVIEW  # chat draft still finishes to review
    auth = state.profile_draft["work_authorization"]
    assert auth["legally_authorized_to_work"] == ""  # the echo never survives
    assert auth["require_sponsorship"] == ""


def test_guided_answer_keeps_the_resume_in_play(portal_client, fake_llm, monkeypatch):
    """A guided-answer ack is a pure answer-acknowledgement turn (spec
    art_ti3XJuNr H3): the pending resume context is never prepended to it
    — it attaches to the next question-asking turn instead, so one button
    click can neither hijack the ack onto an unrelated topic nor spend
    the only copy of the resume. The ack still grounds the agent on the
    exact question and answer (H1), via an unrecorded directive."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # kickoff: no draft

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})
    state = onboarding.load_state()
    assert "MARKER-RESUME-7788" in state.pending_context

    fake_llm.respond("Got it — noted.")
    client.post("/onboarding/answer",
                data={"question_id": "full_name", "free_text": "Jane Q. Public"})
    state = onboarding.load_state()
    assert state.answers["full_name"] == "Jane Q. Public"
    assert "MARKER-RESUME-7788" in state.pending_context  # ack turn: untouched
    # The provider never saw the resume on the ack turn — and it did get
    # the grounded directive naming the question and the submitted value.
    ack_request = fake_llm.calls[-1]
    assert not any("MARKER-RESUME-7788" in m["content"] for m in ack_request)
    assert "What's your full name?" in ack_request[-1]["content"]
    assert "Jane Q. Public" in ack_request[-1]["content"]

    fake_llm.respond(DRAFT_REPLY)  # extraction lands on a question-asking turn
    client.post("/onboarding/send", data={"text": "that's everything"})
    state = onboarding.load_state()
    assert state.pending_context == ""  # spent: exactly once, on the upload turn


def test_failed_turn_keeps_the_resume_in_play(portal_client, fake_llm, monkeypatch):
    """A provider failure on a resume-backed turn must not persist the
    spent state: both the chat path and the guided-answer path restore
    pending_context before saving the error, so the resume survives a
    timeout to be extracted on a later turn (Codex P2 on 6576bef)."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # kickoff: no draft

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})

    def boom(state, *args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(onboarding, "_agent_turn", boom)
    try:
        onboarding.send_message("I also like hiking")
    except RuntimeError:
        pass
    state = onboarding.load_state()
    assert "provider down" in state.error  # the failure is surfaced, not swallowed
    assert "MARKER-RESUME-7788" in state.pending_context  # retained through the failure

    try:
        onboarding.record_answer("full_name", free_text="Jane Q. Public")
    except RuntimeError:
        pass
    state = onboarding.load_state()
    assert state.answers["full_name"] == "Jane Q. Public"  # answer persisted
    assert "MARKER-RESUME-7788" in state.pending_context  # retained here too


def test_kickoff_extraction_never_records_gated_claims(portal_client, fake_llm, monkeypatch):
    """The intake kickoff reads a document, not the user: its draft may
    prefill identity and preferences, but gated work-auth claims are
    stripped — the wizard asks and the user's answer wins."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond(DRAFT_REPLY)  # claims Yes / No / Citizen from the resume

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "Jane Doe — resume text"})
    client.post("/onboarding/start", data={"mode": "llm"})
    state = onboarding.load_state()
    auth = state.profile_draft["work_authorization"]
    assert auth["legally_authorized_to_work"] == ""  # claim stripped
    assert auth["require_sponsorship"] == ""  # claim stripped
    assert auth["work_permit_type"] == "Citizen"  # not gated: survives

    # The user confirms at the wizard — the recorded answer lands.
    onboarding.record_answer("work_authorized", "yes")
    state = onboarding.load_state()
    assert state.answers["work_authorized"] == "yes"
    assert "sponsorship" in [q.id for q in onboarding.remaining_questions(state)]


def test_resume_backed_chat_extraction_never_records_gated_claims(portal_client, fake_llm, monkeypatch):
    """A chat turn that consumed the pending resume context is
    document-informed like the kickoff: its draft may not jump to review
    and its gated work-auth claims are stripped (Codex P1 on 58ba3be) —
    otherwise the retained resume could confirm authorization and
    sponsorship into the profile without the user ever answering."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # draftless kickoff

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})
    assert onboarding.load_state().pending_context  # resume in play

    fake_llm.respond(DRAFT_REPLY)  # claims Yes / No / Citizen from the resume
    client.post("/onboarding/send", data={"text": "that's everything"})
    state = onboarding.load_state()
    assert state.phase == onboarding.INTERVIEW  # the agenda is not the agent's to finish
    assert state.pending_context == ""  # spent: the extraction landed
    auth = state.profile_draft["work_authorization"]
    assert auth["legally_authorized_to_work"] == ""  # claim stripped
    assert auth["require_sponsorship"] == ""  # claim stripped
    assert auth["work_permit_type"] == "Citizen"  # not gated: survives
    assert "work_authorized" not in state.answers


def test_agent_led_switch_never_records_gated_claims(portal_client, fake_llm, monkeypatch):
    """A mode-switch turn is directive-driven; with pending resume
    context still in play (a draftless kickoff left the resume with the
    resumed session), its draft is also document-informed — so it may
    not jump to review and its gated claims are stripped, same guard as
    the kickoff and resume-backed chat turns (Codex P1 on 31060ea)."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # draftless kickoff

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})
    assert onboarding.load_state().pending_context  # resume in play

    fake_llm.respond(DRAFT_REPLY)  # the switch turn echoes the resume
    onboarding.set_agent_led(True)
    sent = fake_llm.calls[-1]
    assert sent[0]["role"] == "system"  # stays first
    assert sent[1]["role"] == "user" and "MARKER-RESUME-7788" in sent[1]["content"]
    state = onboarding.load_state()
    assert state.mode == onboarding.MODE_AGENT
    assert state.phase == onboarding.INTERVIEW  # the agenda is not the agent's to finish
    auth = state.profile_draft["work_authorization"]
    assert auth["legally_authorized_to_work"] == ""  # claim stripped
    assert auth["require_sponsorship"] == ""  # claim stripped
    assert auth["work_permit_type"] == "Citizen"  # not gated: survives
    assert state.pending_context == ""  # spent: the extraction consumed the supplied resume


def test_agent_led_switch_without_a_draft_keeps_the_resume(portal_client, fake_llm, monkeypatch):
    """Without a draft, the switch turn consumed nothing: the pending
    resume context survives for the agent-led turns that follow —
    spending it on a directive-only turn would permanently lose the
    only copy of the resume (Codex P2 on ece4654)."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # draftless kickoff

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})

    fake_llm.respond("Got it — ask me anything.")
    onboarding.set_agent_led(True)
    state = onboarding.load_state()
    assert state.mode == onboarding.MODE_AGENT
    assert state.phase == onboarding.INTERVIEW
    assert state.profile_draft in (None, {})  # nothing was extracted
    assert state.pending_context  # the resume stays in play


def test_resume_context_rides_after_the_system_message(portal_client, fake_llm, monkeypatch):
    """The pending resume context rides AFTER the system message in the
    provider request, never at index 0 — strict OpenAI-compatible
    custom/local endpoints reject (or fail to apply) a
    mid-conversation system role, which would break every
    resume-backed chat turn (Codex P2 on 24969c0)."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # draftless kickoff

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})
    assert onboarding.load_state().pending_context

    fake_llm.respond("Noted — tell me more about your experience.")
    onboarding.send_message("I also like hiking")
    sent = fake_llm.calls[-1]
    assert sent[0]["role"] == "system"  # stays first
    assert sent[1]["role"] == "user"  # context right after it, never before
    assert "MARKER-RESUME-7788" in sent[1]["content"]


# ---------------------------------------------------------------------------
# The intake phase: upload before any question, gaps-only after start

def test_intake_resume_then_start_walks_gaps_only(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond(DRAFT_REPLY)  # the kickoff turn's extraction reply

    onboarding.start_intake()
    page = client.post("/onboarding/resume", data={"resume_text": "Jane Doe — resume text"})
    assert page.status_code == 200
    state = onboarding.load_state()
    assert state.phase == onboarding.INTAKE
    assert config.RESUME_PATH.exists()  # stored before any question is asked

    # Starting the interview consumes the pending resume context and the
    # kickoff reply extracts the draft — no question is wiped by the start.
    client.post("/onboarding/start", data={"mode": "llm"})
    state = onboarding.load_state()
    assert state.phase == onboarding.INTERVIEW
    assert state.pending_context == ""
    assert state.profile_draft["personal"]["full_name"] == "Jane Doe"

    # The wizard's first question is a gap, not an already-extracted field:
    # the draft's raw "6" cannot represent a listed band, so the banded
    # years question stays asked (ahead of the gated pair, in spec order).
    page = client.get("/onboarding/chat")
    assert _qid(page.text) == "experience_years"
    rendered = _walk(client, {"experience_years": "6–10", "work_authorized": "yes",
                              "sponsorship": "no", "currency": "USD"})
    assert rendered == [
        "experience_years", "work_authorized", "sponsorship", "currency",
        "skills_tools", "linkedin_url", "github_url", "website_url",
        "portfolio_url",
    ]
    state = onboarding.load_state()
    assert state.phase == onboarding.REVIEW


def test_intake_kickoff_prompt_carries_the_resume(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond("Hello! Tell me about your background.")  # no draft yet

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-7788"})
    client.post("/onboarding/start", data={"mode": "llm"})

    kickoff_messages = fake_llm.calls[0]
    assert "MARKER-RESUME-7788" in kickoff_messages[-1]["content"]
    state = onboarding.load_state()
    assert state.phase == onboarding.INTERVIEW  # no draft: the agenda is intact


# ---------------------------------------------------------------------------
# The review form: effective values

def test_review_fields_show_effective_draft_values():
    """Review rows render effective answers — recorded answers over draft
    prefill, the same precedence remaining_questions uses — so a question
    skipped via prefill shows the extracted value instead of a blank,
    while a recorded answer still wins (Codex P2 on 80f92a0)."""
    prefilled = seeded_state()
    prefilled.phase = onboarding.REVIEW
    rows = {r["spec"].id: r["value"] for r in onboarding.review_fields(prefilled)}
    assert rows["full_name"] == "Jane Doe"  # from the draft, never recorded
    assert rows["education"] == "Bachelor's"  # from the draft

    answered = seeded_state({"full_name": "Jane Q. Public"})
    answered.phase = onboarding.REVIEW
    rows = {r["spec"].id: r["value"] for r in onboarding.review_fields(answered)}
    assert rows["full_name"] == "Jane Q. Public"  # recorded beats extraction


def test_review_select_shows_and_keeps_offlist_prefills(portal_client, fake_llm, monkeypatch):
    """A choice question whose effective value is off the option list
    (an extracted "BSc") renders that value as its own selected option —
    the browser used to display and submit the blank option over it,
    so saving wiped the prefill (Codex P2 on 58ba3be). The save accepts
    the off-list value for a free-text-allowed question, while a
    pure-choice question still rejects junk."""
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    draft = {**RAW_DRAFT, "_applypilot_profile": {
        **RAW_DRAFT["_applypilot_profile"],
        "experience": {**RAW_DRAFT["_applypilot_profile"]["experience"],
                       "education_level": "BSc"}}}
    profile, meta = onboarding.normalize_profile(draft, {})
    onboarding.save_state(onboarding.InterviewState(
        profile_draft={**profile, "_applypilot_search": meta}, phase=onboarding.REVIEW))

    page = client.get("/onboarding/chat").text
    select = re.search(r'<select id="answer-education".*?</select>', page, re.S)
    assert select
    assert '<option value="BSc" selected>' in select.group(0)  # displayed, selectable

    page = client.post("/onboarding/review", data={"answer_education": "BSc"}).text
    assert onboarding.load_state().answers["education"] == "BSc"  # kept, not wiped

    page = client.post("/onboarding/review", data={"answer_work_authorized": "hacked"}).text
    assert "alert error" in page  # pure-choice junk still rejected
