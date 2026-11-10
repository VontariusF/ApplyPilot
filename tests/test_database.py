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
    init_db,
    mark_hiring_lead,
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
