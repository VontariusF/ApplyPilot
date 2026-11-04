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


# F4: exact matches win, the fallback is suffix-anchored, ambiguity errors.

def test_acquire_url_prefers_exact_match(queue):
    # Sibling /jobs/1234 inserted FIRST (lower rowid): an unanchored contains
    # LIKE with LIMIT 1 would acquire it instead of the requested /jobs/123.
    seed_job(queue, "https://x.com/jobs/1234")
    seed_job(queue, "https://x.com/jobs/123")
    job = launcher.acquire_job(target_url="https://x.com/jobs/123")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/123"


def test_acquire_url_prefers_exact_match_sibling_first_inserted(queue):
    # Same pair, reverse insertion order — exact must win either way.
    seed_job(queue, "https://x.com/jobs/123")
    seed_job(queue, "https://x.com/jobs/1234")
    job = launcher.acquire_job(target_url="https://x.com/jobs/123")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/123"


def test_acquire_url_suffix_anchored_does_not_match_sibling(queue):
    # Only the sibling exists: a suffix-anchored fallback must NOT grab it
    # (the old contains-LIKE matched /jobs/1234 for a /jobs/123 request).
    seed_job(queue, "https://x.com/jobs/1234")
    assert launcher.acquire_job(target_url="https://x.com/jobs/123") is None


def test_acquire_url_ambiguous_fallback_raises(queue):
    # Two distinct suffix-anchored matches and no exact row: refuse to guess.
    seed_job(queue, "https://x.com/jobs/123/group")
    seed_job(queue, "https://x.com/jobs/123/apply")
    with pytest.raises(launcher.AmbiguousJobURLError) as excinfo:
        launcher.acquire_job(target_url="https://x.com/jobs/123")
    assert "https://x.com/jobs/123/group" in str(excinfo.value)
    assert "https://x.com/jobs/123/apply" in str(excinfo.value)


def test_acquire_url_trailing_slash_row_still_fallback_matches(queue):
    # Stored row with a trailing slash: one normalized path, not ambiguity.
    seed_job(queue, "https://x.com/jobs/plain/")
    job = launcher.acquire_job(target_url="https://x.com/jobs/plain")
    assert job is not None
    assert job["url"] == "https://x.com/jobs/plain/"
