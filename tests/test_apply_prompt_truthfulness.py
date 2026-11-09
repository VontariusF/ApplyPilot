"""Apply-prompt truthfulness and salary-currency persistence (sweep
art_kvtM7h6B, wave 2).

The apply prompt speaks only recorded facts: no asserted screening
answers with no profile backing, no bare empty-string interpolations,
no crash on a nameless legacy profile — and a salary floor accepted
from a currency-labeled band never reaches the prompt without its
currency.
"""

import json

import pytest

from kestrel import config, onboarding
from kestrel.apply import prompt as prompt_mod
from kestrel.portal import app as portal_app


# ---------------------------------------------------------------------------
# Profile-summary: the screening boilerplate asserts nothing


def test_summary_asserts_no_screening_answers_without_profile_backing():
    summary = prompt_mod._build_profile_summary({
        "personal": {"full_name": "Test User", "email": "t@example.com",
                     "phone": "5551234567"},
        "work_authorization": {},
        "compensation": {},
        "experience": {},
        "availability": {},
        "eeo_voluntary": {},
    })
    # The old asserted facts are gone from the summary...
    for asserted in ("Age 18+: Yes", "Background Check: Yes", "Felony: No",
                     "Previously Worked Here: No", "How Heard: Online Job Board"):
        assert asserted not in summary
    # ...replaced by answer-from-profile instruction.
    assert "Never assume" in summary
    assert "How Heard" in summary


# ---------------------------------------------------------------------------
# Screening section: truthiness defaults, not bare empty strings


def _screening_profile(personal=None, experience=None):
    return {
        "personal": personal or {},
        "experience": experience or {},
        "work_authorization": {},
    }


def test_screening_section_interpolates_fallbacks_not_empties():
    section = prompt_mod._build_screening_section(_screening_profile())
    assert "lives in their city" in section
    assert "a professional with multiple years experience" in section


def test_screening_section_uses_recorded_values_when_present():
    section = prompt_mod._build_screening_section(_screening_profile(
        personal={"city": "Toronto"},
        experience={"target_role": "Data Engineer",
                    "years_of_experience_total": "6–10"},
    ))
    assert "lives in Toronto" in section
    assert "a Data Engineer with 6–10 years experience" in section


def test_screening_role_falls_back_within_the_experience_section():
    # The onboarding schema never writes personal.current_job_title, so
    # the old fallback chained through a key that does not exist; the
    # experience section's own current_title is the real fallback.
    section = prompt_mod._build_screening_section(_screening_profile(
        experience={"current_title": "Platform Engineer"}))
    assert "a Platform Engineer" in section


# ---------------------------------------------------------------------------
# A nameless profile degrades readably instead of crashing the run


@pytest.fixture
def tailored_resume(app_dir):
    pdf = app_dir / "tailored_resumes" / "job1.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-fake")
    pdf.with_suffix(".txt").write_text("Tailored resume text", encoding="utf-8")
    return pdf


def make_job(resume_pdf):
    return {
        "url": "https://jobs.example.com/listing/123",
        "title": "Backend Engineer",
        "site": "example",
        "application_url": "https://jobs.example.com/apply/123",
        "fit_score": 9,
        "tailored_resume_path": str(resume_pdf),
    }


def _write_profile(app_dir, **personal_overrides):
    profile = {
        "personal": {"full_name": "", "preferred_name": "", "email": "t@example.com",
                     "phone": "5551234567", "password": ""},
        "work_authorization": {},
        "availability": {},
        "compensation": {},
        "experience": {},
        "eeo_voluntary": {},
    }
    profile["personal"].update(personal_overrides)
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    return profile


def test_build_prompt_survives_a_nameless_profile(app_dir, tailored_resume):
    _write_profile(app_dir)
    prompt = prompt_mod.build_prompt(job=make_job(tailored_resume),
                                     tailored_resume="resume text")
    assert isinstance(prompt, str) and prompt  # built, no IndexError


def test_build_prompt_prefers_the_recorded_preferred_name(app_dir, tailored_resume):
    _write_profile(app_dir, full_name="Test User", preferred_name="T")
    prompt = prompt_mod.build_prompt(job=make_job(tailored_resume),
                                     tailored_resume="resume text")
    assert 'Preferred name = T' in prompt


