"""S2 F3: worker upload files must be per-job, never per-user.

With --workers >= 2, every worker shared one upload path
(APPLY_WORKER_DIR/current/{name}_Resume.pdf). Two workers building prompts
for different jobs overwrote each other's tailored resume at that path, so
the agent uploaded whichever resume landed last to the WRONG employer.
Root fix: job-qualified upload filenames plus a byte-identity check on the
copy before the prompt references it.
"""

import json
import re
import threading

import pytest

from kestrel import config
from kestrel.apply import prompt as prompt_mod


@pytest.fixture
def profile(app_dir):
    profile = {
        "personal": {
            "full_name": "Test User",
            "email": "test@example.com",
            "phone": "5551234567",
        },
        "work_authorization": {},
        "compensation": {"salary_expectation": "100000"},
    }
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    return profile


def make_job(url: str, resume_pdf, cl_pdf=None) -> dict:
    job = {
        "url": url,
        "title": "Backend Engineer",
        "site": "example",
        "application_url": url + "/apply",
        "fit_score": 9,
        "tailored_resume_path": str(resume_pdf),
    }
    if cl_pdf:
        job["cover_letter_path"] = str(cl_pdf)
    return job


def tailored_files(app_dir, name: str, body: bytes):
    """A tailored resume pair (pdf + txt sibling) with distinct content."""
    tailored = app_dir / "tailored_resumes"
    tailored.mkdir(parents=True, exist_ok=True)
    pdf = tailored / f"{name}.pdf"
    pdf.write_bytes(body)
    pdf.with_suffix(".txt").write_text(body.decode(), encoding="utf-8")
    return pdf


def resume_path_from(prompt: str) -> str:
    match = re.search(r"Resume PDF \(upload this\): (.+)", prompt)
    assert match, "prompt must reference the upload resume path"
    return match.group(1).strip()


def test_upload_paths_are_per_job(app_dir, profile):
    """Different jobs get distinct upload files, each matching its own resume."""
    pdf_a = tailored_files(app_dir, "job_a", b"%PDF-resume-A")
    pdf_b = tailored_files(app_dir, "job_b", b"%PDF-resume-B")
    job_a = make_job("https://x.com/jobs/123", pdf_a)
    job_b = make_job("https://x.com/jobs/456", pdf_b)

    path_a = resume_path_from(prompt_mod.build_prompt(job=job_a, tailored_resume="A"))
    path_b = resume_path_from(prompt_mod.build_prompt(job=job_b, tailored_resume="B"))

    assert path_a != path_b
    assert open(path_a, "rb").read() == b"%PDF-resume-A"
    assert open(path_b, "rb").read() == b"%PDF-resume-B"


def test_concurrent_workers_upload_own_resume(app_dir, profile):
    """Two workers building prompts for different jobs concurrently each get
    their own tailored resume at the path their prompt references (the sweep's
    named regression test)."""
    pdf_a = tailored_files(app_dir, "job_a", b"%PDF-resume-A")
    pdf_b = tailored_files(app_dir, "job_b", b"%PDF-resume-B")
    job_a = make_job("https://x.com/jobs/123", pdf_a)
    job_b = make_job("https://x.com/jobs/456", pdf_b)
    barrier = threading.Barrier(2)
    results: dict[str, tuple[str, bytes]] = {}

    def build(job: dict, key: str, resume_text: str) -> None:
        barrier.wait()
        prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)
        path = resume_path_from(prompt)
        results[key] = (path, open(path, "rb").read())

    t1 = threading.Thread(target=build, args=(job_a, "a", "A"))
    t2 = threading.Thread(target=build, args=(job_b, "b", "B"))
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    assert results["a"][0] != results["b"][0]
    assert results["a"][1] == b"%PDF-resume-A"
    assert results["b"][1] == b"%PDF-resume-B"


def test_upload_job_key_prefers_application_url_and_is_stable(app_dir, profile):
    job = {"url": "https://x.com/jobs/1", "application_url": "https://ats.com/apply/1"}
    assert (prompt_mod.upload_job_key(job)
            == prompt_mod.upload_job_key(dict(job)))
    assert (prompt_mod.upload_job_key({"url": "https://x.com/jobs/1"})
            != prompt_mod.upload_job_key({"url": "https://x.com/jobs/2"}))


def test_upload_copy_integrity_check(app_dir, profile, monkeypatch):
    """A corrupted copy must fail the job, not reach the agent's prompt."""
    pdf = tailored_files(app_dir, "job_a", b"%PDF-resume-A")
    job = make_job("https://x.com/jobs/123", pdf)

    real_copy = prompt_mod.shutil.copy

    def corrupt_copy(src, dst, *args, **kwargs):
        real_copy(src, dst, *args, **kwargs)
        with open(dst, "wb") as f:
            f.write(b"%PDF-resume-B")  # the OTHER job's resume

    monkeypatch.setattr(prompt_mod.shutil, "copy", corrupt_copy)
    with pytest.raises(RuntimeError, match="integrity check"):
        prompt_mod.build_prompt(job=job, tailored_resume="A")


def test_cover_letter_paths_are_per_job(app_dir, profile):
    pdf_a = tailored_files(app_dir, "job_a", b"%PDF-resume-A")
    pdf_b = tailored_files(app_dir, "job_b", b"%PDF-resume-B")

    def cl_pair(name: str, body: bytes):
        cl_dir = app_dir / "cover_letters"
        cl_dir.mkdir(parents=True, exist_ok=True)
        txt = cl_dir / f"{name}.txt"
        txt.write_text(body.decode(), encoding="utf-8")
        pdf = cl_dir / f"{name}.pdf"
        pdf.write_bytes(body)
        return pdf

    cl_a = cl_pair("cl_a", b"%PDF-cover-A")
    cl_b = cl_pair("cl_b", b"%PDF-cover-B")
    prompt_a = prompt_mod.build_prompt(job=make_job("https://x.com/jobs/123", pdf_a, cl_a),
                                       tailored_resume="A")
    prompt_b = prompt_mod.build_prompt(job=make_job("https://x.com/jobs/456", pdf_b, cl_b),
                                       tailored_resume="B")

    path_a = re.search(r"Cover Letter PDF \(upload if asked\): (.+)", prompt_a).group(1).strip()
    path_b = re.search(r"Cover Letter PDF \(upload if asked\): (.+)", prompt_b).group(1).strip()
    assert path_a != path_b
    assert open(path_a, "rb").read() == b"%PDF-cover-A"
    assert open(path_b, "rb").read() == b"%PDF-cover-B"
