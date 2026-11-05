"""Currency- and level-aware salary handling (spec art_ti3XJuNr, currency-real salary).

Wizard bands derive from the captured currency and experience level — never
a single hardcoded USD ladder — and the apply prompt speaks only recorded
answers: no invented floors, no wrong currency symbols, and no salary
figure at all when the profile records none.
"""

import json

import pytest

from applypilot import config
from applypilot import onboarding
from applypilot.apply import prompt as prompt_mod


def make_job(resume_pdf):
    return {
        "url": "https://jobs.example.com/listing/123",
        "title": "Backend Engineer",
        "site": "example",
        "application_url": "https://jobs.example.com/apply/123",
        "fit_score": 9,
        "tailored_resume_path": str(resume_pdf),
    }


def _labels(choices):
    return [c.label for c in choices]


def _values(choices):
    return [c.value for c in choices]


# ---------------------------------------------------------------------------
# salary_band_choices: the currency x level matrix


def test_usd_bands_scale_by_level():
    entry = _values(onboarding.salary_band_choices("USD", "entry"))
    mid = _values(onboarding.salary_band_choices("USD", "mid"))
    senior = _values(onboarding.salary_band_choices("USD", "senior"))
    assert entry != mid != senior
    assert int(entry[0]) < int(mid[0]) < int(senior[0])


def test_experience_band_answers_map_to_levels():
    assert onboarding.experience_level({"experience_years": "0–2"}) == "entry"
    assert onboarding.experience_level({"experience_years": "3–5"}) == "mid"
    assert onboarding.experience_level({"experience_years": "6–10"}) == "senior"
    assert onboarding.experience_level({"experience_years": "10+"}) == "senior"
    assert onboarding.experience_level({}) == "mid"  # unanswered stays mid


@pytest.mark.parametrize("currency", ["USD", "GBP", "EUR", "CAD", "INR"])
def test_every_offered_currency_has_full_level_sets(currency):
    for level in ("entry", "mid", "senior"):
        choices = onboarding.salary_band_choices(currency, level)
        assert len(choices) >= 3
        values = [int(c.value) for c in choices]
        assert values == sorted(values)  # floors ascend; the last band is open


@pytest.mark.parametrize("currency,symbol", [("USD", "$"), ("GBP", "£"), ("EUR", "€"), ("CAD", "C$"), ("INR", "₹")])
def test_band_labels_use_their_own_currency_symbol(currency, symbol):
    labels = _labels(onboarding.salary_band_choices(currency, "mid"))
    assert all(symbol in label for label in labels)
    if currency != "USD":
        # No USD-style dollar prefix — CAD's C$ keeps its own.
        assert all(not label.startswith("$") for label in labels)


def test_inr_bands_speak_lakhs_not_thousands():
    labels = _labels(onboarding.salary_band_choices("INR", "entry"))
    assert labels[0] == "₹4L – ₹6L"
    assert "₹400k" not in " ".join(labels)


def test_unknown_currency_gets_code_labeled_generic_bands():
    labels = _labels(onboarding.salary_band_choices("AUD", "mid"))
    assert labels[0] == "AUD 50k – AUD 80k"
    assert all("$" not in label for label in labels)


def test_no_currency_means_no_bands_at_all():
    assert onboarding.salary_band_choices(None, "mid") == ()
    assert onboarding.salary_band_choices("", "senior") == ()


def test_level_key_defaults_and_lead_maps_to_senior():
    assert onboarding.salary_level_key("lead") == "senior"
    assert onboarding.salary_level_key("unknown") == "mid"
    assert onboarding.salary_level_key(None) == "mid"


# ---------------------------------------------------------------------------
# The wizard flow: currency capture, country derivation, recorded precedence


def test_non_usd_location_yields_non_usd_bands():
    answers = {"currency": "INR", "experience_years": "6–10"}
    choices = onboarding.resolved_choices(onboarding.QUESTIONS["salary_band"], answers)
    assert "₹" in "".join(_labels(choices)) and "$" not in "".join(_labels(choices))


def test_recorded_currency_wins_over_country_derivation():
    answers = {"country": "India", "currency": "USD", "experience_years": "3–5"}
    choices = onboarding.resolved_choices(onboarding.QUESTIONS["salary_band"], answers)
    assert all("$" in c.label for c in choices)


def test_country_derives_the_currency_when_the_question_was_skipped():
    answers = {"country": "United Kingdom", "experience_years": "0–2"}
    choices = onboarding.resolved_choices(onboarding.QUESTIONS["salary_band"], answers)
    labels = _labels(choices)
    assert labels
    assert all("£" in label for label in labels)


def test_unknown_location_and_currency_defer_the_salary_question():
    state = onboarding.InterviewState(phase=onboarding.INTERVIEW)
    assert "salary_band" not in [s.id for s in onboarding.remaining_questions(state)]


