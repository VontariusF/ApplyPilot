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


def test_prompt_redaction_redacts_capsolver_key(app_dir, monkeypatch):
    """S2/F1: the CapSolver key the prompt embeds verbatim must not survive
    the log boundary -- asserted against the file on disk, not a return
    value."""
    capsolver_key = "CAP-TEST-abc123def456"
    monkeypatch.setenv("CAPSOLVER_API_KEY", capsolver_key)
    prompt = (
        "== CAPTCHA ==\nAPI key: CAP-TEST-abc123def456\nclientKey: 'CAP-TEST-abc123def456'"
    )
    config.ensure_dirs()
    log_file = config.LOG_DIR / "prompt_example_Test.txt"
    log_file.write_text(prompt_mod.redact_prompt_for_log(prompt), encoding="utf-8")

    on_disk = log_file.read_text(encoding="utf-8")
    assert capsolver_key not in on_disk
    assert REDACTED in on_disk


def test_prompt_redaction_reads_provider_keys_from_env_file(app_dir, monkeypatch):
    """A provider key present only in .env (this process may never have
    loaded it) is still redacted -- asserted on disk."""
    provider_key = "sk-test-provider-9876543210"
    config.ENV_PATH.write_text(f'LLM_API_KEY="{provider_key}"\n', encoding="utf-8")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    text = f"endpoint diagnostics: bearer {provider_key}"

    config.ensure_dirs()
    log_file = config.LOG_DIR / "prompt_example_Test.txt"
    log_file.write_text(prompt_mod.redact_prompt_for_log(text), encoding="utf-8")

    on_disk = log_file.read_text(encoding="utf-8")
    assert provider_key not in on_disk
    assert REDACTED in on_disk


def test_redact_prompt_for_log_missing_profile_still_redacts_env_secrets(
    app_dir, monkeypatch
):
    """Missing profile is no longer a pass-through: env secrets still get
    redacted (the CAPTCHA section embeds the CapSolver key without any
    profile)."""
    capsolver_key = "CAP-TEST-noprofile-777"
    monkeypatch.setenv("CAPSOLVER_API_KEY", capsolver_key)
    text = f"API key: {capsolver_key}"
    assert prompt_mod.redact_prompt_for_log(text) == f"API key: {REDACTED}"


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


def test_gen_prompt_capsolver_key_in_zero_output_files(
    app_dir, monkeypatch, profile_with_password, tailored_resume
):
    """The spec's S2/F1 test: a scored run's gen-prompt write boundary keeps
    the CapSolver key out of every output file under the app dir."""
    from applypilot import database
    from applypilot.apply import launcher

    capsolver_key = "CAP-TEST-harness-42"
    monkeypatch.setenv("CAPSOLVER_API_KEY", capsolver_key)

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
    assert prompt_file.name.startswith("prompt_")

    for path in app_dir.rglob("*"):
        if path.is_file() and path.name not in ("profile.json", ".env"):
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert capsolver_key not in content, f"CapSolver key leaked into {path}"
