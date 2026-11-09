"""Contract tests for deep-filtering Layer 1 (spec art_we4AUxEy).

Structured ingestion (date_posted, annualized pay, seniority), the
idempotent startup backfills, the table-driven seniority classifier, and
the scored_resume_hash binding — the data substrate the `jobs filter` /
`jobs rerank` surface (Layer 2/3) will query.
"""

import hashlib

import pandas as pd
import pytest

from kestrel import config, database
from kestrel.discovery.jobspy import store_jobspy_results
from kestrel.discovery.salary import parse_salary_display, parse_salary_row
from kestrel.enrichment.seniority import classify_seniority
from kestrel.scoring.scorer import persist_score, resume_sha256


def _row(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


# ---------------------------------------------------------------------------
# Salary display-string parser (the live zoo, spec Current-state evidence)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("display,expected", [
    # No currency, no interval (live shape)
    ("60,000-82,000", (60000.0, 82000.0, None, None)),
    # CN¥ prefix + monthly
    ("CN¥18,000-CN¥28,000/monthly", (216000.0, 336000.0, "CN¥", "monthly")),
    # EUR prefix + monthly
    ("EUR5,030-EUR7,183/monthly", (60360.0, 86196.0, "EUR", "monthly")),
    # Currency with space + uppercase bare-noun interval
    ("USD 175,000-195,000/YEAR", (175000.0, 195000.0, "USD", "yearly")),
    # Currency repeated on both sides + yearly
    ("USD119,000-USD224,000/yearly", (119000.0, 224000.0, "USD", "yearly")),
    # Single value (jobspy renders when max is missing)
    ("USD119,000", (119000.0, 119000.0, "USD", None)),
    # Hourly annualizes at 2080 (jobspy renders interval with '/')
    ("USD50/hourly", (104000.0, 104000.0, "USD", "hourly")),
    ("$30/hourly", (62400.0, 62400.0, "$", "hourly")),
    # Common interval abbreviations no longer masquerade as annual (L3)
    ("USD20/hr", (41600.0, 41600.0, "USD", "hourly")),
    ("5,000/mo", (60000.0, 60000.0, None, "monthly")),
    ("800/wk", (41600.0, 41600.0, None, "weekly")),
    # k/m-suffixed amounts scale instead of parsing as raw annual figures (L1)
    ("$90k-120k", (90000.0, 120000.0, "$", None)),
    ("€1.5m-2m", (1500000.0, 2000000.0, "€", None)),
])
def test_parse_salary_display_live_zoo(display, expected):
    parsed = parse_salary_display(display)
    assert parsed is not None
    got = (parsed.min_annual, parsed.max_annual, parsed.currency, parsed.interval)
    assert got == expected


@pytest.mark.parametrize("display", [None, "", "   ", "Negotiable", "competitive"])
def test_parse_salary_display_unparseable_returns_none(display):
    assert parse_salary_display(display) is None


def test_parse_salary_display_never_raises_on_weird_input():
    # The B10 keep-the-row pattern depends on the parser being total.
    for weird in ("$$$", "/monthly", "USD", "e5-10", "1,2,3,4"):
        assert parse_salary_display(weird) is None or True  # must not raise


def test_parse_salary_row_raw_values():
    """Ingestion's constructor: raw row values, total (never raises)."""
    # Fractional hourly amounts keep their value
    parsed = parse_salary_row(22.5, 30, "USD", "hourly")
    assert (parsed.min_annual, parsed.max_annual, parsed.currency, parsed.interval) == (
        46800.0, 62400.0, "USD", "hourly")
    # Max-only row is populated (min=max), not dropped
    parsed = parse_salary_row(None, 224000.0, "USD", "yearly")
    assert (parsed.min_annual, parsed.max_annual) == (224000.0, 224000.0)
    # No usable amount → None: strings, None, and pandas NaN all fail closed
    nan = float("nan")
    assert parse_salary_row("lots", "money", "USD", "yearly") is None
    assert parse_salary_row(None, None, None, None) is None
    assert parse_salary_row(nan, nan, "USD", "yearly") is None
    # One valid amount survives a NaN pair-member
    parsed = parse_salary_row(nan, 100000.0, "USD", "yearly")
    assert (parsed.min_annual, parsed.max_annual) == (100000.0, 100000.0)


