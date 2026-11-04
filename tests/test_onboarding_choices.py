"""Onboarding structured choices: the QuestionSpec registry, the persisted
answers map, the /onboarding/answer agenda, the agent-led session mode,
PDF resume extraction, and the fabrication guard.

The registry walk drives every question through the HTTP route with the
shared fake-LLM fixture (conftest) standing in for the provider backend
and the scripted `claude -p` fake (wizard mode, test_onboarding_agent)
for the CLI backend.
"""

import html
import json
import re

import pytest

from applypilot import config, engine_auth, onboarding

from tests.test_onboarding_agent import FAKE_CLAUDE, SIGNED_IN

QUIET_DROID = engine_auth.EngineStatus("droid", engine_auth.NOT_INSTALLED, None, None)


def patch_gates(monkeypatch, claude=SIGNED_IN):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: claude)
    monkeypatch.setattr(engine_auth, "droid_status", lambda *a, **k: QUIET_DROID)


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """The scripted claude fake in wizard mode, logging every prompt."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(fake))
    prompt_log = tmp_path / "prompts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(prompt_log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "wizard")
    return prompt_log


# ---------------------------------------------------------------------------
# Walk fixtures: the full happy-path answer set (registry order)

FULL_WALK = {
    "work_authorized": "yes",
    "sponsorship": "no",
    "permit_type": "Citizen",
    "work_style": "remote",
    "currency": "USD",
    "salary_band": "120000",
    "start_date": "Immediately",
    "education": "Bachelor's",
    "experience_years": "3–5",
    "target_roles": "free:Data Engineer\nAnalytics Engineer",
    "skills_languages": "free:Python\nSQL",
    "skills_frameworks": "free:FastAPI",
    "skills_tools": "free:Docker",
    "full_name": "free:Test Person",
    "email": "free:test@example.com",
    "phone": "free:+1 555 0100",
    "city": "free:Austin, TX",
    "linkedin_url": None,
    "github_url": None,
    "website_url": None,
    "portfolio_url": None,
}

# What the answers map should hold afterwards: values for choices, the
# typed text for free text, "" for the intentionally skipped URLs.
STORED = {
    qid: raw[5:] if isinstance(raw, str) and raw.startswith("free:") else (raw or "")
    for qid, raw in FULL_WALK.items()
}


def _start(client, mode="llm"):
    page = client.post("/onboarding/start", data={"mode": mode})
    assert page.status_code == 200
    return page.text


def _qid(page_html):
    match = re.search(r'name="question_id" value="([^"]+)"', page_html)
    return match.group(1) if match else None


def _answer(client, qid, value=None, free="", skip=False):
    form = {"question_id": qid}
    if skip:
        form["skip"] = "1"
    elif free:
        form["free_text"] = free
    else:
        form["value"] = value or ""
    page = client.post("/onboarding/answer", data=form)
    assert page.status_code == 200
    return page.text


def _walk(client, answers):
    """Answer every rendered question until the surface leaves the interview.

    answers maps question id -> choice value, "free:<text>", or None
    (skip). Returns the question ids in the order they were rendered.
    """
    rendered = []
    for _ in range(len(onboarding.QUESTION_SPECS) + 2):
        page = client.get("/onboarding/chat")
        assert page.status_code == 200
        qid = _qid(page.text)
        if qid is None:
            return rendered
        rendered.append(qid)
        spec = onboarding.QUESTIONS[qid]
        assert spec.prompt in html.unescape(page.text)
        raw = answers.get(qid)
        if raw is None:
            _answer(client, qid, skip=True)
        elif isinstance(raw, str) and raw.startswith("free:"):
            _answer(client, qid, free=raw[5:])
        else:
            _answer(client, qid, value=raw)
    raise AssertionError("the agenda never completed")


def _expected_ack_prompts():
    """The user turns the agent should read for STORED: labels for choices,
    the typed text for free text, nothing for skipped questions."""
    prompts = []
    for spec in onboarding.QUESTION_SPECS:
        if spec.id == "location":
            continue  # never rendered: the walk chose remote
        raw = STORED[spec.id]
        if not raw:
            continue  # skipped — recorded empty, no acknowledgement turn
        prompts.append(next((c.label for c in spec.choices if c.value == raw), raw))
    return prompts


# ---------------------------------------------------------------------------
# The registry itself

def test_registry_covers_the_spec_table():
    expected = [
        "work_authorized", "sponsorship", "permit_type", "work_style", "currency",
        "salary_band", "start_date", "education", "experience_years", "target_roles",
        "location", "skills_languages", "skills_frameworks", "skills_tools",
        "full_name", "email", "phone", "city", "linkedin_url", "github_url",
        "website_url", "portfolio_url",
    ]
    assert [q.id for q in onboarding.QUESTION_SPECS] == expected
    for spec in onboarding.QUESTION_SPECS:
        assert spec.prompt.strip()
        assert spec.agent_hint.strip()
        assert spec.maps_to.strip()
        assert spec.choices or spec.allow_free_text
        if spec.free_text_kind is not None:
            assert spec.allow_free_text
            assert spec.free_text_kind in ("date", "number", "password", "multiline")
        for parent, allowed in (spec.depends_on or {}).items():
            assert parent in onboarding.QUESTIONS
            assert allowed.strip()
    # The permit question takes both buttons and an "other" free text.
    permit = onboarding.QUESTIONS["permit_type"]
    assert permit.choices and permit.allow_free_text
    assert permit.free_text_kind is None
    assert permit.depends_on == {"work_authorized": "yes"}


# ---------------------------------------------------------------------------
# The HTTP agenda walk (provider backend)

def test_walk_renders_records_and_advances_every_question(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    page = _start(client)
    assert "Skip for now" in page and "Let the agent ask" in page

    rendered = _walk(client, FULL_WALK)
    state = onboarding.load_state()
    assert state.phase == onboarding.REVIEW
    assert rendered == [q.id for q in onboarding.QUESTION_SPECS if q.id != "location"]
    assert state.answers == STORED

    # The agent read the LABELS as user turns (prose, same as free-form chat).
    turns = [m["content"] for m in state.messages if m["role"] == "user"]
    assert "Yes" in turns and "Citizen" in turns and "120k – 140k" in turns and "3–5" in turns

    draft = state.profile_draft
    assert draft["work_authorization"]["legally_authorized_to_work"] == "Yes"
    assert draft["work_authorization"]["require_sponsorship"] == "No"
    assert draft["work_authorization"]["work_permit_type"] == "Citizen"
    assert draft["compensation"]["salary_expectation"] == "120000"
    assert draft["compensation"]["salary_currency"] == "USD"
    assert draft["experience"]["education_level"] == "Bachelor's"
    assert draft["experience"]["years_of_experience_total"] == "3–5"
    assert draft["skills_boundary"]["programming_languages"] == ["Python", "SQL"]
    assert draft["_applypilot_search"]["queries"] == ["Data Engineer", "Analytics Engineer"]
    assert draft["_applypilot_search"]["remote"] is True
    assert draft["_applypilot_search"]["location"] == "Austin, TX"  # falls back to the city

    page = client.post("/onboarding/confirm")
    assert page.status_code == 200
    written = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
    assert written["work_authorization"] == {
        "legally_authorized_to_work": "Yes",
        "require_sponsorship": "No",
        "work_permit_type": "Citizen",
    }
    assert onboarding.missing_review_fields(written) == []


def test_depends_on_skips_unmet_questions(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    rendered = _walk(client, dict(FULL_WALK, work_authorized="no"))

    assert "permit_type" not in rendered
    state = onboarding.load_state()
    assert "permit_type" not in state.answers
    assert state.answers["work_authorized"] == "no"
    assert "location" not in state.answers  # remote-only: no radius needed
    profile, _ = onboarding.normalize_profile({}, state.answers)
    assert profile["work_authorization"]["work_permit_type"] == ""


def test_free_text_kinds_route_correctly(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)

    # A choices-only question refuses free text; nothing is recorded.
    page = _answer(client, "work_authorized", free="I think so, yes")
    assert "takes one of the listed choices" in page
    assert onboarding.load_state().answers == {}

    _answer(client, "work_authorized", value="yes")
    _answer(client, "sponsorship", value="no")
    _answer(client, "permit_type", free="EAD holder, awaiting green card")
    _answer(client, "salary_band", free="133000")
    _answer(client, "start_date", free="2027-03-01")
    _answer(client, "target_roles", free="Data Engineer\nAnalytics Engineer")

    answers = onboarding.load_state().answers
    assert answers["permit_type"] == "EAD holder, awaiting green card"
    assert answers["salary_band"] == "133000"
    assert answers["start_date"] == "2027-03-01"
    assert answers["target_roles"] == "Data Engineer\nAnalytics Engineer"

    profile, meta = onboarding.normalize_profile({}, answers)
    assert profile["work_authorization"]["work_permit_type"] == "EAD holder, awaiting green card"
    assert profile["compensation"]["salary_expectation"] == "133000"
    assert profile["availability"]["earliest_start_date"] == "2027-03-01"
    assert meta["queries"] == ["Data Engineer", "Analytics Engineer"]


# ---------------------------------------------------------------------------
# The fabrication guard (PR #22): unanswered never becomes an answer

def test_skip_records_empty_and_flags_them_for_review(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    _walk(client, {qid: None for qid in onboarding.QUESTIONS})

    state = onboarding.load_state()
    assert state.phase == onboarding.REVIEW
    assert set(state.answers.values()) == {""}
    profile, _ = onboarding.normalize_profile({}, state.answers)
    assert profile["work_authorization"]["legally_authorized_to_work"] == ""
    assert profile["work_authorization"]["require_sponsorship"] == ""
    missing = onboarding.missing_review_fields(profile)
    assert "Work authorization (yes/no)" in missing
    assert "Sponsorship needed (yes/no)" in missing
    notes = " ".join(m["content"] for m in state.messages if m["role"] == "note")
    assert "left empty on purpose" in notes

    client.post("/onboarding/confirm")
    written = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
    assert written["work_authorization"]["legally_authorized_to_work"] == ""
    assert written["work_authorization"]["require_sponsorship"] == ""


def test_not_sure_sponsorship_stays_unanswered(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    _walk(client, dict(FULL_WALK, sponsorship="not_sure"))

    state = onboarding.load_state()
    assert state.answers["sponsorship"] == "not_sure"
    profile, _ = onboarding.normalize_profile({}, state.answers)
    assert profile["work_authorization"]["require_sponsorship"] == ""
    assert "Sponsorship needed (yes/no)" in onboarding.missing_review_fields(profile)


def test_answers_win_over_agent_json(app_dir):
    agent_json = {"_applypilot_profile": {"work_authorization": {
        "legally_authorized_to_work": "Yes", "require_sponsorship": "No"}},
        "_applypilot_search": {}}
    answers = {"work_authorized": "no", "sponsorship": "not_sure"}
    profile, _ = onboarding.normalize_profile(agent_json, answers)
    assert profile["work_authorization"]["legally_authorized_to_work"] == "No"
    assert profile["work_authorization"]["require_sponsorship"] == ""
    assert "Sponsorship needed (yes/no)" in onboarding.missing_review_fields(profile)


def test_no_answers_means_no_gating_values(app_dir):
    profile, _ = onboarding.normalize_profile({}, {})
    assert profile["work_authorization"]["legally_authorized_to_work"] == ""
    assert profile["work_authorization"]["require_sponsorship"] == ""
    missing = onboarding.missing_review_fields(profile)
    assert missing[:2] == ["Work authorization (yes/no)", "Sponsorship needed (yes/no)"]


def test_agent_json_fills_what_the_wizard_does_not_own(app_dir):
    agent_json = {"_applypilot_profile": {"personal": {"email": "agent@example.com"}},
                  "_applypilot_search": {"location": "Denver, CO"}}
    answers = {"full_name": "Test Person", "email": "mine@example.com"}
    profile, meta = onboarding.normalize_profile(agent_json, answers)
    assert profile["personal"]["full_name"] == "Test Person"   # wizard
    assert profile["personal"]["email"] == "mine@example.com"  # wizard wins
    assert meta["location"] == "Denver, CO"                    # agent fills


def test_agent_json_cannot_override_recorded_answers(app_dir, fake_claude, monkeypatch):
    """End-to-end guard: the agent emits a fenced profile claiming Yes while
    the recorded answers say no / not sure — the answers stay authoritative."""
    patch_gates(monkeypatch)
    onboarding.start_interview("claude")
    onboarding.record_answer("work_authorized", value="no")
    onboarding.record_answer("sponsorship", value="not_sure")
    onboarding.send_message("AGENT_JSON_FALLBACK please complete my profile")
    state = onboarding.load_state()
    assert state.phase == onboarding.REVIEW  # the agent's block completed the draft
    assert state.profile_draft["work_authorization"]["legally_authorized_to_work"] == "No"
    assert state.profile_draft["work_authorization"]["require_sponsorship"] == ""


# ---------------------------------------------------------------------------
# Service-level validation

def test_record_answer_requires_an_active_interview_and_a_listed_choice(
        app_dir, fake_llm, monkeypatch):
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    with pytest.raises(onboarding.OnboardingError):
        onboarding.record_answer("work_authorized", value="yes")
    onboarding.save_state(onboarding.InterviewState(phase=onboarding.INTERVIEW, backend="llm"))
    with pytest.raises(onboarding.OnboardingError):
        onboarding.record_answer("no_such_question", value="yes")
    with pytest.raises(onboarding.OnboardingError):
        onboarding.record_answer("work_authorized", value="perhaps")
    assert onboarding.load_state().answers == {}


def test_answers_and_mode_persist_in_onboarding_state(app_dir, fake_llm, monkeypatch):
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    onboarding.start_interview("llm")
    onboarding.record_answer("work_authorized", value="yes")
    persisted = json.loads(config.ONBOARDING_STATE_PATH.read_text(encoding="utf-8"))
    assert persisted["answers"] == {"work_authorized": "yes"}
    assert persisted["mode"] == onboarding.MODE_CHOICES
    reloaded = onboarding.load_state()
    assert reloaded.answers == {"work_authorized": "yes"}
    assert reloaded.mode == onboarding.MODE_CHOICES


# ---------------------------------------------------------------------------
# Agent-led mode ("let the agent ask")

def test_agent_mode_hands_the_agenda_back_and_resumes(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    _answer(client, "work_authorized", value="yes")

    page = client.post("/onboarding/agent-mode", data={"enabled": "1"})
    assert page.status_code == 200
    state = onboarding.load_state()
    assert state.mode == onboarding.MODE_AGENT
    assert 'name="question_id"' not in page.text  # no question buttons
    assert "Back to guided questions" in page.text

    # Free-form chat works in agent mode and records nothing in answers.
    page = client.post("/onboarding/send", data={"text": "I have a Master's degree"})
    assert page.status_code == 200
    state = onboarding.load_state()
    assert state.answers == {"work_authorized": "yes"}
    assert any(m["role"] == "user" and m["content"] == "I have a Master's degree"
               for m in state.messages)

    # Back to buttons: the agenda resumes at the NEXT unanswered question.
    page = client.post("/onboarding/agent-mode", data={"enabled": "0"})
    assert page.status_code == 200
    state = onboarding.load_state()
    assert state.mode == onboarding.MODE_CHOICES
    assert _qid(page.text) == "sponsorship"


def test_agent_mode_directive_reaches_backends_but_not_the_transcript(app_dir, fake_llm, monkeypatch):
    fake = fake_llm
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    state = onboarding.start_interview("llm")
    assert state.mode == onboarding.MODE_CHOICES

    onboarding.set_agent_led(True)
    directive = fake.calls[-1][-1]["content"]
    assert "agent-led questions" in directive
    assert "Are you legally authorized" in directive  # the still-open fields
    state = onboarding.load_state()
    assert not any("agent-led questions" in m["content"] for m in state.messages)
    assert any(m["role"] == "assistant" for m in state.messages)  # the ack landed

    onboarding.set_agent_led(False)
    directive = fake.calls[-1][-1]["content"]
    assert "switched back to the guided questions" in directive
    state = onboarding.load_state()
    assert not any("switched back to the guided" in m["content"] for m in state.messages)


# ---------------------------------------------------------------------------
# The CLI backend runs the same agenda

def test_claude_backend_walk_sends_labels_as_prompts(app_dir, fake_claude, monkeypatch):
    patch_gates(monkeypatch)
    state = onboarding.start_interview("claude")
    assert state.backend == "claude"

    for spec in onboarding.QUESTION_SPECS:
        if spec.id == "location":
            continue  # never rendered: remote-only
        raw = STORED[spec.id]
        if not raw:
            onboarding.record_answer(spec.id, skip=True)
        elif any(c.value == raw for c in spec.choices):
            onboarding.record_answer(spec.id, value=raw)
        else:
            onboarding.record_answer(spec.id, free_text=raw)

    state = onboarding.load_state()
    assert state.backend == "claude"  # no fallback fired
    assert state.phase == onboarding.REVIEW
    assert state.answers == STORED
    prompts = [json.loads(line)["prompt"] for line in fake_claude.read_text().splitlines()]
    assert prompts[1:] == _expected_ack_prompts()
    resumes = [json.loads(line)["resume"] for line in fake_claude.read_text().splitlines()]
    assert all(r == "fake-session-1" for r in resumes[1:])  # session continuity


# ---------------------------------------------------------------------------
# PDF resume extraction

def _pdf_with_text(text):
    """A minimal one-page PDF whose content stream prints `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
         b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f\n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n\n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def test_pdf_resume_is_extracted_into_pending_context(app_dir, fake_llm, monkeypatch):
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    onboarding.save_state(onboarding.InterviewState(phase=onboarding.INTERVIEW, backend="llm"))
    onboarding.add_resume(pdf_bytes=_pdf_with_text("Senior Data Engineer, eight years of Python."),
                          filename="me.pdf")
    state = onboarding.load_state()
    assert "Senior Data Engineer" in state.pending_context
    assert config.RESUME_PATH.read_text(encoding="utf-8").startswith("Senior Data Engineer")
    notes = " ".join(m["content"] for m in state.messages if m["role"] == "note")
    assert "extracted" in notes


def test_unreadable_pdf_falls_back_to_the_paste_note(app_dir, fake_llm, monkeypatch):
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    onboarding.save_state(onboarding.InterviewState(phase=onboarding.INTERVIEW, backend="llm"))
    onboarding.add_resume(pdf_bytes=b"this is not a pdf", filename="broken.pdf")
    state = onboarding.load_state()
    assert state.pending_context == ""
    assert not config.RESUME_PATH.exists()
    notes = " ".join(m["content"] for m in state.messages if m["role"] == "note")
    assert "couldn't be read" in notes


def test_pdf_resume_route_extracts(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    page = client.post(
        "/onboarding/resume",
        files={"resume_file": ("me.pdf", _pdf_with_text("Ops Manager, five years."), "application/pdf")})
    assert page.status_code == 200
    state = onboarding.load_state()
    assert "Ops Manager" in state.pending_context



# ---------------------------------------------------------------------------
# The wizard screens: choice buttons first, "Something else…" for typing

def test_guided_screen_renders_choices_and_something_else(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    page = _start(client)

    # First question (work_authorized): buttons, no free text allowed.
    assert "choice-grid" in page and 'data-question-id="work_authorized"' in page
    assert "Something else…" not in page

    # The permit question carries both buttons and the typed escape hatch.
    _answer(client, "work_authorized", value="yes")
    page = client.get("/onboarding/chat").text  # sponsorship is current
    _answer(client, "sponsorship", value="no")
    page = client.get("/onboarding/chat").text  # permit_type is current
    assert 'data-question-id="permit_type"' in page
    assert "Something else…" in page  # the collapsed typed-answer box
    page = _answer(client, "permit_type", value="Citizen")
    _answer(client, "work_style", value="remote")
    page = _answer(client, "currency", value="USD")
    assert "Something else…" in page  # currency also allows a typed code

    # A free-text-only question renders the widget directly — typing is
    # the answer, no "Something else…" gate in the way.
    page = _answer(client, "salary_band", free="145000")
    page = _answer(client, "start_date", value="Immediately")
    page = _answer(client, "education", value="Bachelor's")
    page = _answer(client, "experience_years", value="3–5")
    page = _answer(client, "target_roles", free="Data Engineer\nAnalytics Engineer")
    assert "Something else…" not in page  # no choices on target_roles
    assert "<textarea" in page


def test_free_text_widget_matches_the_kind(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    for _ in range(8):  # walk to salary_band: choices + a number widget
        page = client.get("/onboarding/chat").text
        qid = _qid(page)
        if qid == "salary_band":
            break
        spec = onboarding.QUESTIONS[qid]
        _answer(client, qid, value=spec.choices[0].value)
    assert '<input type="number"' in page

    # The date kind renders a date input (start_date question).
    page = _answer(client, "salary_band", free="145000")
    page = _answer(client, "start_date", value="Immediately")
    page = _answer(client, "education", value="Bachelor's")
    page = _answer(client, "experience_years", value="3–5")
    page = _answer(client, "target_roles", free="Data Engineer")
    assert "<textarea" in page  # multiline kind for the skills/roles questions


def test_choice_answers_stay_out_of_the_transcript_typed_chat_shows(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    page = _answer(client, "work_authorized", value="yes")
    # The picked answer renders as the clicked button, not a chat bubble.
    assert "chat-msg user" not in page
    assert "choice-grid" in page

    # A typed question in the Ask-the-agent box is a real chat turn —
    # it stays in the log even in guided mode.
    page = client.post("/onboarding/send", data={"text": "Why does this matter for the search?"}).text
    assert "Why does this matter for the search?" in page
    assert "chat-msg user" in page



# ---------------------------------------------------------------------------
# The review form: prefilled widgets, needs-answer chips, quiet edits

def test_review_form_renders_prefilled_with_needs_answer_chips(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    walk = {**FULL_WALK, "education": None, "linkedin_url": None}
    _walk(client, walk)
    page = client.get("/onboarding/chat").text

    assert "Review your profile" in page
    # Answered questions prefill their widgets; skipped ones stay empty.
    assert '<option value="yes" selected>' in page
    assert 'id="answer-salary_band"' in page and 'value="120000"' in page
    assert 'id="answer-education"' in page and 'selected>— not answered —' in page
    assert 'id="answer-linkedin_url"' in page and 'value=""' in page

    # Needs-answer chips link to the flagged fields (education feeds a
    # flagged path; the optional LinkedIn does not).
    assert 'href="#review-education"' in page
    assert 'href="#review-linkedin_url"' not in page
    # The answered required fields raise no chips.
    assert 'href="#review-work_authorized"' not in page


def test_review_update_route_validates_and_saves_quietly(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    _walk(client, FULL_WALK)

    # A value outside the choice list is rejected — nothing stored.
    page = client.post("/onboarding/review", data={"answer_work_authorized": "hacked"}).text
    assert "alert error" in page
    assert onboarding.load_state().answers["work_authorized"] == "yes"

    # A valid edit saves and stays in review (no agent turn ran).
    page = client.post("/onboarding/review", data={"answer_education": "Master's"}).text
    assert onboarding.load_state().phase == onboarding.REVIEW
    assert onboarding.load_state().answers["education"] == "Master's"
    assert '<option value="Master&#39;s" selected>' in page
    assert 'href="#review-education"' not in page  # the chip cleared


def test_review_cleared_answer_stays_empty_through_confirm(portal_client, fake_llm, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    _start(client)
    # Skip the GATED question: the fabrication guard holds it at "" through
    # confirm — the agent's extraction may never fill a yes/no the user
    # declined (PR #22 guard, confirm behavior unchanged).
    walk = {**FULL_WALK, "work_authorized": None}
    _walk(client, walk)
    assert onboarding.load_state().answers["work_authorized"] == ""

    # The review form renders the gated widget empty — never prefilled —
    # and flags it with a needs-answer chip.
    page = client.get("/onboarding/chat").text
    assert 'href="#review-work_authorized"' in page
    select = re.search(r'<select id="answer-work_authorized"[^>]*>.*?</select>', page, re.S)
    assert select
    # Exactly one selected option, and it is the empty "not answered" one.
    assert select.group(0).count(" selected") == 1
    assert '<option value="" selected>— not answered —</option>' in select.group(0)

    client.post("/onboarding/confirm")
    profile = json.loads(config.PROFILE_PATH.read_text())
    assert profile["work_authorization"]["legally_authorized_to_work"] == ""
