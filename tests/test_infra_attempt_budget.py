"""P1-1(c): infra-classified LLM failures must not burn attempt budgets.

A provider outage used to convert every queued job's tailor/cover pass into
a per-job error result that still incremented tailor_attempts /
cover_attempts — one outage exhausted every queued job's attempt budget at
~1 pass/sec while the stage reported "ok" (reliability sweep P1-1).
Connection errors, timeouts, and HTTP 5xx are now classified by
llm.is_infra_error and exempt from the attempt counters; content-level
failures (parse errors, validation rejections) still count.
"""

from pathlib import Path

import httpx
import pytest

from kestrel import config, database
from kestrel.database import get_connection
import kestrel.scoring.cover_letter as cover_letter
import kestrel.scoring.tailor as tailor

URL = "https://jobs.example/posting-1"


@pytest.fixture(autouse=True)
def _pdf_stub(monkeypatch):
    """No Chromium in tests: patch the PDF converter the writers import at call time."""
    monkeypatch.setattr(
        "kestrel.scoring.pdf.convert_to_pdf",
        lambda p: Path(str(p)).with_suffix(".pdf"),
    )


@pytest.fixture()
def queue(app_dir):
    database.init_db()
    return get_connection()


def _seed_job(conn, url=URL, **extra):
    cols = {
        "url": url, "title": "Engineer", "site": "acme",
        "full_description": "d" * 300, "fit_score": 9,
        "discovered_at": "2026-01-01T00:00:00+00:00",
    }
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


def _resume_and_profile(monkeypatch):
    """Point the scoring modules' bound paths at the isolated app dir."""
    resume = config.RESUME_PATH
    resume.write_text("Base resume text", encoding="utf-8")
    for module in (tailor, cover_letter):
        monkeypatch.setattr(module, "RESUME_PATH", resume)
        monkeypatch.setattr(module, "load_profile",
                            lambda: {"personal": {"full_name": "Test"}})
    monkeypatch.setattr(tailor, "TAILORED_DIR", config.TAILORED_DIR)
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", config.COVER_LETTER_DIR)


def _attempts(conn, url=URL):
    row = conn.execute(
        "SELECT tailor_attempts, cover_attempts, tailored_resume_path, "
        "cover_letter_path FROM jobs WHERE url = ?", (url,)).fetchone()
    return row


def test_tailor_infra_error_does_not_burn_attempts(queue, monkeypatch):
    """A connection error during tailoring leaves the job fully retryable."""
    _resume_and_profile(monkeypatch)

    def _infra_failure(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(tailor, "tailor_resume", _infra_failure)
    _seed_job(queue, tailor_attempts=2)

    result = tailor.run_tailoring(min_score=7, limit=1)
    assert result["errors"] == 1

    tailor_attempts, _, tailored_path, _ = _attempts(queue)
    assert tailor_attempts == 2, (
        "an infra failure must not burn the job's tailor attempt budget")
    assert tailored_path is None  # and the job stays pending


def test_tailor_content_error_still_burns_attempts(queue, monkeypatch):
    """A content-level failure (bad output) keeps counting toward the cap."""
    _resume_and_profile(monkeypatch)

    def _content_failure(*args, **kwargs):
        raise ValueError("model returned unusable resume JSON")

    monkeypatch.setattr(tailor, "tailor_resume", _content_failure)
    _seed_job(queue)

    result = tailor.run_tailoring(min_score=7, limit=1)
    assert result["errors"] == 1

    tailor_attempts, _, _, _ = _attempts(queue)
    assert tailor_attempts == 1


def test_cover_infra_error_does_not_burn_attempts(queue, monkeypatch):
    """Same exemption for the cover stage: the provider outage stays free."""
    _resume_and_profile(monkeypatch)

    def _infra_failure(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(cover_letter, "generate_cover_letter", _infra_failure)
    _seed_job(queue, tailored_resume_path="/tmp/fake/resume.pdf",
              cover_attempts=2)

    result = cover_letter.run_cover_letters(min_score=7, limit=1)
    assert result["errors"] == 1

    _, cover_attempts, _, _ = _attempts(queue)
    assert cover_attempts == 2, (
        "an infra failure must not burn the job's cover attempt budget")


def test_cover_content_error_still_burns_attempts(queue, monkeypatch):
    _resume_and_profile(monkeypatch)

    def _content_failure(*args, **kwargs):
        raise ValueError("model refused the request")

    monkeypatch.setattr(cover_letter, "generate_cover_letter", _content_failure)
    _seed_job(queue, tailored_resume_path="/tmp/fake/resume.pdf")

    result = cover_letter.run_cover_letters(min_score=7, limit=1)
    assert result["errors"] == 1

    _, cover_attempts, _, _ = _attempts(queue)
    assert cover_attempts == 1


def test_http_5xx_is_infra_but_4xx_is_not():
    """Server-side errors are the provider's fault (exempt); a 4xx is a
    configuration problem on this side and still counts."""
    from kestrel.llm import is_infra_error

    def _status_error(code):
        request = httpx.Request("POST", "https://example.invalid/v1")
        response = httpx.Response(code, request=request)
        return httpx.HTTPStatusError(f"HTTP {code}", request=request,
                                     response=response)

    assert is_infra_error(_status_error(503))
    assert is_infra_error(_status_error(500))
    assert not is_infra_error(_status_error(401))
    assert is_infra_error(httpx.ConnectError("refused"))
    assert is_infra_error(httpx.ReadTimeout("timed out"))
    assert not is_infra_error(ValueError("bad JSON"))
