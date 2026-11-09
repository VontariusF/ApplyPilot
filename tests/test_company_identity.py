"""Company-identity layer tests (hiring-leads spec art_u0YGUqpk §1-2).

Pins the acceptance criteria: every discovery source resolves a company
name (Workday rows also a domain), the backfill is NULL-only idempotent
(a second run never overwrites), company fit is MAX across member jobs,
and the high-match threshold reads leads.yaml with its boundary exactly
at the default.
"""

import sqlite3

import pandas as pd
import pytest

from kestrel import config, database
from kestrel.company_identity import (
    company_from_description_header,
    company_from_workday_subdomain,
    company_key_for,
    domain_for_company,
    normalize_name_key,
    resolve_company_identity,
)
from kestrel.database import (
    backfill_company_identity,
    company_fit_aggregates,
    high_match_companies,
    init_db,
)
from kestrel.discovery.jobspy import store_jobspy_results

# A two-slug registry fixture: resolvers take the registry as a plain
# dict, so unit tests never depend on the packaged employers.yaml.
REGISTRY = {
    "acme": {"name": "Acme Robotics", "tenant": "acme", "site_id": "External",
             "base_url": "https://acme.wd5.myworkdayjobs.com"},
}


def _row(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


# ---------------------------------------------------------------------------
# Pure resolvers over real URL shapes (criterion A1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,company,description,expected", [
    # Workday subdomain: identity from the URL + registry, domain resolved
    ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/12345",
     None, None, ("Acme Robotics", "acme.wd5.myworkdayjobs.com")),
    # Workday slug the registry doesn't know: a visible gap, never a guess
    ("https://mystery.wd3.myworkdayjobs.com/en-US/site/job/9",
     None, None, (None, None)),
    # Direct-ATS posting: the host is the employer (with career prefix)
    ("https://jobs.acme-robotics.com/posting/77",
     "Acme Robotics", None, ("Acme Robotics", "acme-robotics.com")),
    # Aggregator posting: name-confidence — the platform's host is not
    # the employer's domain
    ("https://www.linkedin.com/jobs/view/424242",
     "Acme Robotics", None, ("Acme Robotics", None)),
    # Composio rows: "Company: X" folded into the description header
    ("https://web3.career/job/backend-engineer",
     None, "Company: ChainForge Labs\n\nTags: solidity\n\nBuild things.",
     ("ChainForge Labs", None)),
    # Nothing to resolve: stays ungrouped
    ("https://web3.career/job/another", None, "Just a body text.",
     (None, None)),
])
def test_resolve_company_identity_url_shapes(url, company, description, expected):
    assert resolve_company_identity(url, company, description, REGISTRY) == expected


def test_resolve_precedence_parsed_name_beats_header_and_registry():
    # Parsed name wins even when the URL is a registered Workday host and
    # the description carries a header.
    name, domain = resolve_company_identity(
        "https://acme.wd5.myworkdayjobs.com/en-US/x/job/1",
        "Acme Corp (parsed)", "Company: Not Acme", REGISTRY)
    assert name == "Acme Corp (parsed)"
    assert domain == "acme.wd5.myworkdayjobs.com"


def test_resolve_precedence_workday_beats_header():
    name, _ = resolve_company_identity(
        "https://acme.wd5.myworkdayjobs.com/en-US/x/job/1",
        None, "Company: Not Acme", REGISTRY)
    assert name == "Acme Robotics"


def test_description_header_parses_only_the_leading_line():
    # The exact shape map_web3_job emits.
    assert company_from_description_header("Company: ChainForge\n\nbody") == "ChainForge"
    # A "Company:" mention deeper in the body is not identity.
    assert company_from_description_header(
        "Great team.\n\nCompany: perks listed here") is None
    assert company_from_description_header(None) is None
    assert company_from_description_header("Tags: rust\n\nbody") is None


def test_workday_subdomain_uses_registry_and_is_total():
    assert company_from_workday_subdomain(
        "https://acme.wd5.myworkdayjobs.com/x", REGISTRY) == "Acme Robotics"
    # Unknown slug → None (registry lookup, no invented display name)
    assert company_from_workday_subdomain(
        "https://mystery.wd3.myworkdayjobs.com/x", REGISTRY) is None
    # Not a Workday URL at all
    assert company_from_workday_subdomain("https://acme.com/careers", REGISTRY) is None
    assert company_from_workday_subdomain(None, REGISTRY) is None