# ---------------------------------------------------------------------------
# Seniority classifier (table-driven, deterministic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Summer Intern — Engineering", "intern"),
    ("Apprentice Machinist", "intern"),
    ("Junior Data Analyst", "junior"),
    ("Entry-Level Support Engineer", "junior"),
    ("Graduate Developer", "junior"),
    ("Staff Software Engineer", "staff"),
    ("Solutions Architect", "staff"),
    ("Principal Engineer", "principal"),
    ("Engineering Lead", "lead"),
    ("Team Lead, Payments", "lead"),
    ("Engineering Manager", "manager"),
    ("Head of Infrastructure", "manager"),
    ("Director of Product", "manager"),
    ("Senior Backend Engineer", "senior"),
    ("Sr. Backend Engineer", "senior"),
])
def test_classify_seniority_all_classes(title, expected):
    assert classify_seniority(title) == expected


@pytest.mark.parametrize("title,description,expected", [
    ("Software Engineer", None, None),
    ("Software Engineer", "We build things.", None),
    # A years-of-experience hint upgrades the row to mid — never a named tier
    ("Software Engineer", "5+ years of experience required.", "mid"),
    (None, None, None),
    ("", "", None),
])
def test_classify_seniority_fallthrough(title, description, expected):
    assert classify_seniority(title, description) == expected


def test_classify_seniority_description_rule_match():
    # The spec's rules run over title, then description — a description
    # match tiers the row when the title yields nothing. (This accepts
    # false positives like "you will lead a team" for unmatchable
    # titles; seniority is advisory data, not a gate.)
    assert classify_seniority("Platform Engineer", "This is a junior role.") == "junior"
    assert classify_seniority("Software Engineer",
                              "You will lead a team of engineers.") == "lead"


def test_classify_seniority_experience_hint_yields_mid():
    assert classify_seniority("Software Engineer",
                              "Requires 3 to 5 years of experience.") == "mid"


def test_classify_seniority_title_outranks_description():
    # A senior title plus an intern-heavy description stays senior.
    assert classify_seniority("Senior Engineer",
                              "Our internship program hosts 20 interns.") == "senior"


# ---------------------------------------------------------------------------
# Structured ingestion (store_jobspy_results)
# ---------------------------------------------------------------------------

def _df(rows):
    return pd.DataFrame(rows)


def test_ingestion_persists_all_structured_fields(app_dir, isolated_db):
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/a", "title": "Staff Platform Engineer",
        "company": "Acme", "location": "Austin, TX",
        "min_amount": 175000, "max_amount": 195000,
        "currency": "USD", "interval": "yearly",
        "description": "Build the platform. " * 20,
        "site": "linkedin", "is_remote": True,
        "job_url_direct": None,
        "date_posted": pd.Timestamp("2026-11-01"),
    }])
    new, existing = store_jobspy_results(conn, df, "linkedin")
    assert (new, existing) == (1, 0)

    row = _row(conn, "https://jobs/a")
    assert row["date_posted"] == "2026-11-01"
    assert row["salary_min"] == 175000.0
    assert row["salary_max"] == 195000.0
    assert row["salary_currency"] == "USD"
    assert row["salary_interval"] == "yearly"
    assert row["seniority"] == "staff"


def test_ingestion_annualizes_monthly_pay(app_dir, isolated_db):
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/m", "title": "Engineer", "company": "Acme",
        "location": "Paris", "min_amount": 5000, "max_amount": 7000,
        "currency": "EUR", "interval": "monthly",
        "description": None, "site": "linkedin", "is_remote": False,
        "job_url_direct": None, "date_posted": None,
    }])
    store_jobspy_results(conn, df, "linkedin")
    row = _row(conn, "https://jobs/m")
    assert row["salary_min"] == 60000.0
    assert row["salary_max"] == 84000.0
    assert row["salary_interval"] == "monthly"  # original unit kept


