"""S1: the job-site password must never enter an agent prompt or a log file.

Root fix: build_prompt references the browser profile's credentials instead of
embedding the password. Boundary fix: prompt files written to ~/.applypilot/
logs/ pass through redact_prompt_for_log.
"""

import json

import pytest

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.security import REDACTED

PASSWORD = "S3cr3t-Passw0rd-XYZ"


@pytest.fixture
def profile_with_password(app_dir):
    profile = {
        "personal": {
            "full_name": "Test User",
            "email": "test@example.com",
            "phone": "5551234567",
            "password": PASSWORD,
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "compensation": {"salary_expectation": "100000"},
    }
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    return profile


@pytest.fixture
def tailored_resume(app_dir):
    """Resume files the prompt builder picks up (pdf with txt sibling)."""
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


def test_build_prompt_excludes_password(app_dir, profile_with_password, tailored_resume):
    prompt = prompt_mod.build_prompt(
        job=make_job(tailored_resume), tailored_resume="resume text"
    )
    assert PASSWORD not in prompt
    # The agent still gets the email and the credential-store guidance.
    assert "test@example.com" in prompt
    assert "credentials saved in this browser profile" in prompt


def test_build_prompt_without_password_still_builds(app_dir, tailored_resume):
    profile = {
        "personal": {
            "full_name": "Test User",
            "email": "t@example.com",
            "phone": "5551234567",
            "password": "",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "compensation": {"salary_expectation": "100000"},
    }
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    prompt = prompt_mod.build_prompt(
        job=make_job(tailored_resume), tailored_resume="resume text"
    )
    assert "Test User" in prompt


def test_redact_prompt_for_log_masks_password(app_dir, profile_with_password):
    text = f"Try sign in: x / {PASSWORD}"
    redacted = prompt_mod.redact_prompt_for_log(text)
    assert PASSWORD not in redacted
    assert REDACTED in redacted


def test_redact_prompt_for_log_missing_profile_is_noop(app_dir):
    # Fresh app dir: no profile.json -- nothing to redact, text passes through.
    assert prompt_mod.redact_prompt_for_log("unchanged") == "unchanged"


def test_gen_prompt_secret_in_zero_output_files(app_dir, profile_with_password, tailored_resume):
    """The spec's S1 test: build a prompt from a profile with a password and
    run the gen-prompt write boundary -- the secret appears in zero output
    files under the app dir."""
    from applypilot import database
    from applypilot.apply import launcher

    database.init_db()
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO jobs (url, title, site, application_url, fit_score, tailored_resume_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "https://jobs.example.com/listing/123",
            "Backend Engineer",
            "example",
            "https://jobs.example.com/apply/123",
            9,
            str(tailored_resume),
        ),
    )
    conn.commit()

    prompt_file = launcher.gen_prompt(target_url="https://jobs.example.com/listing/123")
    assert prompt_file is not None and prompt_file.exists()

    for path in app_dir.rglob("*"):
        if path.is_file() and path.name != "profile.json":
            # profile.json is the credential store itself (S2 gives it 0600);
            # every OTHER output file must be password-free.
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert PASSWORD not in content, f"password leaked into {path}"
