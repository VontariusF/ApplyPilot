"""Validator unit tests: required fields and profile-grounded fabrication.

Live-verification F-5 / F-6 (art_M7Xt2rnH): the tailoring prompt tells the
model to drop irrelevant projects, so an honest empty projects list must
validate (F-5); and the fabrication watchlist must never flag a skill the
user actually listed in their profile's skills_boundary (F-6) — the
profile-grounded helper existed but was never wired.
"""

from applypilot.scoring.validator import (
    validate_json_fields,
    validate_tailored_resume,
)


def _data(**overrides):
    """A minimal tailored-resume payload that passes every other check."""
    data = {
        "title": "Senior Backend Engineer",
        "summary": "Backend engineer with seven years of API work.",
        "skills": {"backend": "Python, SQL"},
        "experience": [
            {"header": "Acme Corp", "bullets": ["Shipped the billing API."]},
        ],
        "projects": [
            {"header": "Payments", "bullets": ["Built a ledger."]},
        ],
        "education": "State University, BSc.",  # a string (F3 type gate)
    }
    data.update(overrides)
    return data


def _profile(**overrides):
    profile = {
        "personal": {"full_name": "", "email": "", "phone": ""},
        "skills_boundary": {},
        "resume_facts": {},
    }
    profile.update(overrides)
    return profile


# ── F-5: an honest empty projects list is accepted ────────────────────────

def test_empty_projects_list_is_accepted():
    """The prompt instructs dropping irrelevant projects; the validator
    must agree instead of rejecting the honest empty list as 'missing'."""
    result = validate_json_fields(_data(projects=[]), _profile())
    assert result["passed"], result["errors"]


def test_missing_projects_key_is_still_rejected():
    data = _data()
    del data["projects"]
    result = validate_json_fields(data, _profile())
    assert not result["passed"]
    assert any("projects" in e for e in result["errors"])


def test_non_list_projects_is_still_rejected():
    result = validate_json_fields(_data(projects="dropped all"), _profile())
    assert not result["passed"]
    assert any("projects" in e for e in result["errors"])


# ── F-6: user-listed skills are never flagged as fabricated ───────────────

def test_listed_skill_not_flagged_in_json_fields():
    profile = _profile(skills_boundary={"frameworks": ["Django", "Python"]})
    data = _data(skills={"frameworks": "Django, Python, FastAPI"})
    result = validate_json_fields(data, profile)
    assert result["passed"], result["errors"]


def test_unlisted_watchlist_skill_still_flagged_in_json_fields():
    profile = _profile(skills_boundary={"frameworks": ["FastAPI"]})
    data = _data(skills={"frameworks": "FastAPI, Django"})
    result = validate_json_fields(data, profile)
    assert not result["passed"]
    assert any("django" in e for e in result["errors"])


RESUME = """SUMMARY
Backend engineer.

TECHNICAL SKILLS
Python, Django, SQL

EXPERIENCE
Acme Corp
Built the billing API.

PROJECTS
None.

EDUCATION
State University, BSc.
"""


def test_listed_skill_not_flagged_in_tailored_resume():
    profile = _profile(skills_boundary={"frameworks": ["Django"]})
    result = validate_tailored_resume(RESUME, profile)
    assert result["passed"], result["errors"]


def test_unlisted_watchlist_skill_still_flagged_in_tailored_resume():
    profile = _profile(skills_boundary={"frameworks": ["FastAPI"]})
    result = validate_tailored_resume(RESUME, profile)
    assert not result["passed"]
    assert any("django" in e.lower() for e in result["errors"])


def test_listed_skill_not_warned_when_absent_from_original():
    """Check 8 compares against the original resume; a boundary skill the
    model echoes is a fact, not a 'new tool appeared' warning."""
    profile = _profile(skills_boundary={"frameworks": ["Django"]})
    result = validate_tailored_resume(RESUME, profile,
                                      original_text="original without it")
    assert not any("New tool/skill appeared" in w for w in result["warnings"])


def test_substring_boundary_matches_still_exempt():
    """Matching mirrors the watchlist's own substring semantics: a user who
    lists 'go' is not flagged for 'golang' and vice versa."""
    profile = _profile(skills_boundary={"languages": ["go"]})
    resume = RESUME.replace("Django", "Golang")
    result = validate_tailored_resume(resume, profile)
    assert result["passed"], result["errors"]