def test_decided_country_opens_the_salary_question_even_with_currency_skipped():
    state = onboarding.InterviewState(phase=onboarding.INTERVIEW)
    # Everything else already answered; currency was skipped — a skip
    # records an empty answer. Canada still opens the question, with CAD bands.
    state.answers.update({q.id: "x" for q in onboarding.QUESTION_SPECS
                          if q.id not in ("salary_band", "currency", "location")})
    state.answers["country"] = "Canada"
    state.answers["currency"] = ""  # the skip record
    assert "salary_band" in [s.id for s in onboarding.remaining_questions(state)]
    question = onboarding.wizard_question(state)
    assert question.id == "salary_band"
    assert all("C$" in c.label for c in question.choices)


def test_recorded_salary_answer_beats_agent_extraction():
    answers = {"salary_band": "90000"}
    draft = {"_applypilot_profile": {"compensation": {
        "salary_expectation": "999999", "salary_currency": "EUR"}}}
    profile, _ = onboarding.normalize_profile(draft, answers)
    assert profile["compensation"]["salary_expectation"] == "90000"


def test_normalize_never_defaults_the_currency_to_usd():
    profile, _ = onboarding.normalize_profile(
        {"_applypilot_profile": {"compensation": {"salary_expectation": "90000"}}}, {})
    assert profile["compensation"]["salary_currency"] == ""
    assert profile["compensation"]["salary_expectation"] == "90000"


# ---------------------------------------------------------------------------
# The apply prompt: recorded answers only


@pytest.fixture
def profile_without_salary(app_dir):
    profile = {
        "personal": {"full_name": "Test User", "email": "t@example.com",
                     "phone": "5551234567", "password": ""},
        "work_authorization": {"legally_authorized_to_work": True,
                               "require_sponsorship": False},
        "compensation": {},
    }
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    return profile


@pytest.fixture
def profile_inr(app_dir):
    profile = {
        "personal": {"full_name": "Test User", "email": "t@example.com",
                     "phone": "5551234567", "password": ""},
        "work_authorization": {"legally_authorized_to_work": True,
                               "require_sponsorship": False},
        "compensation": {"salary_expectation": "2500000", "salary_currency": "INR"},
    }
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    return profile


@pytest.fixture
def tailored_resume(app_dir):
    pdf = app_dir / "tailored_resumes" / "job1.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-fake")
    pdf.with_suffix(".txt").write_text("Tailored resume text", encoding="utf-8")
    return pdf


def test_prompt_states_no_salary_with_nothing_recorded(app_dir, profile_without_salary, tailored_resume):
    prompt = prompt_mod.build_prompt(job=make_job(tailored_resume), tailored_resume="resume text")
    assert "Do not state an expected" in prompt
    assert "$110K" not in prompt and "110K" not in prompt


def test_prompt_no_salary_section_has_no_figure_at_all(app_dir, profile_without_salary, tailored_resume):
    prompt_mod.build_prompt(job=make_job(tailored_resume), tailored_resume="resume text")
    salary_section = prompt_mod._build_salary_section(
        {"compensation": {"salary_expectation": "", "salary_currency": ""}})
    assert not any(ch.isdigit() for ch in salary_section)


def test_prompt_uses_recorded_currency_without_dollar_symbols(app_dir, profile_inr, tailored_resume):
    prompt = prompt_mod.build_prompt(job=make_job(tailored_resume), tailored_resume="resume text")
    assert "2500000 INR" in prompt
    assert "$" not in prompt_mod._build_salary_section({"compensation": profile_inr["compensation"]})


def test_prompt_summary_line_omits_unset_salary(app_dir, profile_without_salary, tailored_resume):
    prompt = prompt_mod.build_prompt(job=make_job(tailored_resume), tailored_resume="resume text")
    assert "Salary Expectation:" not in prompt


def test_no_senior_minimum_rule_anywhere():
    section = prompt_mod._build_salary_section(
        {"compensation": {"salary_expectation": "90000", "salary_currency": "USD"}})
    assert "Senior" not in section and "110K" not in section


def test_recorded_range_is_spoken_but_never_invented():
    comp = {"salary_expectation": "90000", "salary_currency": "GBP",
            "salary_range_min": "90000", "salary_range_max": "110000"}
    section = prompt_mod._build_salary_section({"compensation": comp})
    assert "90000-110000 GBP" in section
    invented = prompt_mod._build_salary_section(
        {"compensation": {"salary_expectation": "90000", "salary_currency": "GBP"}})
    assert "110000" not in invented  # the old floor+20000 range is gone


def test_ack_prompt_carries_the_derived_band_options():
    answers = {"currency": "INR", "experience_years": "3–5"}
    spec = onboarding.QUESTIONS["salary_band"]
    prompt = onboarding.answer_ack_prompt(spec, "₹12L – ₹18L",
                                          onboarding.resolved_choices(spec, answers))
    assert '"₹12L – ₹18L"' in prompt and '"₹18L – ₹24L"' in prompt