def test_domain_heuristic_direct_hosts_and_platform_hosts():
    # Career subdomain prefixes strip to the employer's real domain.
    assert domain_for_company("https://jobs.acme-robotics.com/x") == "acme-robotics.com"
    assert domain_for_company("https://careers.acme.com/x") == "acme.com"
    assert domain_for_company("https://www.acme.com/x") == "acme.com"
    assert domain_for_company("https://acme.com/careers") == "acme.com"
    # Workday hosts resolve to their own registry-stable subdomain.
    assert domain_for_company("https://acme.wd5.myworkdayjobs.com/x") == \
        "acme.wd5.myworkdayjobs.com"
    # Job boards and multi-tenant ATS hosts are never employer domains.
    assert domain_for_company("https://www.linkedin.com/jobs/view/1") is None
    assert domain_for_company("https://jobs.lever.co/acme/1") is None
    assert domain_for_company("https://boards.greenhouse.io/acme/1") is None


def test_name_keys_normalize_and_mark_name_confidence():
    assert normalize_name_key("Acme Robotics, Inc.") == "acme-robotics-inc"
    # Domain present → the domain IS the key; absent → prefixed name key.
    assert company_key_for("Acme Robotics", "acme-robotics.com") == "acme-robotics.com"
    assert company_key_for("Acme Robotics", None) == "name:acme-robotics"


# ---------------------------------------------------------------------------
# Backfill: NULL-only, idempotent, runs at init_db (criterion A2)
# ---------------------------------------------------------------------------

