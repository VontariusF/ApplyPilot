"""S7: --url LIKE patterns escape % and _ so a URL matches itself only.

acquire_job's target-URL lookup builds a LIKE pattern from user input;
unescaped wildcards could pull in unrelated jobs.
"""

import pytest

from applypilot import database
from applypilot.apply import launcher


def seed_job(conn, url):
    conn.execute(
        "INSERT INTO jobs (url, title, site, fit_score, tailored_resume_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (url, "Engineer", "example", 9, f"/tmp/fake/{abs(hash(url))}.pdf"),
    )
    conn.commit()


@pytest.fixture
def queue(app_dir):
    database.init_db()
    return database.get_connection()


def test_percent_url_does_not_wildcard_match(queue):
    # Only the trap row exists: an unescaped % would wildcard-match it.
    seed_job(queue, "https://x.com/jobs/100Xremote")
    assert launcher.acquire_job(target_url="https://x.com/jobs/100%remote?utm=x") is None


def test_percent_url_matches_literal_row(queue):
    seed_job(queue, "https://x.com/jobs/100%remote")
    job = launcher.acquire_job(target_url="https://x.com/jobs/100%remote?utm=x")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/100%remote"


def test_underscore_url_does_not_wildcard_match(queue):
    seed_job(queue, "https://x.com/jobs/aXb")
    assert launcher.acquire_job(target_url="https://x.com/jobs/a_b?utm=x") is None


def test_underscore_url_matches_literal_row(queue):
    seed_job(queue, "https://x.com/jobs/a_b")
    job = launcher.acquire_job(target_url="https://x.com/jobs/a_b?utm=x")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/a_b"


def test_backslash_url_matches_itself(queue):
    # The escape char itself must be escaped, or the pattern breaks ESCAPE syntax.
    seed_job(queue, "https://x.com/jobs/we\\ird")
    job = launcher.acquire_job(target_url="https://x.com/jobs/we\\ird")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/we\\ird"


def test_trailing_slash_still_matches(queue):
    seed_job(queue, "https://x.com/jobs/plain")
    job = launcher.acquire_job(target_url="https://x.com/jobs/plain/")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/plain"