def test_ingestion_malformed_salary_keeps_job_with_nulls(app_dir, isolated_db):
    """B10 pattern: one malformed pay cell skips the structured fields,
    never the job or the batch."""
    conn = database.init_db()
    df = _df([
        {"job_url": "https://jobs/bad", "title": "Engineer", "company": "Acme",
         "location": "X", "min_amount": "lots", "max_amount": "money",
         "currency": "USD", "interval": "yearly", "description": None,
         "site": "linkedin", "is_remote": False, "job_url_direct": None,
         "date_posted": None},
        {"job_url": "https://jobs/good", "title": "Engineer", "company": "Acme",
         "location": "X", "min_amount": 100000, "max_amount": 120000,
         "currency": "USD", "interval": "yearly", "description": None,
         "site": "linkedin", "is_remote": False, "job_url_direct": None,
         "date_posted": None},
    ])
    new, _ = store_jobspy_results(conn, df, "linkedin")
    assert new == 2  # the batch was not aborted

    bad = _row(conn, "https://jobs/bad")
    assert bad["salary"] is None
    assert bad["salary_min"] is None and bad["salary_max"] is None
    assert bad["salary_currency"] is None and bad["salary_interval"] is None
    assert _row(conn, "https://jobs/good")["salary_min"] == 100000.0


def test_ingestion_missing_date_posted_stores_null(app_dir, isolated_db):
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/nodate", "title": "Engineer",
        "company": "Acme", "location": "X", "min_amount": None,
        "max_amount": None, "currency": None, "interval": None,
        "description": None, "site": "indeed", "is_remote": False,
        "job_url_direct": None, "date_posted": None,
    }])
    store_jobspy_results(conn, df, "indeed")
    assert _row(conn, "https://jobs/nodate")["date_posted"] is None


def test_ingestion_date_posted_string_form(app_dir, isolated_db):
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/str", "title": "Engineer", "company": "Acme",
        "location": "X", "min_amount": None, "max_amount": None,
        "currency": None, "interval": None, "description": None,
        "site": "indeed", "is_remote": False, "job_url_direct": None,
        "date_posted": "2026-11-05 13:00:00",
    }])
    store_jobspy_results(conn, df, "indeed")
    assert _row(conn, "https://jobs/str")["date_posted"] == "2026-11-05"


def test_ingestion_impossible_date_stores_null(app_dir, isolated_db):
    """A well-formed-looking but impossible calendar date must not be
    stored verbatim (it would break --posted-within arithmetic later)."""
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/baddate", "title": "Engineer",
        "company": "Acme", "location": "X", "min_amount": None,
        "max_amount": None, "currency": None, "interval": None,
        "description": None, "site": "indeed", "is_remote": False,
        "job_url_direct": None, "date_posted": "2026-99-99",
    }])
    store_jobspy_results(conn, df, "indeed")
    assert _row(conn, "https://jobs/baddate")["date_posted"] is None


def test_ingestion_fractional_hourly_pay_not_truncated(app_dir, isolated_db):
    """M2: structured pay comes from the raw row — a fractional hourly
    minimum keeps its value (the display string would truncate it to
    USD22, losing ~2% on the annualized figure)."""
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/frac", "title": "Engineer",
        "company": "Acme", "location": "X", "min_amount": 22.5,
        "max_amount": 30, "currency": "USD", "interval": "hourly",
        "description": None, "site": "indeed", "is_remote": False,
        "job_url_direct": None, "date_posted": None,
    }])
    store_jobspy_results(conn, df, "indeed")
    row = _row(conn, "https://jobs/frac")
    assert row["salary_min"] == 46800.0  # 22.5 * 2080
    assert row["salary_max"] == 62400.0
    assert row["salary_interval"] == "hourly"