def _seed_identity_rows(conn):
    # Legacy JobSpy row: the pre-fix INSERT dropped the parsed name and
    # the posting carries no other identity source — an unresolvable gap.
    conn.execute(
        "INSERT INTO jobs (url, site, description) VALUES (?, 'linkedin', NULL)",
        ("https://www.linkedin.com/jobs/view/1",))
    # Workday row: identity lives in the URL subdomain + registry.
    conn.execute(
        "INSERT INTO jobs (url, site) VALUES (?, 'workday')",
        ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/2",))
    # Composio row: the writer folded the company into the description.
    conn.execute(
        "INSERT INTO jobs (url, site, description) VALUES (?, 'web3_career', ?)",
        ("https://web3.career/job/3", "Company: ChainForge Labs\n\nBuild things."))
    # Composio row with a null company field: no header folded, no identity.
    conn.execute(
        "INSERT INTO jobs (url, site, description) VALUES (?, 'web3_career', ?)",
        ("https://web3.career/job/4", "No identity anywhere in here."))
    # Direct-ATS row identified via the header (name) + its own host (domain).
    conn.execute(
        "INSERT INTO jobs (url, site, description) VALUES (?, 'acme_ats', ?)",
        ("https://jobs.acme-robotics.com/posting/5",
         "Company: Acme Robotics\n\nBuild robots."))
    conn.commit()


def test_backfill_resolves_every_source_and_leaves_gaps_visible(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    _seed_identity_rows(conn)

    assert backfill_company_identity(conn) == 3

    # Legacy JobSpy row: nothing to resolve from — the visible gap.
    assert _row(conn, "https://www.linkedin.com/jobs/view/1")["company"] is None
    assert _row(conn, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/2")["company"] == \
        "Acme Robotics"
    assert _row(conn, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/2")["company_domain"] == \
        "acme.wd5.myworkdayjobs.com"
    assert _row(conn, "https://web3.career/job/3")["company"] == "ChainForge Labs"
    assert _row(conn, "https://web3.career/job/3")["company_domain"] is None
    assert _row(conn, "https://jobs.acme-robotics.com/posting/5")["company"] == \
        "Acme Robotics"
    assert _row(conn, "https://jobs.acme-robotics.com/posting/5")["company_domain"] == \
        "acme-robotics.com"
    # The unresolvable row is the visible gap: still NULL, never guessed.
    assert _row(conn, "https://web3.career/job/4")["company"] is None


def test_backfill_idempotent_sentinel_survives(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    _seed_identity_rows(conn)
    assert backfill_company_identity(conn) == 3

    # Mutate one resolved row to a sentinel; the second pass must never
    # overwrite a non-NULL company.
    conn.execute("UPDATE jobs SET company = 'SENTINEL', company_domain = 'sentinel-dom' "
                 "WHERE url = 'https://acme.wd5.myworkdayjobs.com/en-US/External/job/2'")
    conn.commit()

    assert backfill_company_identity(conn) == 0
    row = _row(conn, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/2")
    assert row["company"] == "SENTINEL"
    assert row["company_domain"] == "sentinel-dom"

    # ...and init_db as a whole re-runs the same contract.
    conn = init_db()
    assert _row(conn, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/2")["company"] == \
        "SENTINEL"


def test_init_db_backfills_identity_on_startup(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    _seed_identity_rows(conn)
    conn.close()
    database.close_connection(str(isolated_db))

    conn = init_db()
    assert _row(conn, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/2")["company"] == \
        "Acme Robotics"


def test_jobspy_store_persists_parsed_company(app_dir, isolated_db):
    conn = init_db()
    df = pd.DataFrame([{
        "job_url": "https://www.linkedin.com/jobs/view/1",
        "title": "Staff Platform Engineer",
        "company": "Acme Robotics",
        "location": "Austin, TX",
        "min_amount": 175000, "max_amount": 195000,
        "currency": "USD", "interval": "yearly",
        "description": "Build the platform. " * 20,
        "site": "linkedin", "is_remote": True,
        "job_url_direct": None,
        "date_posted": pd.Timestamp("2026-11-01"),
    }])
    assert store_jobspy_results(conn, df, "linkedin") == (1, 0)
    row = _row(conn, "https://www.linkedin.com/jobs/view/1")
    assert row["company"] == "Acme Robotics"
    assert row["company_domain"] is None  # platform host: name-confidence


# ---------------------------------------------------------------------------
# Company fit aggregation: MAX across member jobs (criterion A3)
# ---------------------------------------------------------------------------

def _seed_scored(conn, url, company, domain, fit_score):
    conn.execute(
        "INSERT INTO jobs (url, title, company, company_domain, fit_score, site) "
        "VALUES (?, 'Engineer', ?, ?, ?, 'T')",
        (url, company, domain, fit_score))
    conn.commit()


def test_company_fit_is_max_not_mean(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    for i, score in enumerate([92, 40, 40, 40, 40]):
        _seed_scored(conn, f"https://www.linkedin.com/jobs/view/{i}",
                     "Acme Robotics", None, score)

    aggregates = company_fit_aggregates(conn=conn)
    assert len(aggregates) == 1
    agg = aggregates[0]
    assert agg["company_key"] == "name:acme-robotics"
    assert agg["fit_score"] == 92          # MAX wins; the mean is 50.4
    assert agg["scored_jobs"] == 5
    assert agg["display_name"] == "Acme Robotics"
    assert agg["domain"] is None


def test_high_match_threshold_boundary_at_default(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    _seed_scored(conn, "https://x/at-8", "Acme Robotics", None, 8)
    _seed_scored(conn, "https://x/below-8", "Beta Labs", None, 7)

    # Default threshold (8, on the scorer's 1-10 scale — dogfood
    # 2026-11-09): exactly-at is high-match, one-under is not.
    keys = [a["company_key"] for a in high_match_companies(conn=conn)]
    assert keys == ["name:acme-robotics"]

    # An explicit stricter threshold excludes the boundary company.
    assert high_match_companies(conn=conn, min_fit=8.5) == []
    # ...and a looser one admits both, highest fit first.
    looser = high_match_companies(conn=conn, min_fit=7)
    assert [a["fit_score"] for a in looser] == [8, 7]


def test_high_match_requires_at_least_one_scored_job(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    # Five unscored member jobs — the company must not surface.
    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (url, company, company_domain, site) "
            "VALUES (?, 'Acme Robotics', 'acme-robotics.com', 'T')",
            (f"https://jobs.acme-robotics.com/posting/{i}",))
    conn.commit()

    assert company_fit_aggregates(conn=conn) == []
    assert high_match_companies(conn=conn, min_fit=0) == []


def test_domain_keyed_groups_and_case_insensitive_names(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    _seed_scored(conn, "https://jobs.acme-robotics.com/1", "Acme", "acme-robotics.com", 80)
    _seed_scored(conn, "https://acme-robotics.com/2", "Acme", "acme-robotics.com", 90)
    _seed_scored(conn, "https://www.linkedin.com/jobs/view/3", "ACME", None, 70)
    _seed_scored(conn, "https://www.linkedin.com/jobs/view/4", "Acme", None, 71)

    aggregates = company_fit_aggregates(conn=conn)
    assert [(a["company_key"], a["fit_score"]) for a in aggregates] == [
        ("acme-robotics.com", 90),
        ("name:acme", 71),  # case variants of the name group together
    ]
    # display_name rides the row that produced the max.
    by_key = {a["company_key"]: a for a in aggregates}
    assert by_key["acme-robotics.com"]["display_name"] == "Acme"
    assert by_key["name:acme"]["display_name"] == "Acme"


# ---------------------------------------------------------------------------
# company_identity mirror + leads.yaml threshold
# ---------------------------------------------------------------------------

def test_refresh_materializes_company_identity(isolated_db, monkeypatch):
    monkeypatch.setattr("kestrel.discovery.workday.load_employers", lambda: REGISTRY)
    conn = init_db()
    _seed_scored(conn, "https://jobs.acme-robotics.com/1", "Acme Robotics",
                 "acme-robotics.com", 90)

    assert database.refresh_company_identity(conn=conn) == 1
    row = conn.execute("SELECT * FROM company_identity").fetchone()
    assert row["company_key"] == "acme-robotics.com"
    assert row["display_name"] == "Acme Robotics"
    assert row["domain"] == "acme-robotics.com"
    assert row["fit_score"] == 90
    assert row["updated_at"]

    # Re-running refresh recomputes the derived fit (mirror may drift UP
    # when a better job scores; identity columns are the guarded ones).
    _seed_scored(conn, "https://jobs.acme-robotics.com/2", "Acme Robotics",
                 "acme-robotics.com", 95)
    database.refresh_company_identity(conn=conn)
    row = conn.execute("SELECT * FROM company_identity").fetchone()
    assert row["fit_score"] == 95


def test_threshold_reads_leads_yaml_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    # No user copy → packaged example: the default on the scorer's
    # 1-10 scale (dogfood 2026-11-09 — 75 presumed a 0-100 scale).
    assert config.leads_min_company_fit() == 8

    (tmp_path / "leads.yaml").write_text("min_company_fit: 50\n", encoding="utf-8")
    assert config.leads_min_company_fit() == 50.0


def test_threshold_refuses_unparseable_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "leads.yaml").write_text('min_company_fit: "high"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="min_company_fit"):
        config.leads_min_company_fit()


def test_company_identity_table_created_by_init_db(isolated_db):
    conn = init_db()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "company_identity" in names
    columns = {r[1] for r in conn.execute("PRAGMA table_info(company_identity)")}
    assert columns == {"company_key", "display_name", "domain",
                       "fit_score", "updated_at"}


def test_jobs_company_columns_in_registry(isolated_db):
    conn = init_db()
    added = database.ensure_columns(conn)
    assert added == []  # both columns already present on a current schema
    columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"company", "company_domain"} <= columns


def test_ensure_columns_migrates_legacy_table(tmp_path):
    """A pre-feature store gains both columns via the registry (no data loss)."""
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url           TEXT PRIMARY KEY,
            title         TEXT,
            salary        TEXT,
            description   TEXT,
            location      TEXT,
            site          TEXT,
            strategy      TEXT,
            discovered_at TEXT
        )
    """)
    conn.execute("INSERT INTO jobs (url, title) VALUES ('https://x/1', 'Eng')")
    conn.commit()

    added = database.ensure_columns(conn)
    assert "company" in added and "company_domain" in added
    names = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"company", "company_domain"} <= names
    # Existing rows survive the migration.
    assert conn.execute("SELECT title FROM jobs WHERE url = 'https://x/1'").fetchone()[
        "title"] == "Eng"
