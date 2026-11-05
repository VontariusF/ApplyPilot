"""Resume-first prefill bridge (spec art_ti3XJuNr, parts 1-3).

The intake step accepts a resume before any question; the extracted
draft maps onto answers for every non-gated question (draft_to_answers),
so remaining_questions asks only the residual gaps. The fabrication
guard is absolute: gated fields (work authorization, sponsorship) are
never prefilled from extraction, and a recorded answer always beats it.
"""

import json

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
    not answer: the gated pair plus currency (normalize_profile's "USD"
    default is indistinguishable from extraction, so currency never
    bridges)."""
    state = seeded_state()
    open_ids = [q.id for q in onboarding.remaining_questions(state)]
    assert open_ids == [
        "work_authorized", "sponsorship", "currency", "skills_tools",
        "linkedin_url", "github_url", "website_url", "portfolio_url",
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

    # The wizard's first question is a gap, not an already-extracted field.
    page = client.get("/onboarding/chat")
    assert _qid(page.text) == "work_authorized"
    rendered = _walk(client, {"work_authorized": "yes", "sponsorship": "no", "currency": "USD"})
    assert rendered == [
        "work_authorized", "sponsorship", "currency", "skills_tools",
        "linkedin_url", "github_url", "website_url", "portfolio_url",
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