def test_ingestion_max_only_row_is_populated(app_dir, isolated_db):
    """M2: a row with only max_amount keeps its structured pay (the old
    display-string path rendered salary=None and lost the value)."""
    conn = database.init_db()
    df = _df([{
        "job_url": "https://jobs/maxonly", "title": "Engineer",
        "company": "Acme", "location": "X", "min_amount": None,
        "max_amount": 224000, "currency": "USD", "interval": "yearly",
        "description": None, "site": "indeed", "is_remote": False,
        "job_url_direct": None, "date_posted": None,
    }])
    store_jobspy_results(conn, df, "indeed")
    row = _row(conn, "https://jobs/maxonly")
    assert row["salary_min"] == 224000.0
    assert row["salary_max"] == 224000.0
    assert row["salary_currency"] == "USD"
    assert row["salary_interval"] == "yearly"


# ---------------------------------------------------------------------------
# Backfills (salary parse + seniority) — idempotent, never overwrite
# ---------------------------------------------------------------------------

def _seed_legacy(conn, url, salary, title="Software Engineer",
                 description=None, **extra):
    cols = {"url": url, "title": title, "salary": salary,
            "description": description}
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})",
                 tuple(cols.values()))
    conn.commit()


def test_backfill_salary_data_parses_the_zoo(app_dir, isolated_db):
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", "USD119,000-USD224,000/yearly")
    _seed_legacy(conn, "https://jobs/2", "CN¥18,000-CN¥28,000/monthly")
    _seed_legacy(conn, "https://jobs/3", "60,000-82,000")
    _seed_legacy(conn, "https://jobs/4", "USD 175,000-195,000/YEAR")
    _seed_legacy(conn, "https://jobs/5", "Negotiable")  # unparseable

    updated = database.backfill_salary_data(conn)
    assert updated == 4  # parseable strings all migrated

    r1 = _row(conn, "https://jobs/1")
    assert (r1["salary_min"], r1["salary_max"],
            r1["salary_currency"], r1["salary_interval"]) == (
        119000.0, 224000.0, "USD", "yearly")
    r2 = _row(conn, "https://jobs/2")
    assert (r2["salary_min"], r2["salary_interval"]) == (216000.0, "monthly")
    r3 = _row(conn, "https://jobs/3")
    assert (r3["salary_min"], r3["salary_currency"]) == (60000.0, None)
    r5 = _row(conn, "https://jobs/5")
    assert r5["salary_min"] is None  # kept the job, kept the NULLs


def test_backfill_salary_data_is_idempotent(app_dir, isolated_db):
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", "USD119,000-USD224,000/yearly")
    assert database.backfill_salary_data(conn) == 1
    # Second startup pass: no candidate rows left with a parsed range.
    assert database.backfill_salary_data(conn) == 0
    row = _row(conn, "https://jobs/1")
    assert row["salary_max"] == 224000.0


def test_backfill_never_overwrites_non_null_values(app_dir, isolated_db):
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", "USD119,000-USD224,000/yearly",
                 salary_min=1.0, salary_max=2.0,
                 salary_currency="XXX", salary_interval="hourly")
    assert database.backfill_salary_data(conn) == 0  # not a candidate
    row = _row(conn, "https://jobs/1")
    assert (row["salary_min"], row["salary_max"],
            row["salary_currency"], row["salary_interval"]) == (
        1.0, 2.0, "XXX", "hourly")


def test_backfill_seniority_and_never_overwrites(app_dir, isolated_db):
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", "USD100,000", title="Senior Go Engineer")
    _seed_legacy(conn, "https://jobs/2", "USD100,000", title="Intern")
    _seed_legacy(conn, "https://jobs/3", "USD100,000", title="Engineer")
    _seed_legacy(conn, "https://jobs/4", "USD100,000", title="Principal",
                 seniority="manager")  # pre-existing tier must survive

    updated = database.backfill_seniority(conn)
    assert updated == 2  # rows 1 and 2; "Engineer" carries no tier signal
    assert _row(conn, "https://jobs/1")["seniority"] == "senior"
    assert _row(conn, "https://jobs/2")["seniority"] == "intern"
    assert _row(conn, "https://jobs/3")["seniority"] is None
    assert _row(conn, "https://jobs/4")["seniority"] == "manager"

    # Idempotent second pass: only the still-NULL title row remains a
    # candidate and still classifies to nothing.
    assert database.backfill_seniority(conn) == 0


