"""Contract tests for the store-level identity layer (spec art_u0YGUqpk
§1/§2, sweep art_Izev3UNN findings 6-7).

company_identity is derived data — recomputed from jobs on every
init_db — so it must track job deletion (no ghost high-match companies)
and hiring_leads must dedupe on the person, not on the URL spelling a
particular provider happened to serve.
"""

import pytest

from kestrel.database import (
    LEAD_SOURCE_SERP,
    company_fit_aggregates,
    init_db,
    refresh_company_identity,
    upsert_hiring_lead,
)
from kestrel.discovery.hiring_leads import LeadsRefusal, discover_hiring_leads

NOW = "2026-11-09T12:00:00+00:00"


@pytest.fixture
def conn(app_dir):
    return init_db()


def _seed_scored_job(conn, url, company, domain, fit):
    conn.execute(
        "INSERT INTO jobs (url, title, company, company_domain, fit_score, "
        "discovered_at) VALUES (?, ?, ?, ?, ?, ?)",
        (url, f"Senior Engineer at {company}", company, domain, fit, NOW),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Finding 6: job deletion must not leave a ghost company in the mirror
# ---------------------------------------------------------------------------


def test_refresh_drops_companies_whose_fit_rows_are_gone(conn):
    _seed_scored_job(conn, "https://jobs.acme-robotics.com/posting/1",
                     "Acme Robotics", "acme-robotics.com", 90)
    _seed_scored_job(conn, "https://jobs.acme-robotics.com/posting/3",
                     "Acme Robotics", "acme-robotics.com", 80)
    _seed_scored_job(conn, "https://jobs.boltlabs.com/posting/2",
                     "Bolt Labs", "boltlabs.com", 85)
    refresh_company_identity(conn)
    assert {r["company_key"] for r in conn.execute(
        "SELECT company_key FROM company_identity")} == {
        "acme-robotics.com", "boltlabs.com"}

    # Deleting one member job keeps the company (the other job still
    # scores) and recomputes its MAX fit.
    conn.execute("DELETE FROM jobs WHERE url = ?",
                 ("https://jobs.acme-robotics.com/posting/1",))
    conn.commit()
    refresh_company_identity(conn)
    acme = conn.execute(
        "SELECT fit_score FROM company_identity WHERE company_key = ?",
        ("acme-robotics.com",)).fetchone()
    assert acme is not None
    assert acme["fit_score"] == 80

    # Deleting the last member job must remove the company — an
    # upsert-only refresh left a ghost high-match row that --company
    # discovery kept scanning against a stale MAX fit.
    conn.execute("DELETE FROM jobs WHERE url = ?",
                 ("https://jobs.boltlabs.com/posting/2",))
    conn.commit()
    refresh_company_identity(conn)
    keys = {r["company_key"] for r in conn.execute(
        "SELECT company_key FROM company_identity")}
    assert "boltlabs.com" not in keys


def test_deleted_company_is_no_longer_discoverable_by_key(conn,
                                                          monkeypatch):
    """The ghost bit the discovery path: --company read the mirror
    directly, so a fit-less company still scanned. After the cleanup it
    refuses like any other unresolved key."""
    _seed_scored_job(conn, "https://jobs.acme-robotics.com/posting/1",
                     "Acme Robotics", "acme-robotics.com", 90)
    refresh_company_identity(conn)
    conn.execute("DELETE FROM jobs WHERE url = ?",
                 ("https://jobs.acme-robotics.com/posting/1",))
    conn.commit()
    refresh_company_identity(conn)

    monkeypatch.setenv("TAVILY_API_KEY", "test-key-not-a-credential")
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, company_key="acme-robotics.com")
    assert refused.value.code == "company_unresolved"
# ---------------------------------------------------------------------------
# Finding 7: profile-URL variants of one person are one lead row
# ---------------------------------------------------------------------------

_JANE = {"full_name": "Jane Doe",
         "title": "Jane Doe - Head of Talent - Acme Robotics",
         "company_key": "acme-robotics.com"}


def _upsert_jane(conn, profile_url, **overrides):
    fields = {**_JANE, **overrides}
    return upsert_hiring_lead(
        conn, profile_url=profile_url, source=LEAD_SOURCE_SERP,
        discovered_at=NOW, **fields)


def test_upsert_hiring_lead_dedupes_url_variants(conn):
    assert _upsert_jane(conn, "https://linkedin.com/in/jane-doe") is True
    # The same person via www/host-case/trailing-slash variants of the
    # one profile: still one row — the raw spellings stored three rows
    # for one person before the normalization (sweep finding 7).
    assert _upsert_jane(conn, "https://www.linkedin.com/in/jane-doe") is False
    assert _upsert_jane(conn, "https://LinkedIn.com/in/jane-doe/") is False
    rows = conn.execute("SELECT profile_url FROM hiring_leads").fetchall()
    assert len(rows) == 1
    assert rows[0]["profile_url"] == "https://linkedin.com/in/jane-doe"


def test_upsert_reconciliation_preserves_decided_state(conn):
    """Collapsing duplicates never silently drops a decision: the
    surviving row carries the most-decided status of the variants."""
    assert _upsert_jane(conn, "https://linkedin.com/in/jane-doe") is True
    # A legacy store that keyed on raw spellings holds the duplicate as
    # its own row — one the person has already decided on.
    conn.execute(
        "INSERT INTO hiring_leads (profile_url, full_name, title, "
        "company_key, source, last_status, discovered_at, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("https://www.linkedin.com/in/jane-doe", _JANE["full_name"],
         _JANE["title"], _JANE["company_key"], LEAD_SOURCE_SERP,
         "connected", NOW, NOW))
    conn.commit()

    # A later write of a third spelling reconciles the duplicate rows
    # into one, and the connected decision transfers deliberately.
    assert _upsert_jane(conn, "https://LinkedIn.com/in/jane-doe/") is False
    rows = conn.execute(
        "SELECT profile_url, last_status FROM hiring_leads").fetchall()
    assert len(rows) == 1
    assert rows[0]["last_status"] == "connected"


def test_upsert_still_inserts_a_distinct_person(conn):
    assert _upsert_jane(conn, "https://linkedin.com/in/jane-doe") is True
    assert _upsert_jane(conn, "https://linkedin.com/in/john-smith",
                        full_name="John Smith",
                        title="John Smith - Recruiter") is True


# ---------------------------------------------------------------------------
# Suggestion 4: display-name ties resolve deterministically
# ---------------------------------------------------------------------------


def test_company_fit_aggregates_tiebreak_display_names_stably(conn):
    """Two spellings of one company tied at the max must resolve to the
    same display name regardless of the row order SQLite returns — the
    first-seen rule depended on unspecified scan order (sweep
    art_Izev3UNN suggestion 4)."""
    _seed_scored_job(conn, "https://jobs.zeta-corp.com/posting/1",
                     "Zeta Corp", "zeta-corp.com", 90)
    _seed_scored_job(conn, "https://jobs.zeta-corp.com/posting/2",
                     "Alpha Corp", "zeta-corp.com", 90)
    aggs = company_fit_aggregates(conn=conn)
    assert len(aggs) == 1
    assert aggs[0]["display_name"] == "Alpha Corp"