def test_confirm_blocks_a_nameless_profile_with_a_readable_message(app_dir):
    state = onboarding.InterviewState(
        phase=onboarding.REVIEW,
        profile_draft={"_kestrel_profile": {"personal": {"full_name": ""}}},
    )
    onboarding.save_state(state)
    with pytest.raises(onboarding.OnboardingError, match="full name"):
        onboarding.confirm_profile()


def test_confirm_accepts_a_named_profile(app_dir):
    state = onboarding.InterviewState(
        phase=onboarding.REVIEW,
        profile_draft={"_kestrel_profile": {"personal": {"full_name": "Test User"}}},
    )
    onboarding.save_state(state)
    profile = onboarding.confirm_profile()
    assert profile["personal"]["full_name"] == "Test User"


# ---------------------------------------------------------------------------
# The agent's currency-conversion note reaches the apply prompt


def test_conversion_note_flows_through_normalize_profile_into_the_prompt():
    draft = {"_kestrel_profile": {"compensation": {
        "salary_expectation": "90000", "salary_currency": "GBP",
        "currency_conversion_note": "Compare EUR postings at 1.15 GBP/EUR"}}}
    profile, _ = onboarding.normalize_profile(draft, {})
    note = profile["compensation"]["currency_conversion_note"]
    assert note == "Compare EUR postings at 1.15 GBP/EUR"
    section = prompt_mod._build_salary_section({"compensation": profile["compensation"]})
    assert "1.15 GBP/EUR" in section


def test_wizard_profile_normalizes_with_an_empty_note():
    profile, _ = onboarding.normalize_profile({}, {"salary_band": "90000"})
    assert profile["compensation"]["currency_conversion_note"] == ""


# ---------------------------------------------------------------------------
# The jobs-board min_salary chip carries no currency symbol


def test_min_salary_chip_states_the_number_without_a_dollar_sign():
    view = portal_app._normalize_view({"min_salary": "90000"})
    filters = portal_app._board_active_filters(view)
    chip = next(f for f in filters if f["label"].startswith("Pay at least"))
    assert chip["label"] == "Pay at least 90,000+"
    assert "$" not in chip["label"]


# ---------------------------------------------------------------------------
# Accepting a band button settles the currency question


def _band_ready_state(country: str) -> onboarding.InterviewState:
    """An INTERVIEW-state one click from the salary question: everything
    answered except currency (skipped — recorded empty) and salary_band."""
    state = onboarding.InterviewState(phase=onboarding.INTERVIEW)
    state.answers.update({q.id: "x" for q in onboarding.QUESTION_SPECS
                          if q.id not in ("salary_band", "currency", "location")})
    state.answers["country"] = country
    state.answers["currency"] = ""  # the skip record
    state.answers["location"] = "x"
    return state


def test_accepted_band_stamps_the_derived_currency(app_dir, fake_llm):
    onboarding.save_state(_band_ready_state("Canada"))
    band_value = onboarding.salary_band_choices("CAD", "mid")[0].value
    onboarding.record_answer("salary_band", value=band_value)
    state = onboarding.load_state()
    assert state.answers["currency"] == "CAD"
    profile, _ = onboarding.normalize_profile(state.profile_draft or {}, state.answers)
    assert profile["compensation"]["salary_currency"] == "CAD"
    assert profile["compensation"]["salary_expectation"] == band_value
    # ...and the salary section then speaks a unit-ed minimum.
    section = prompt_mod._build_salary_section({"compensation": profile["compensation"]})
    assert band_value in section and "CAD" in section


def test_free_text_salary_stays_unit_less(app_dir, fake_llm):
    onboarding.save_state(_band_ready_state("Canada"))
    onboarding.record_answer("salary_band", free_text="72000")
    state = onboarding.load_state()
    # A typed number can be genuinely unit-less: no currency is invented,
    # and the no-currency ⇒ no-salary prompt rule keeps covering it.
    assert state.answers["currency"] == ""
    assert state.answers["salary_band"] == "72000"


def test_accepted_band_never_overrides_a_recorded_currency(app_dir, fake_llm):
    state = _band_ready_state("Canada")
    state.answers["currency"] = "USD"  # explicitly recorded despite CAD country
    onboarding.save_state(state)
    band_value = onboarding.salary_band_choices("USD", "mid")[0].value
    onboarding.record_answer("salary_band", value=band_value)
    assert onboarding.load_state().answers["currency"] == "USD"