def test_init_db_runs_backfills_on_legacy_rows(app_dir, isolated_db):
    """A store upgraded in place gets the structured columns AND the
    backfilled values on the next startup."""
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", "USD90,000-USD110,000/yearly",
                 title="Junior Developer")
    conn.close()

    conn2 = database.init_db()
    row = _row(conn2, "https://jobs/1")
    assert row["salary_min"] == 90000.0
    assert row["salary_max"] == 110000.0
    assert row["seniority"] == "junior"


def test_migration_adds_columns_to_pre_layer1_store(app_dir, isolated_db):
    """A database created before Layer 1 (no structured columns) migrates
    forward without touching existing rows."""
    import sqlite3

    db_path = isolated_db
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, salary TEXT, description TEXT,
            location TEXT, site TEXT, strategy TEXT, discovered_at TEXT,
            fit_score INTEGER, scored_at TEXT
        )
    """)
    legacy.execute(
        "INSERT INTO jobs (url, title, salary) VALUES (?, ?, ?)",
        ("https://jobs/old", "Engineer", "USD100,000-USD120,000/yearly"))
    legacy.commit()
    legacy.close()

    conn = database.init_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col in ("date_posted", "salary_min", "salary_max", "salary_currency",
                "salary_interval", "seniority", "scored_resume_hash"):
        assert col in cols, f"missing {col} after migration"
    row = _row(conn, "https://jobs/old")
    assert row["salary_min"] == 100000.0  # backfill ran over the migrated row
    assert row["scored_resume_hash"] is None  # pre-binding rows stay NULL


# ---------------------------------------------------------------------------
# Score binding (scored_resume_hash)
# ---------------------------------------------------------------------------

def test_resume_sha256_matches_hashlib():
    assert resume_sha256("resume body") == hashlib.sha256(
        b"resume body").hexdigest()


def test_persist_score_stamps_resume_hash(app_dir, isolated_db):
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", None, full_description="d" * 300)
    persist_score(conn, "https://jobs/1", 7, "kws", "solid fit",
                  "host", "2026-11-09T00:00:00+00:00",
                  resume_hash="abc123")
    row = _row(conn, "https://jobs/1")
    assert row["scored_resume_hash"] == "abc123"
    assert row["fit_score"] == 7
    assert row["scored_by"] == "host"


def test_persist_score_unbound_hash_is_null(app_dir, isolated_db):
    """Existing behavior preserved: no hash passed → NULL = pre-binding."""
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", None, full_description="d" * 300)
    persist_score(conn, "https://jobs/1", 7, "kws", "solid fit",
                  "llm", "2026-11-09T00:00:00+00:00")
    assert _row(conn, "https://jobs/1")["scored_resume_hash"] is None


def test_host_score_verb_stamps_current_resume_hash(app_dir, isolated_db):
    """`agent score` binds the score to the resume.txt it was judged
    against; a resume change later makes this score detectably stale."""
    from typer.testing import CliRunner

    from kestrel.cli import app

    (config.RESUME_PATH).write_text("My resume text v1", encoding="utf-8")
    conn = database.init_db()
    _seed_legacy(conn, "https://jobs/1", None, full_description="d" * 300)

    result = CliRunner().invoke(app, [
        "agent", "score", "--url", "https://jobs/1",
        "--score", "8", "--reasoning", "good match",
    ])
    assert result.exit_code == 0, result.stdout

    row = _row(conn, "https://jobs/1")
    assert row["scored_resume_hash"] == resume_sha256("My resume text v1")

    # After a resume edit, the binding no longer matches — the staleness
    # signal the Layer 3 rerank flow will key on.
    stale = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE scored_resume_hash != ?",
        (resume_sha256("My resume text v2 — edited"),),
    ).fetchone()[0]
    assert stale == 1
