# SPDX-License-Identifier: AGPL-3.0-only
"""Kestrel database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from kestrel import config
from kestrel.config import DB_PATH, history_enabled

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()

log = logging.getLogger(__name__)

# Document kinds tracked in document_versions (spec D2)
KIND_RESUME = "resume"
KIND_COVER_LETTER = "cover_letter"

# B16: ORDER BY ... NULLS LAST (database.py queue queries) requires SQLite >= 3.30.
_MIN_SQLITE_VERSION = (3, 30)
_sqlite_version_checked = False


def _check_sqlite_version(version_info: tuple[int, ...] | None = None) -> None:
    """Raise a readable error if the runtime SQLite is too old (B16).

    Args:
        version_info: Override the version to check (for tests). Defaults to
            sqlite3.sqlite_version_info.
    """
    ver = tuple(version_info if version_info is not None else sqlite3.sqlite_version_info)
    if ver < _MIN_SQLITE_VERSION:
        raise RuntimeError(
            f"Kestrel requires SQLite >= {'.'.join(map(str, _MIN_SQLITE_VERSION))} "
            f"(for 'ORDER BY ... NULLS LAST'); found {sqlite3.sqlite_version}. "
            "Upgrade your Python build or system SQLite."
        )


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    # B16: check once per process before any query that uses NULLS LAST.
    global _sqlite_version_checked
    if not _sqlite_version_checked:
        _check_sqlite_version()
        _sqlite_version_checked = True

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path)
    # One busy budget for every connection: PRAGMA busy_timeout overrides
    # the connect(timeout=...) handler, so the old 30s-vs-10s pair was two
    # numbers pretending to be one — and the WAL switch below ran before
    # the pragma existed, on default handling. The pragma is set first, so
    # first-ever WAL conversion on a contended store waits the same
    # budget as every later statement.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def normalize_polluted_urls(conn: sqlite3.Connection | None = None) -> int:
    """Idempotent startup repair for the B1 pollution class — no migration.

    The pre-_clean_cell discovery writer could persist the literal strings
    'None'/'null'/'n/a' into application_url, and enrichment/detail.py's
    relative-URL fixer could then fabricate a plausible '<base>/None' URL
    from such a row. Both are garbage every reader treated as a real apply
    target; NULL them. Runs on every init_db: cheap (a LIKE scan that
    matches nothing on a clean store) and a no-op once clean, so old rows
    heal without a schema migration (the repo's no-unnecessary-migration
    precedent). Returns the number of repaired rows.
    """
    if conn is None:
        conn = get_connection()
    cursor = conn.execute(
        "UPDATE jobs SET application_url = NULL "
        "WHERE lower(application_url) IN ('none', 'null', 'n/a') "
        "OR application_url LIKE '%/None'"
    )
    # Commit unconditionally: in legacy isolation mode even a 0-row UPDATE
    # auto-opens a transaction, and leaving it open on the thread-cached
    # connection breaks the caller's next explicit BEGIN IMMEDIATE with
    # "cannot start a transaction within a transaction".
    conn.commit()
    return cursor.rowcount


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, company, company_domain, salary,
                    description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - PDF:        pdf_generated_at, pdf_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence
      - Leads (spec §3): hiring_leads — profile_url (PK), full_name, title,
                    company_key, source, evidence_url, last_status,
                    discovered_at, decided_at

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Company identity (hiring-leads spec art_u0YGUqpk §1):
            -- resolved at ingestion (JobSpy's parsed name) or backfilled
            -- (Workday registry, Composio header). company_domain is the
            -- employer's own domain when the posting URL carries one
            -- (direct-ATS host, Workday subdomain), else NULL — a
            -- name-confidence identity.
            company               TEXT,
            company_domain        TEXT,

            -- Inferred remote-work flag (spec PR D): 0/1, set during
            -- enrichment and backfilled heuristically. NOT site data —
            -- the UI must label it inferred.
            is_remote             INTEGER,

            -- Structured discovery fields (spec art_we4AUxEy Layer 1):
            -- parsed at ingestion, backfilled from the salary display
            -- string. salary_min/max are ANNUALIZED; salary_interval
            -- keeps the delivered unit. NULL = unknown, never zero.
            date_posted           TEXT,
            salary_min            REAL,
            salary_max            REAL,
            salary_currency       TEXT,
            salary_interval       TEXT,

            -- Deterministic seniority tier (spec Layer 1): one of
            -- intern|junior|mid|senior|staff|principal|lead|manager,
            -- NULL = not classified.
            seniority             TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,
            score_error           TEXT,

            -- Resume binding for the score (spec art_we4AUxEy Layer 1):
            -- sha256 of resume.txt at persist time. NULL = pre-binding
            -- (scored before this column existed) — treated as stale.
            scored_resume_hash    TEXT,


            -- jobs rerank (spec art_we4AUxEy Layer 3): the timestamp
            -- `jobs rerank` stamps when a row enters the re-judgment
            -- set. job-next --stage score serves marked rows before
            -- the pending_score queue; persisting a score clears it.
            -- A presentation-of-intent flag only: STAGE_CONDITIONS and
            -- plan eligibility never read it.
            rerank_marked_at      TEXT,

            -- score_history (spec Layer 3): JSON sidecar, append-only,
            -- capped at the last 5 entries. persist_score appends
            -- {prev_score, prev_score_hash, prev_scored_at, rescored_at}
            -- whenever it overwrites an existing score, so
            -- --rescored-since can report movement without a table.
            score_history         TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- PDF conversion stage (scoring/pdf.py). Tracks whether the
            -- tailored resume's PDF has been generated, so the streaming
            -- pipeline's pending count reads state batch_convert actually
            -- settles instead of re-counting unchanged .txt paths forever.
            pdf_generated_at      TEXT,
            pdf_attempts          INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,
            apply_conflict        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS document_versions (
            -- One row per generated document version (spec D2). Version 1
            -- may be a backfill row pointing at a pre-history legacy
            -- deterministic file; validation_status records the generation
            -- outcome ('legacy' for backfilled rows).
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url           TEXT NOT NULL,
            kind              TEXT NOT NULL,
            version           INTEGER NOT NULL,
            file_path         TEXT NOT NULL,
            validation_status TEXT,
            created_at        TEXT NOT NULL,
            UNIQUE (job_url, kind, version)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_versions_job
            ON document_versions (job_url, kind, version)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_identity (
            -- One row per resolved company (hiring-leads spec
            -- art_u0YGUqpk §1) — the join target for anything that
            -- groups jobs by employer. Keyed by the normalized domain
            -- when the posting URLs carry one (direct-ATS host,
            -- Workday subdomain), else the normalized display name
            -- under the 'name:' prefix (name-confidence).
            company_key   TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL,
            domain        TEXT,
            -- MAX(fit_score) across member jobs (spec §2). Derived
            -- data, recomputed from jobs on every init_db.
            fit_score     REAL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hiring_leads (
            -- One row per hiring lead (hiring-leads spec art_u0YGUqpk
            -- §3). Dedupe rides on profile_url exactly as jobs dedupes
            -- on url. last_status and source hold the LEAD_* vocabularies
            -- defined in this module — the discovery adapters write them
            -- and the agent verbs + portal /leads board read the same
            -- constants, so no surface can drift.
            profile_url   TEXT PRIMARY KEY,
            full_name     TEXT NOT NULL,
            title         TEXT NOT NULL,
            -- FK-by-convention to company_identity (this store enforces
            -- no SQLite FKs; jobs ↔ document_versions works the same way).
            company_key   TEXT NOT NULL,
            source        TEXT NOT NULL,
            -- Where the lead was found — a public evidence page, never a
            -- LinkedIn fetch. NULL when the search result itself is the
            -- only discovery surface.
            evidence_url  TEXT,
            last_status   TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            decided_at    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_captures (
            -- One row per smart-extract site URL the host agent was asked to
            -- capture (browser-removal spec art_VGFSMgRh). status
            -- 'pending' = JS shell over HTTP, awaiting `kestrel agent
            -- page-capture`; 'captured' = host-submitted intelligence
            -- stored as JSON, consumed by the next smart-extract run.
            -- Dedupe rides on url exactly as jobs does.
            url          TEXT PRIMARY KEY,
            status       TEXT NOT NULL DEFAULT 'pending',
            intel        TEXT,
            requested_at TEXT,
            captured_at  TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    backfill_is_remote(conn)
    # Structured pay + seniority backfills (spec Layer 1): parse the legacy
    # salary display zoo and tier the unclassified rows. Both are idempotent
    # and never overwrite non-NULL values.
    backfill_salary_data(conn)
    backfill_seniority(conn)
    # Company identity (hiring-leads spec art_u0YGUqpk §1): resolve each
    # row's employer from the fields the writers already hold, then
    # materialize the per-company mirror (MAX fit across member jobs).
    backfill_company_identity(conn)
    refresh_company_identity(conn)
    # B1: idempotent repair of the pre-_clean_cell 'None'/'n/a' URL class
    # (no migration — see normalize_polluted_urls).
    normalize_polluted_urls(conn)

    return conn


def backfill_is_remote(conn: sqlite3.Connection) -> int:
    """Infer is_remote for rows the enrichment pass hasn't flagged yet.

    Rows discovered before the is_remote column (and rows from sites that
    skip detail scraping) would otherwise be invisible to the remote
    filter forever. The heuristic runs over the text the store already
    holds; the flag stays an inference (spec PR D). Idempotent: only rows
    with a NULL flag are touched. Returns the number of rows backfilled.
    """
    from kestrel.enrichment.remote import infer_remote

    rows = conn.execute(
        "SELECT url, title, location, description, full_description "
        "FROM jobs WHERE is_remote IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE jobs SET is_remote = ? WHERE url = ?",
            (1 if infer_remote(row["title"], row["location"],
                               row["description"], row["full_description"]) else 0,
             row["url"]),
        )
    if rows:
        conn.commit()
    return len(rows)


def backfill_salary_data(conn: sqlite3.Connection) -> int:
    """Parse legacy salary display strings into the structured columns.

    Rows discovered before the structured pay columns (spec Layer 1,
    art_we4AUxEy) carry only the rendered display string. One tolerant
    parse pass at startup gives every parseable row filterable
    salary_min/max (annualized), salary_currency and salary_interval.

    Idempotent: only rows with a salary string but NULL structured range
    are candidates, so a row that parsed once is never re-touched and
    non-NULL values are never overwritten. Rows whose string defeats the
    parser stay candidates (re-parsed each startup — cheap and pure) and
    keep NULLs. Returns the number of rows that gained a range.
    """
    from kestrel.discovery.salary import parse_salary_display

    rows = conn.execute(
        "SELECT url, salary FROM jobs "
        "WHERE salary IS NOT NULL AND salary_min IS NULL AND salary_max IS NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        parsed = parse_salary_display(row["salary"])
        if parsed is None:
            continue
        conn.execute(
            "UPDATE jobs SET salary_min = ?, salary_max = ?, "
            "salary_currency = ?, salary_interval = ? WHERE url = ?",
            (parsed.min_annual, parsed.max_annual,
             parsed.currency, parsed.interval, row["url"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def backfill_seniority(conn: sqlite3.Connection) -> int:
    """Classify seniority for rows the classifier hasn't tiered yet.

    Deterministic keyword pass over title + best description (spec Layer
    1) — no LLM in the loop. Idempotent: only rows with NULL seniority
    AND a title are touched (a titleless row has nothing to classify and
    would otherwise be re-classified on every startup). Never overwrites
    a non-NULL tier. Returns the number of rows classified.
    """
    from kestrel.enrichment.seniority import classify_seniority

    rows = conn.execute(
        "SELECT url, title, description, full_description "
        "FROM jobs WHERE seniority IS NULL AND title IS NOT NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        tier = classify_seniority(row["title"],
                                  row["full_description"] or row["description"])
        if tier is None:
            continue
        conn.execute(
            "UPDATE jobs SET seniority = ? WHERE url = ?", (tier, row["url"])
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def backfill_company_identity(conn: sqlite3.Connection) -> int:
    """Resolve company (and its domain) for rows that have no company.

    Resolution order (hiring-leads spec art_u0YGUqpk §1): JobSpy's
    parsed name (persisted at ingestion going forward), the Workday
    subdomain mapped through config/employers.yaml, the Composio
    writer's "Company: X" description header. Heuristics only — no LLM
    entity resolution, no network.

    Same NULL-only, idempotent contract as backfill_salary_data: only
    rows with a NULL company are candidates, non-NULL values are never
    overwritten, and a row that defeats the resolvers today (e.g. a
    Workday slug missing from the registry) stays a candidate for a
    later run. Returns the number of rows that gained an identity.

    There is deliberately no domain-only backfill: the resolver couples
    the domain to a resolved name (domain_for_company runs only when a
    name was found), so a row with a company but a NULL domain is not a
    candidate — its domain was already derived-or-absent at write time.
    Docstring and code aligned per sweep art_Izev3UNN suggestion 5.
    """
    from kestrel.company_identity import resolve_company_identity
    from kestrel.discovery.workday import load_employers

    rows = conn.execute(
        "SELECT url, company, description FROM jobs WHERE company IS NULL"
    ).fetchall()
    if not rows:
        return 0

    registry = load_employers()
    updated = 0
    for row in rows:
        name, domain = resolve_company_identity(
            row["url"], row["company"], row["description"], registry)
        if name is None:
            continue
        conn.execute(
            "UPDATE jobs SET company = ?, company_domain = ? WHERE url = ?",
            (name, domain, row["url"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def company_fit_aggregates(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Per-company fit aggregates over the store's scored rows (spec §2).

    The one definition of "company fit": MAX(fit_score) across a
    company's member jobs. Only scored rows participate, so every
    aggregate has at least one scored member job by construction.
    Grouping derives the same key the identity layer writes — the
    company's domain when the posting URLs carry one, else the
    normalized name under the 'name:' prefix — so CLI, agent verbs, and
    the portal read one definition and cannot drift.

    Returns {company_key, display_name, domain, fit_score, scored_jobs},
    highest fit first. display_name is the spelling on the row that
    produced the max — the lexicographically smallest spelling on ties,
    so the portal board renders the same name no matter what row order
    SQLite happened to return (sweep art_Izev3UNN suggestion 4).
    """
    from kestrel.company_identity import company_key_for

    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT company, company_domain, fit_score FROM jobs "
        "WHERE company IS NOT NULL AND fit_score IS NOT NULL"
    ).fetchall()

    groups: dict[str, dict] = {}
    for row in rows:
        domain = row["company_domain"]
        key = company_key_for(row["company"], domain)
        agg = groups.get(key)
        if agg is None:
            agg = {"company_key": key, "display_name": row["company"],
                   "domain": domain, "fit_score": row["fit_score"],
                   "scored_jobs": 0}
            groups[key] = agg
        if row["fit_score"] > agg["fit_score"] or (
                row["fit_score"] == agg["fit_score"]
                and (row["company"] or "") < (agg["display_name"] or "")):
            agg["display_name"] = row["company"]
            agg["fit_score"] = row["fit_score"]
        agg["scored_jobs"] += 1
    return sorted(groups.values(),
                  key=lambda a: (-a["fit_score"], a["company_key"]))


def refresh_company_identity(conn: sqlite3.Connection | None = None) -> int:
    """Materialize company_identity from the live aggregates.

    Runs at every init_db so the mirror cannot drift from jobs.
    Everything it writes is derived data — recomputed from the identity
    columns on each run; the NULL-only never-overwrite contract protects
    the jobs rows, not this mirror. Companies whose fit rows are gone
    (job deletion removes rows — sweep art_Izev3UNN finding 6) are
    deleted from the mirror too: an upsert-only refresh left a ghost
    high-match company that `--company` discovery kept scanning against
    a stale MAX fit. Returns the number of keys upserted.
    """
    if conn is None:
        conn = get_connection()
    now = _utc_now()
    aggregates = company_fit_aggregates(conn=conn)
    live_keys = {agg["company_key"] for agg in aggregates}
    for row in conn.execute(
        "SELECT company_key FROM company_identity").fetchall():
        if row["company_key"] not in live_keys:
            conn.execute(
                "DELETE FROM company_identity WHERE company_key = ?",
                (row["company_key"],))
    for agg in aggregates:
        conn.execute(
            "INSERT INTO company_identity (company_key, display_name, domain, "
            "fit_score, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(company_key) DO UPDATE SET "
            "display_name=excluded.display_name, domain=excluded.domain, "
            "fit_score=excluded.fit_score, updated_at=excluded.updated_at",
            (agg["company_key"], agg["display_name"], agg["domain"],
             agg["fit_score"], now),
        )
    conn.commit()
    return len(aggregates)


def high_match_companies(conn: sqlite3.Connection | None = None,
                         min_fit: float | None = None) -> list[dict]:
    """Companies whose best member job reaches the high-match threshold.

    Threshold: config/leads.yaml min_company_fit, falling back to
    config.DEFAULTS['min_company_fit'] (spec §2). At least one scored
    member job is guaranteed by company_fit_aggregates. Same list shape,
    filtered, ordered highest fit first.
    """
    if min_fit is None:
        min_fit = config.leads_min_company_fit()
    return [agg for agg in company_fit_aggregates(conn=conn)
            if agg["fit_score"] >= min_fit]


# ---------------------------------------------------------------------------
# Hiring-lead vocabulary (hiring-leads spec art_u0YGUqpk §3)
#
# One home: the discovery adapters write these values, and the agent
# verbs and portal /leads board read the same constants, so no surface
# can drift. Statuses are deliberately minimal — discovered → dismissed
# | connected, set by the person from the portal; there is no outreach
# state machine because nothing is ever sent.
# ---------------------------------------------------------------------------

LEAD_STATUS_DISCOVERED = "discovered"
LEAD_STATUS_DISMISSED = "dismissed"
LEAD_STATUS_CONNECTED = "connected"
LEAD_STATUSES = (LEAD_STATUS_DISCOVERED, LEAD_STATUS_DISMISSED,
                 LEAD_STATUS_CONNECTED)

LEAD_SOURCE_SERP = "serp"
LEAD_SOURCE_TAVILY = "tavily"
LEAD_SOURCE_PUBLIC_PAGE = "public_page"
LEAD_SOURCE_MANUAL = "manual"
LEAD_SOURCES = (LEAD_SOURCE_SERP, LEAD_SOURCE_TAVILY,
                LEAD_SOURCE_PUBLIC_PAGE, LEAD_SOURCE_MANUAL)


def normalize_profile_url(url: str) -> str:
    """The canonical dedupe key for a lead's profile URL.

    Lowercased host, leading 'www.' stripped, trailing slash stripped,
    query dropped (tracker noise — the same shape map_search_result
    already stores), fragment kept (the public-page person key lives
    there). One person's profile reached the store as three rows under
    www./host-case/trailing-slash spellings of one LinkedIn URL (sweep
    art_Izev3UNN finding 7) because dedupe keyed on the raw string.
    """
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.hostname:
        return url
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[len("www."):]
    path = parsed.path.rstrip("/")
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"https://{host}{path}{fragment}"


def _collapse_lead_variants(conn: sqlite3.Connection,
                            profile_urls: list[str]) -> None:
    """Merge duplicate lead rows that are one person — deliberately.

    The survivor is the oldest discovered_at row (ties: lexicographic
    URL), so the queue a person works down keeps its head. The status
    merge never silently drops a user decision: the most-decided status
    across the variants (connected > dismissed > discovered) wins, with
    the decided_at that recorded it. Everything else on the survivor is
    already there — identity fields are only ever written at ingestion.
    """
    if len(profile_urls) < 2:
        return
    marks = ", ".join("?" for _ in profile_urls)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM hiring_leads WHERE profile_url IN ({marks})",
        profile_urls)]
    if len(rows) < 2:
        return
    _DECIDED_RANK = {LEAD_STATUS_DISCOVERED: 0, LEAD_STATUS_DISMISSED: 1,
                     LEAD_STATUS_CONNECTED: 2}
    survivor = min(rows, key=lambda r: (r["discovered_at"], r["profile_url"]))
    decided = max(rows, key=lambda r: (_DECIDED_RANK[r["last_status"]],
                                       r["decided_at"] or ""))
    if decided["last_status"] != survivor["last_status"]:
        conn.execute(
            "UPDATE hiring_leads SET last_status = ?, decided_at = ? "
            "WHERE profile_url = ?",
            (decided["last_status"], decided["decided_at"],
             survivor["profile_url"]))
    for row in rows:
        if row["profile_url"] != survivor["profile_url"]:
            conn.execute("DELETE FROM hiring_leads WHERE profile_url = ?",
                         (row["profile_url"],))
    conn.commit()


def upsert_hiring_lead(conn: sqlite3.Connection,
                       *,
                       profile_url: str,
                       full_name: str,
                       title: str,
                       company_key: str,
                       source: str,
                       evidence_url: str | None = None,
                       discovered_at: str | None = None,
                       last_status: str = LEAD_STATUS_DISCOVERED) -> bool:
    """Insert one hiring lead; dedupe on the normalized profile URL.

    The one write path for hiring_leads — adapters now, the verbs and
    portal later — so the vocabulary checks cannot be bypassed. A status
    or source outside the LEAD_* vocabulary raises ValueError (a caller
    bug, never silently coerced); so does a missing identity field.
    Dedupe keys on normalize_profile_url, not the raw spelling, and
    upsert-time reconciliation collapses pre-existing variants of the
    same person on the next write (sweep art_Izev3UNN finding 7).
    """
    if last_status not in LEAD_STATUSES:
        raise ValueError(
            f"unknown lead status {last_status!r}; expected one of {LEAD_STATUSES}")
    if source not in LEAD_SOURCES:
        raise ValueError(
            f"unknown lead source {source!r}; expected one of {LEAD_SOURCES}")
    profile_url = (profile_url or "").strip()
    full_name = (full_name or "").strip()
    title = (title or "").strip()
    company_key = (company_key or "").strip()
    if not profile_url or not full_name or not title or not company_key:
        raise ValueError(
            "lead requires profile_url, full_name, title and company_key")
    # Variant reconciliation before insert: a write of one spelling of
    # a person's URL is the trigger that collapses their duplicates.
    existing = [row["profile_url"] for row in conn.execute(
        "SELECT profile_url FROM hiring_leads").fetchall()
        if normalize_profile_url(row["profile_url"])
        == normalize_profile_url(profile_url)]
    if existing:
        _collapse_lead_variants(conn, existing)
        return False
    cursor = conn.execute(
        "INSERT INTO hiring_leads (profile_url, full_name, title, company_key, "
        "source, evidence_url, last_status, discovered_at, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(profile_url) DO NOTHING",
        (profile_url, full_name, title, company_key, source, evidence_url,
         last_status, discovered_at or _utc_now(), None),
    )
    conn.commit()
    return cursor.rowcount == 1


def hiring_leads_rows(conn: sqlite3.Connection | None = None,
                      *,
                      company_key: str | None = None,
                      status: str | None = None) -> list[dict]:
    """Stored leads, optionally filtered by company and status.

    The read path the agent verbs (and the portal board) share, beside
    the LEAD_* vocabulary above so no surface can drift from it. Status
    is validated against LEAD_STATUSES — a caller bug, not silently
    empty. Ordered oldest first: the queue a person works down.
    """
    if conn is None:
        conn = get_connection()
    if status is not None and status not in LEAD_STATUSES:
        raise ValueError(
            f"unknown lead status {status!r}; expected one of {LEAD_STATUSES}")
    clauses, params = [], []
    if company_key:
        clauses.append("company_key = ?")
        params.append(company_key)
    if status:
        clauses.append("last_status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT profile_url, full_name, title, company_key, source, "
        "evidence_url, last_status, discovered_at, decided_at "
        f"FROM hiring_leads{where} ORDER BY discovered_at, profile_url",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def mark_hiring_lead(conn: sqlite3.Connection, profile_url: str,
                     status: str) -> bool:
    """Transition one lead's last_status to dismissed or connected.

    The one write path for lead status decisions — the portal's
    POST /leads/mark calls it (the verbs are read-only over decisions).
    Only the two decided statuses are valid targets: "discovered" is the
    ingestion state, never a decision, so it is refused here. Values come
    from the LEAD_* vocabulary above — the same constants the adapters
    write and every surface reads, so no caller can drift. Sets
    decided_at (the user decision timestamp); unknown profile_url
    returns False rather than raising.
    """
    if status not in (LEAD_STATUS_DISMISSED, LEAD_STATUS_CONNECTED):
        raise ValueError(
            f"a lead can only be marked {LEAD_STATUS_DISMISSED!r} or "
            f"{LEAD_STATUS_CONNECTED!r}, got {status!r}")
    profile_url = (profile_url or "").strip()
    if not profile_url:
        raise ValueError("mark_hiring_lead requires profile_url")
    cursor = conn.execute(
        "UPDATE hiring_leads SET last_status = ?, decided_at = ? "
        "WHERE profile_url = ?",
        (status, _utc_now(), profile_url),
    )
    conn.commit()
    return cursor.rowcount == 1


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    # Company identity (hiring-leads spec art_u0YGUqpk §1): derived at
    # ingestion (JobSpy) or backfilled (Workday registry, Composio
    # header) — backfill_company_identity never overwrites once set.
    "company": "TEXT",
    "company_domain": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Inferred remote-work flag (spec PR D) — heuristic, labeled inferred in the UI.
    "is_remote": "INTEGER",
    # Structured discovery fields (spec art_we4AUxEy Layer 1). salary_min/max
    # are annualized; salary_interval keeps the delivered unit.
    "date_posted": "TEXT",
    "salary_min": "REAL",
    "salary_max": "REAL",
    "salary_currency": "TEXT",
    "salary_interval": "TEXT",
    # Deterministic seniority tier (spec Layer 1); NULL = not classified.
    "seniority": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    # Resume binding for the score (spec Layer 1): sha256 of resume.txt at
    # persist time; NULL = pre-binding, treated as stale.
    "scored_resume_hash": "TEXT",
    # jobs rerank (spec Layer 3): the re-judgment intent flag stamped by
    # `jobs rerank`, cleared when a score is persisted. Advisory only.
    "rerank_marked_at": "TEXT",
    # score_history (spec Layer 3): JSON sidecar of the last 5 rescore
    # entries — {prev_score, prev_score_hash, prev_scored_at, rescored_at}.
    "score_history": "TEXT",
    "scored_at": "TEXT",
    # Last scoring failure, cleared on the next successful score (E).
    "score_error": "TEXT",
    # Score provenance (spec art_IMViYTMU): 'host' for protocol-written
    # scores, the provider name for LLM-written ones, NULL for historical
    # rows — never backfilled.
    "scored_by": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # PDF conversion (scoring/pdf.py)
    "pdf_generated_at": "TEXT",
    "pdf_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Apply-time document snapshot (spec D3): the document_versions ids that
    # were actually uploaded when the application was submitted.
    "applied_resume_version_id": "INTEGER",
    "applied_cover_version_id": "INTEGER",
    # Writer-coordination note (fix 5): set when a worker result met a
    # conflicting manual mark and neither side was clobbered — the board and
    # detail page show it so the desync is visible instead of silent.
    "apply_conflict": "TEXT",
    # Board curation (spec Jobs panel): the user dismissed this listing from
    # the board. A presentation flag with a timestamp — never a pipeline
    # stage; STAGE_CONDITIONS and the automation queries ignore it.
    "dismissed_at": "TEXT",
}


def _add_column(conn: sqlite3.Connection, col: str, dtype: str) -> bool:
    """ALTER TABLE jobs ADD COLUMN, tolerating a concurrent identical ALTER.

    The lock below serializes threads in this process, but the portal and
    CLI run init_db in separate processes and SQLite only serializes the
    writes — the loser of that race gets 'duplicate column name' after
    the winner committed. That is the winner's success, not a failure:
    re-read the pragma to confirm the column now exists and report it as
    added (P2-3). Any other OperationalError propagates.
    """
    try:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
        present = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if col in present:
            return True
        raise  # 'duplicate column' phrasing without the column present: real error


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    # B4.8/spike-confirmed race: two threads can both pass the PRAGMA check
    # before either commits its ALTER TABLE, raising a duplicate-column
    # OperationalError. The lock serializes threads in this process; the
    # same race across processes (portal vs CLI) is tolerated in
    # _add_column, so the lock is advisory rather than load-bearing (P2-3).
    with _migration_lock:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        added = []

        for col, dtype in _ALL_COLUMNS.items():
            if col not in existing:
                # PRIMARY KEY columns can't be added via ALTER TABLE, but url
                # is always created with the table itself so this is safe
                if "PRIMARY KEY" in dtype:
                    continue
                if _add_column(conn, col, dtype):
                    added.append(col)

        if added:
            conn.commit()

    return added


# B4.8: serializes ensure_columns across threads (confirmed migration race).
_migration_lock = threading.Lock()


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description, undescribed,
            scored, scored_fresh, score_failed, unscored, pending_score,
            tailored, untailored_eligible, with_cover_letter, applied,
            prepared, score_distribution

    Accounting (S2): `unscored`, `scored_fresh` and `score_failed` are
    disjoint and partition the described rows —
        unscored + scored_fresh + score_failed == with_description
        undescribed + with_description == total
    `scored` (fit_score IS NOT NULL) deliberately overlaps `score_failed`
    when a rescore failed: the previous good score is kept by design and
    carries a live `score_error`, so the row counts in both. Terminal
    apply statuses (`captcha`, `manual_marked`, `expired`) are reported
    as separate buckets so every apply_status reconciles to total.

    `pending_score` (F2) is NOT part of that partition — it is the
    retried queue (described, no live score, errored rows included),
    identical to STAGE_CONDITIONS["pending_score"]; unscored + the
    described `score_failed` rows reconcile to it.
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage. H3: skip-site rows are settled by policy (the
    # scraper refuses them) — they must not count as pending enrichment
    # work the CLI runner will never do.
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL "
        f"AND {SKIP_DETAIL_CLAUSE}"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage. S2 accounting: a failed RESCORE leaves the previous
    # fit_score/scored_by in place and sets score_error (stability by
    # design — a good score is not destroyed because a rescore errored),
    # so such a row appears in BOTH `scored` and `score_failed`. The
    # disjoint buckets are `scored_fresh` (live score, no error) and
    # `score_failed`; together with `unscored` they partition every
    # described row exactly once:
    #   unscored + scored_fresh + score_failed == with_description
    # and undescribed + with_description == total.
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["scored_fresh"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score IS NOT NULL AND score_error IS NULL"
    ).fetchone()[0]

    stats["score_failed"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE score_error IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL "
        "AND score_error IS NULL"
    ).fetchone()[0]

    stats["undescribed"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NULL"
    ).fetchone()[0]

    # F2: the actual pending-score queue the scorer drains — described
    # rows without a live score, INCLUDING previously-errored ones
    # (run_scoring's default path retries them via STAGE_CONDITIONS
    # ["pending_score"]). Read from the same shared condition so the
    # stat cannot drift from the queue; `unscored` above stays the
    # narrower partition bucket (never-errored) for the S2 identity.
    stats["pending_score"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {STAGE_CONDITIONS['pending_score']}"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Score provenance (spec art_IMViYTMU): host-written vs LLM-written
    # vs pre-column rows. S1 symmetry: both arms require a LIVE score
    # (fit_score IS NOT NULL) — a row whose rescore failed keeps its
    # stale attribution but must not be counted as freshly attributed;
    # a scored_by row without a score counts in none of the three.
    # 'provider' buckets every non-host attribution (openai, gemini,
    # local, ...); NULL scored_by is 'unattributed' — historical rows
    # are never backfilled.
    stats["score_provenance"] = {
        "host": conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE scored_by = 'host' AND fit_score IS NOT NULL"
        ).fetchone()[0],
        "provider": conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE scored_by IS NOT NULL "
            "AND scored_by != 'host' AND fit_score IS NOT NULL"
        ).fetchone()[0],
        "unattributed": conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND scored_by IS NULL"
        ).fetchone()[0],
    }

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL "
        # M3a: the queue never tailors an attempts-exhausted row — the
        # stat must not count one as pending tailoring.
        "AND COALESCE(tailor_attempts, 0) < ?",
        (config.DEFAULTS["max_tailor_attempts"],),
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= ? "
        "AND tailored_resume_path IS NULL",
        (config.DEFAULTS["max_tailor_attempts"],),
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= ? "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')",
        (config.DEFAULTS["max_cover_attempts"],),
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    # Captcha blocks: terminal by decision, reported separately from the
    # other apply errors (a captcha block is not an application failure —
    # the application never happened).
    stats["captcha"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'captcha'"
    ).fetchone()[0]

    # S4: the other terminal apply statuses — manual-ATS marks (the
    # destination of every acquire-time manual skip) and expired
    # postings — surfaced so every apply_status reconciles to total.
    stats["manual_marked"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'manual'"
    ).fetchone()[0]

    stats["expired"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'expired'"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL "
        "AND COALESCE(apply_status, '') != 'captcha'"
    ).fetchone()[0]

    # B9/M3b: readiness carries the queue's URL-validity and min-score
    # guards — a literal 'None' URL is not ready, and a row rescored below
    # the threshold is not pending apply.
    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        f"AND {APPLY_URL_VALID_SQL} "
        "AND fit_score >= ?",
        (config.DEFAULTS["min_score"],),
    ).fetchone()[0]

    # F4: prepared is the human-confirmation checkpoint (apply-result
    # --status prepared; applied_at stays NULL). Additive beside
    # ready_to_apply — each row stays an independent lens (S2-style), so
    # the awaiting-approval gate is visible accounting instead of a job
    # silently merging into the ready bucket (or dropping out of view).
    stats["prepared"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'prepared'"
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


# --- Shared vocabulary and queue predicates (one source of truth) -----------

# The apply-status vocabulary's terminal set (agent_protocol's apply-result
# records these with the attempts ban via the launcher's permanent
# classification): once a row carries one of these statuses it is retired,
# and reset_failed must never resurrect it. Canonical here — the lowest
# module both the launcher and the agent protocol import — so the two
# cannot drift.
TERMINAL_APPLY_STATUSES = ("expired", "captcha", "login_issue")

# Sites the detail scraper never scrapes (they block it). Canonical here so
# the scraper's WHERE, the pending_detail stat, the streaming enrich count,
# and the portal's stage filter agree (H3): a skip-site row with no scraped
# description is settled by policy, never "pending work nobody will do".
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}
SKIP_DETAIL_CLAUSE = (
    "site NOT IN (" + ", ".join(f"'{s}'" for s in sorted(SKIP_DETAIL_SITES)) + ")"
)

# A stored application_url counts as real only if it is an http(s) URL (B9):
# the pre-_clean_cell writer could store the literal 'None' string, which is
# truthy and passed `IS NOT NULL` while pointing the browser at nothing.
APPLY_URL_VALID_SQL = "application_url LIKE 'http%'"


# Stage -> WHERE condition, shared by get_jobs_by_stage and the portal's
# filtered_jobs (spec PR D). "?" placeholders are min-score binds.
STAGE_CONDITIONS: dict[str, str] = {
    "discovered": "1=1",
    "pending_detail": f"detail_scraped_at IS NULL AND {SKIP_DETAIL_CLAUSE}",
    "enriched": "full_description IS NOT NULL",
    "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
    "scored": "fit_score IS NOT NULL",
    "score_failed": "score_error IS NOT NULL",
    "pending_tailor": (
        "fit_score >= ? AND full_description IS NOT NULL "
        # V3: the attempts cap is config.DEFAULTS' — the same number the
        # host verbs' stage_exhausted refusal reads.
        f"AND tailored_resume_path IS NULL "
        f"AND COALESCE(tailor_attempts, 0) < {config.DEFAULTS['max_tailor_attempts']}"
    ),
    "tailored": "tailored_resume_path IS NOT NULL",
    # Same shape as run_cover_letters' inline queue query (score >= min,
    # tailored materials present, nothing generated yet, attempts left) —
    # shared so the host cover verb drains exactly the LLM path's queue.
    "pending_cover": (
        "fit_score >= ? AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        f"AND COALESCE(cover_attempts, 0) < {config.DEFAULTS['max_cover_attempts']}"
    ),
    "pending_apply": (
        "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
        # B9/M3b: a literal-'None' URL is not ready, and a row rescored
        # below the threshold is not pending apply — the queue's guards.
        f"AND {APPLY_URL_VALID_SQL} AND fit_score >= ?"
    ),
    "applied": "applied_at IS NOT NULL",
    # S4: terminal states the apply vocabulary retires — surfaced as board
    # stages and stats buckets so every row reconciles to total.
    "manual_marked": "apply_status = 'manual'",
    "expired": "apply_status = 'expired'",
}


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100,
                      url: str | None = None) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.
        url: Exact job URL filter — scopes the stage query to one job
            (single-job tailor runs).

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    where = STAGE_CONDITIONS.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    if url:
        where += " AND url = ?"
        params.append(url)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


# ---------------------------------------------------------------------------
# Deep filtering: composable read-only job queries (spec art_we4AUxEy Layer 2)
# ---------------------------------------------------------------------------

# Valid seniority tiers — the same set enrichment/seniority.py classifies
# into. The CLI validates --seniority tokens against this before the
# predicate ever reaches SQL.
VALID_SENIORITY_TIERS = (
    "intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager",
)

# Sort key → SQL expression. Salary sorts on the annualized range's top
# (COALESCE falls back to min when the board delivered a single value).
# NULLS LAST keeps unposted/unpayed/unscored rows from crowding the head;
# url is the deterministic tiebreaker. Cross-currency salary sorts compare
# raw annualized numbers — filtering with --currency first is the honest
# way to rank one currency.
_FILTER_SORT_COLUMNS = {
    "score": "fit_score",
    "date": "date_posted",
    "salary": "COALESCE(salary_max, salary_min)",
    "discovered": "discovered_at",
}


@dataclass
class FilterPredicates:
    """The composable `jobs filter` predicate set (spec Layer 2).

    Every field is optional; a field left at its default contributes no
    clause, and set fields AND together. Zero predicates = full listing.
    The CLI translates flags into this dataclass — it holds no SQL — so
    the portal can call `filter_jobs(FilterPredicates(...))` directly.

    Salary comparisons are annualized (`--min-salary` against
    salary_max — a job's range reaching the threshold counts). When a
    currency is set, rows with a different known currency are excluded;
    rows with NULL salary_currency still match (unknown currency — the
    live store's un-prefixed strings are the profile-locale ones).
    """

    posted_within_days: int | None = None
    location: str | None = None
    remote: bool | None = None          # True=remote, False=on-site, None=either
    seniority: list[str] | None = None  # any-of
    min_salary: float | None = None
    currency: str | None = None         # uppercased by the builder
    min_score: int | None = None
    max_score: int | None = None
    sites: list[str] | None = None      # any-of, case-insensitive
    keywords: list[str] | None = None   # any-of, title+full_description substring
    exclude_keywords: list[str] | None = None
    stale_only: bool = False
    # Spec Layer 3: ISO cutoff — keep only rows whose latest score_history
    # entry's rescored_at is at or after this moment. A Python post-filter
    # (the JSON sidecar is not a column SQL can index); derived prev_score/
    # score_delta/rescored_at keys ride every result regardless.
    rescored_since: str | None = None
    sort: str = "discovered"
    ascending: bool = False
    limit: int | None = None


def _escape_like(text: str) -> str:
    """Escape SQL LIKE wildcards so user input matches literally.

    `%` and `_` in a location, keyword, or exclusion are data, not
    patterns — the keyword `50%` must match a literal `50%`, not every
    string containing `50`. Paired with `ESCAPE '\\'` on every LIKE
    clause the builder emits (audit L2).
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def filter_jobs(predicates: FilterPredicates,
                current_resume_hash: str | None = None,
                conn: sqlite3.Connection | None = None) -> list[dict]:
    """Run one composable filter query and return matching job dicts.

    Args:
        predicates: The AND-composed predicate set (see FilterPredicates).
        current_resume_hash: sha256 of the current resume.txt text, used
            to compute each row's `stale` flag (spec Layer 3 semantics:
            a score is stale when scored_resume_hash is NULL or differs).
            Pass None (no resume on disk) and every scored row reports
            stale. Only affects the returned `stale` key, never the row
            set — unless predicates.stale_only is set, which also filters.
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of job dicts (all columns) in sort order, each with a
        derived `stale` bool. Rows already carry the structured
        dimensions (date_posted, salary_min/max/currency/interval,
        seniority, fit_score) — the --json surface forwards them as-is.

    Raises:
        ValueError: Unknown sort key (a CLI-level typed usage error).
    """
    if predicates.sort not in _FILTER_SORT_COLUMNS:
        valid = "|".join(_FILTER_SORT_COLUMNS)
        raise ValueError(f"Unknown sort key {predicates.sort!r} (valid: {valid})")
    if conn is None:
        conn = get_connection()

    where: list[str] = []
    params: list = []

    if predicates.posted_within_days is not None:
        # Lexicographic compare on the date prefix — date_posted holds
        # ISO text, optionally with a time component. Unknown posting
        # dates (NULL) cannot be "within" anything.
        cutoff = (datetime.now(timezone.utc) - timedelta(
            days=predicates.posted_within_days)).date().isoformat()
        where.append("substr(date_posted, 1, 10) >= ?")
        params.append(cutoff)

    if predicates.location:
        where.append("location LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(predicates.location)}%")

    if predicates.remote is not None:
        where.append("is_remote = ?")
        params.append(1 if predicates.remote else 0)

    if predicates.seniority:
        where.append(
            f"lower(seniority) IN ({', '.join('?' * len(predicates.seniority))})")
        params.extend(t.lower() for t in predicates.seniority)

    if predicates.min_salary is not None:
        where.append("salary_max >= ?")
        params.append(predicates.min_salary)
        if predicates.currency:
            # Currency-matched: exclude rows known to be in another
            # currency; NULL-currency rows stay (unknown ≠ foreign).
            where.append("(salary_currency IS NULL OR upper(salary_currency) = ?)")
            params.append(predicates.currency.upper())

    if predicates.min_score is not None:
        where.append("fit_score >= ?")
        params.append(predicates.min_score)
    if predicates.max_score is not None:
        where.append("fit_score <= ?")
        params.append(predicates.max_score)

    if predicates.sites:
        where.append(
            f"lower(site) IN ({', '.join('?' * len(predicates.sites))})")
        params.extend(s.lower() for s in predicates.sites)

    searchable = "lower(coalesce(title, '') || ' ' || coalesce(full_description, ''))"
    if predicates.keywords:
        like = [f"{searchable} LIKE ? ESCAPE '\\'" for _ in predicates.keywords]
        where.append(f"({' OR '.join(like)})")
        params.extend(f"%{_escape_like(k.lower())}%" for k in predicates.keywords)
    if predicates.exclude_keywords:
        for keyword in predicates.exclude_keywords:
            where.append(f"{searchable} NOT LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(keyword.lower())}%")

    if predicates.stale_only:
        # Spec: scored_resume_hash != current resume hash (or NULL). An
        # empty fallback hash marks every score stale when no resume
        # exists to compare against. Parenthesized — this OR is ANDed
        # with every other clause.
        where.append(
            "(scored_resume_hash IS NULL OR scored_resume_hash != ?)")
        params.append(current_resume_hash or "")

    query = "SELECT * FROM jobs"
    if where:
        query += " WHERE " + " AND ".join(where)
    column = _FILTER_SORT_COLUMNS[predicates.sort]
    direction = "ASC" if predicates.ascending else "DESC"
    query += f" ORDER BY {column} {direction} NULLS LAST, url ASC"
    if predicates.limit is not None:
        query += " LIMIT ?"
        params.append(predicates.limit)

    rows = conn.execute(query, params).fetchall()
    results = []
    for row in rows:
        job = dict(zip(row.keys(), row))
        job["stale"] = job["scored_resume_hash"] != (current_resume_hash or "")
        # Spec Layer 3 delta reporting: the newest score_history entry is
        # the score this row just replaced. Malformed sidecar JSON degrades
        # to 'no history' — never crashes the query.
        history = parse_score_history(job.get("score_history"))
        latest = history[-1] if history else None
        job["prev_score"] = latest.get("prev_score") if latest else None
        job["rescored_at"] = latest.get("rescored_at") if latest else None
        job["score_delta"] = (
            job["fit_score"] - job["prev_score"]
            if latest is not None and job["fit_score"] is not None
            and job["prev_score"] is not None else None)
        if predicates.rescored_since is not None:
            # The --rescored-since gate: ISO timestamps compare
            # lexicographically. Rows never rescored cannot be "since".
            if latest is None or not (latest.get("rescored_at") or ""):
                continue
            if latest["rescored_at"] < predicates.rescored_since:
                continue
        results.append(job)
    return results


# ---
# jobs rerank + score-delta plumbing (spec art_we4AUxEy Layer 3)
# ---

# The score_history sidecar keeps the last N rescore entries (locked spec
# decision: JSON sidecar, not a table; a new table only if verification
# shows a query need).
SCORE_HISTORY_CAP = 5


def parse_score_history(raw: str | None) -> list[dict]:
    """Parse the score_history JSON sidecar into its entry list.

    Tolerant by design: a missing, empty, or malformed sidecar is 'no
    history' ([]) — a hand-edited store must not crash a read-only
    filter. A non-list payload or non-dict entries are dropped the same
    way.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def mark_rerank(conn: sqlite3.Connection, urls: list[str], when: str) -> int:
    """Stamp `when` onto each URL's rerank_marked_at — the re-judgment set.

    Only described, non-dismissed rows are markable (an undescribed job
    cannot be re-judged — it needs enrich first; a dismissed job is
    board-level curation the user removed from the pipeline, so it never
    re-enters the scoring loop through rerank — audit Finding 3);
    callers get back how many rows actually entered the set. Advisory
    only: no STAGE_CONDITIONS, plan eligibility, or staged plan token
    reads this column — spec locked decision, staleness never disturbs
    plan semantics.

    Callers commit.
    """
    if not urls:
        return 0
    marks = ", ".join("?" for _ in urls)
    cursor = conn.execute(
        f"UPDATE jobs SET rerank_marked_at = ? "
        f"WHERE url IN ({marks}) AND full_description IS NOT NULL "
        f"AND dismissed_at IS NULL",
        (when, *urls),
    )
    return cursor.rowcount


def get_rerank_jobs(conn: sqlite3.Connection | None = None,
                    limit: int = 1) -> list[dict]:
    """The current rerank set: marked, described rows, newest mark first.

    What `agent job-next --stage score` serves before the pending_score
    queue — "exactly these first". Rows leave the set when their new
    score persists (persist_score clears the mark).
    """
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE rerank_marked_at IS NOT NULL "
        "AND full_description IS NOT NULL "
        "ORDER BY rerank_marked_at DESC, url ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(zip(row.keys(), row)) for row in rows]


# ---------------------------------------------------------------------------
# Document version history (spec PR F, decisions D1/D2)
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentClaimConflict(RuntimeError):
    """A document write lost the claim race (audit F2).

    The job's version slot was reserved by a concurrent writer between
    this caller's read and its claim, or the store's write lock was busy
    past the busy_timeout. Typed and retryable: the protocol renders it
    as a conflict envelope, never an internal_error — the losing writer
    of a parallel tailor/cover race must be able to branch on it.
    """


@dataclass
class DocumentClaim:
    """A reserved version slot (audit F2): the row is inserted and the
    job pointer flipped inside one immediate transaction BEFORE any file
    is written, so concurrent writers can no longer interleave file
    bytes behind one version number. version/version_id are None when
    document history is off (spec D1) — the claim still owns the pointer.
    """

    version: int | None
    version_id: int | None
    file_path: str


def _backfill_legacy_version(conn: sqlite3.Connection, job_url: str, kind: str,
                             legacy_path: str | Path | None) -> bool:
    """Insert the legacy backfill row for a pre-history document, if any.

    No commit — callers own the transaction (spec D2): a job whose
    document predates history has its file at the deterministic legacy
    path; the first versioned write backfills that file as version 1
    (status 'legacy') and the legacy file itself is never touched.
    """
    if legacy_path and Path(legacy_path).exists():
        conn.execute(
            "INSERT INTO document_versions "
            "(job_url, kind, version, file_path, validation_status, created_at) "
            "VALUES (?, ?, 1, ?, 'legacy', ?)",
            (job_url, kind, str(legacy_path), _utc_now()),
        )
        return True
    return False


def _next_version_core(conn: sqlite3.Connection, job_url: str, kind: str,
                       legacy_path: str | Path | None = None) -> int:
    """next_document_version without a commit — callers own the transaction."""
    row = conn.execute(
        "SELECT MAX(version) FROM document_versions WHERE job_url = ? AND kind = ?",
        (job_url, kind),
    ).fetchone()
    max_version = row[0] if row else None
    if max_version is None and _backfill_legacy_version(conn, job_url, kind, legacy_path):
        return 2
    return (max_version or 0) + 1


def next_document_version(conn: sqlite3.Connection, job_url: str, kind: str,
                          legacy_path: str | Path | None = None) -> int:
    """Return the next version number for a job's document kind.

    Legacy compatibility (spec D2): a job whose document predates history has
    its file at the deterministic legacy path. The first versioned write
    backfills that file as version 1 (status 'legacy') and returns 2 — the
    legacy file itself is never touched. The regeneration trigger clears the
    jobs pointer before the tailor stage runs, so the legacy file on disk is
    the durable signal, not the pointer. A job with no prior document starts
    at version 1.

    Committing: use claim_document_version for the write paths — a version
    number taken outside the claim's immediate transaction is a TOCTOU race
    (audit F2).
    """
    version = _next_version_core(conn, job_url, kind, legacy_path)
    conn.commit()
    return version


def record_document_version(conn: sqlite3.Connection, job_url: str, kind: str,
                            version: int, file_path: str | Path,
                            validation_status: str | None = None) -> int:
    """Insert a document version row and return its id (the version id).

    Args:
        conn: Database connection.
        job_url: The job the document was generated for.
        kind: KIND_RESUME or KIND_COVER_LETTER.
        version: Version number (from next_document_version).
        file_path: Path the document was written to.
        validation_status: Generation outcome (e.g. 'approved',
            'failed_validation') or 'legacy' for backfilled rows.
    """
    cur = conn.execute(
        "INSERT INTO document_versions "
        "(job_url, kind, version, file_path, validation_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_url, kind, version, str(file_path), validation_status, _utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def claim_document_version(
    conn: sqlite3.Connection,
    job_url: str,
    kind: str,
    *,
    path_for: Callable[[int], str],
    legacy_path: str | Path | None,
    validation_status: str | None,
    pointer_column: str,
    stamp_column: str,
    attempts_column: str,
    history_on: bool,
) -> DocumentClaim:
    """Reserve a document's version slot and flip the job pointer atomically.

    The write-path counterpart of acquire_job's BEGIN IMMEDIATE discipline
    (audit F2): parallel tailor/cover processes on the same job once raced
    through MAX(version)+1 and a post-hoc pointer check, doubled the version
    INSERT into an IntegrityError, and let the loser's file writes (which
    preceded the INSERT) clobber the winner's bytes on disk. Here one
    BEGIN IMMEDIATE transaction re-checks the pointer, allocates the
    version, inserts the version row, and updates the pointer — and the
    caller writes files only after the claim commits, to the path the
    claim returned. The transaction is the claim: a concurrent second
    writer sees the first's pointer flip and gets DocumentClaimConflict
    instead of an IntegrityError.

    Column names come from the two persist paths' literals, never from
    input. The caller's connection must have no open implicit transaction
    with uncommitted work it expects to survive independently — any is
    committed at entry to start the claim from a clean boundary (both
    call sites persist only after their generation loop closed).
    """
    # Commit first: in legacy isolation mode even a read-only statement
    # sequence can leave an implicit transaction open, and starting
    # BEGIN IMMEDIATE under one raises "cannot start a transaction within
    # a transaction" (the pattern _repair_bad_application_urls documents).
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise DocumentClaimConflict(
            f"{kind} claim for {job_url} lost the store's write lock — "
            "retry once the concurrent writer finishes") from exc
    try:
        pointer = conn.execute(
            f"SELECT {pointer_column} FROM jobs WHERE url = ?", (job_url,)
        ).fetchone()
        if pointer is None:
            conn.rollback()
            raise ValueError(f"claim_document_version: no job row at {job_url}")
        if pointer[0]:
            conn.rollback()
            raise DocumentClaimConflict(
                f"{kind} slot for {job_url} was just claimed by a concurrent "
                f"writer ({pointer[0]}) — drain the queue again for the next job")
        if history_on:
            version = _next_version_core(conn, job_url, kind, legacy_path)
            file_path = path_for(version)
            cur = conn.execute(
                "INSERT INTO document_versions "
                "(job_url, kind, version, file_path, validation_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_url, kind, version, file_path, validation_status, _utc_now()),
            )
            claim = DocumentClaim(version=version, version_id=int(cur.lastrowid),
                                  file_path=file_path)
        else:
            # History off (spec D1): no version row; the claim still owns
            # the pointer. path_for is called with 0 and should return the
            # unversioned path.
            file_path = path_for(0)
            claim = DocumentClaim(version=None, version_id=None, file_path=file_path)
        conn.execute(
            f"UPDATE jobs SET {pointer_column}=?, {stamp_column}=?, "
            f"{attempts_column}=COALESCE({attempts_column},0)+1 WHERE url=?",
            (file_path, _utc_now(), job_url),
        )
        conn.commit()
        return claim
    except Exception:
        conn.rollback()
        raise


def get_document_versions(conn: sqlite3.Connection | None = None,
                          job_url: str | None = None,
                          kind: str | None = None) -> list[dict]:
    """Version rows for a job (optionally one kind), oldest first."""
    if conn is None:
        conn = get_connection()
    sql = "SELECT * FROM document_versions"
    clauses: list[str] = []
    params: list = []
    if job_url is not None:
        clauses.append("job_url = ?")
        params.append(job_url)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY kind, version"
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(row.keys(), row)) for row in rows]


def resolve_version_id(conn: sqlite3.Connection, job_url: str, kind: str,
                       file_path: str | None) -> int | None:
    """The version id whose file_path matches, or None (pre-history/unknown).

    Pre-history documents (written before the history feature, or with
    history disabled) have no version row; their snapshot id is honestly
    NULL rather than guessed.
    """
    if not file_path:
        return None
    row = conn.execute(
        "SELECT id FROM document_versions "
        "WHERE job_url = ? AND kind = ? AND file_path = ? "
        "ORDER BY version DESC LIMIT 1",
        (job_url, kind, str(file_path)),
    ).fetchone()
    return row[0] if row else None


def count_applications_on(day: str) -> int:
    """Applications recorded on calendar day `day` (YYYY-MM-DD, UTC).

    The automation daily cap reads this — applied_at is a UTC ISO timestamp
    written by the apply worker and the mark routes.
    """
    conn = get_connection()
    return conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE applied_at IS NOT NULL AND date(applied_at) = ?",
        (day,),
    ).fetchone()[0]


def count_apply_eligible(min_score: int = 7) -> int:
    """Jobs the apply worker could pick up right now (scheduler guard).

    Mirrors the queue predicate of apply/launcher.acquire_job's non-target
    branch — tailored, not locked (or a stale lock), under the apply
    attempt limit, scored at or above min_score, and not on a blocked
    site/URL pattern or a manual-ATS site. Keep the two in sync; the
    launcher's query owns the semantics, this count only answers "is
    there anything to apply to".
    """
    stale_minutes = config.DEFAULTS["apply_stale_lock_minutes"]
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=stale_minutes)).isoformat()
    conn = get_connection()
    params: list = [cutoff, config.DEFAULTS["max_apply_attempts"], min_score]
    site_clause = ""
    blocked_sites, blocked_patterns = config.load_blocked_sites()
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND site NOT IN ({placeholders})"
        params.extend(blocked_sites)
    url_clauses = ""
    if blocked_patterns:
        url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
        params.extend(blocked_patterns)
    # acquire_job skips manual-ATS sites after the SQL (is_manual_ats on
    # application_url or url); the count must skip them too, or a
    # scheduled run sees eligible jobs the worker refuses and burns a
    # tracked run per manual-ATS job. LIKE is case-insensitive for ASCII
    # by default, matching is_manual_ats's lowercased containment check.
    ats_clauses = ""
    manual_domains = config.load_sites_config().get("manual_ats", [])
    if manual_domains:
        ats_clauses = " ".join(
            "AND COALESCE(application_url, url) NOT LIKE ?"
            for _ in manual_domains)
        params.extend(f"%{domain.lower()}%" for domain in manual_domains)
    row = conn.execute(f"""
        SELECT COUNT(*) FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed'
               -- stale prepared checkpoints recover like stale in_progress
               -- locks (launcher._queue_where is the one selection truth)
               OR (apply_status IN ('in_progress', 'prepared')
                   AND last_attempted_at IS NOT NULL
                   AND last_attempted_at <= ?))
          -- captcha is terminal by decision: a blocked job is never requeued
          AND COALESCE(apply_status, '') != 'captcha'
          AND (apply_attempts IS NULL OR apply_attempts < ?)
          AND fit_score >= ?
          {site_clause}
          {url_clauses}
          {ats_clauses}
    """, params).fetchone()
    return row[0]


def job_apply_in_progress(url: str) -> bool:
    """True when an apply run currently holds this job's lock (P2-4).

    One predicate for every route that would mutate or delete documents an
    active apply run is using (apply-now, regenerate, tailor-now): the
    worker's lock is `apply_status = 'in_progress'` until mark_result
    records its outcome.
    """
    conn = get_connection()
    return conn.execute(
        "SELECT 1 FROM jobs WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    ).fetchone() is not None


def set_job_dismissed(conn: sqlite3.Connection | None = None, url: str = "",
                      dismissed: bool = True) -> bool:
    """Dismiss/undismiss a job on the board (spec Jobs panel).

    Sets or clears dismissed_at. Board-level curation only: pipeline stage
    queries (STAGE_CONDITIONS) never consult this flag, so automation is
    unaffected. Returns True when the job exists.
    """
    if conn is None:
        conn = get_connection()
    stamp = datetime.now(timezone.utc).isoformat() if dismissed else None
    cur = conn.execute("UPDATE jobs SET dismissed_at = ? WHERE url = ?",
                       (stamp, url))
    conn.commit()
    return cur.rowcount > 0


def reset_job_tailoring(conn: sqlite3.Connection, url: str,
                        include_cover: bool = False) -> bool:
    """Clear a job's tailoring state so the tailor stage re-enqueues it (D3).

    Keeps fit_score and full_description (the tailor stage needs both) and
    resets the attempt counters so a previously-exhausted job can try again.
    Also clears the PDF bookkeeping: the new resume is a different document,
    so the stale pdf_generated_at/pdf_attempts of the previous one must not
    keep it out of the pdf stage's pending count. With history off the
    regenerated resume overwrites the same .txt stem, so the previous
    output at that stem is deleted — it renders the OLD resume and PDF
    reconciliation would otherwise mark the regenerated document as done
    from the stale file. The .pdf sibling is deleted too: legacy output
    from the pre-HTML renderer. With history on the replacement
    lands at a fresh _v{N} stem; the old pairs stay on disk — history is
    never deleted. Version rows also stay on disk in both modes.
    Returns True when the job exists.
    """
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return False

    old_path = row["tailored_resume_path"]
    if old_path and old_path.lower().endswith(".txt") and not history_enabled():
        # .html is the current print-ready output, .pdf the legacy one —
        # either settling on disk would mark the regenerated resume done.
        for suffix in (".html", ".pdf"):
            stale_out = Path(old_path).with_suffix(suffix)
            try:
                stale_out.unlink()
            except FileNotFoundError:
                pass  # nothing to invalidate
            except OSError as e:
                log.warning("Could not remove stale output %s during "
                            "re-tailor reset: %s", stale_out, e)

    sets = ["tailored_resume_path = NULL", "tailored_at = NULL", "tailor_attempts = 0",
            "pdf_generated_at = NULL", "pdf_attempts = 0"]
    params: list = []
    if include_cover:
        sets += ["cover_letter_path = NULL", "cover_letter_at = NULL", "cover_attempts = 0"]
    params.append(url)
    cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", params)
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Page captures: the smart-extract browser_needed queue (spec art_VGFSMgRh)
# ---------------------------------------------------------------------------

def record_capture_needed(conn: sqlite3.Connection, url: str) -> None:
    """Mark a smart-extract site URL as awaiting a host page-capture.

    Idempotent upsert: a re-run of a JS-shell site re-stamps the request so
    requested_at reflects the latest escalation.
    """
    conn.execute(
        "INSERT INTO page_captures (url, status, requested_at) VALUES (?, 'pending', ?) "
        "ON CONFLICT(url) DO UPDATE SET status = 'pending', requested_at = excluded.requested_at",
        (url, _utc_now()),
    )
    conn.commit()


def record_page_capture(conn: sqlite3.Connection, url: str, intel: dict) -> None:
    """Persist host-submitted page intelligence for one site URL.

    The intel dict is the same shape collect_page_intelligence used to
    return, serialized as JSON — the smart-extract pipeline consumes it in
    place of an HTTP fetch it cannot render.
    """
    now = _utc_now()
    conn.execute(
        "INSERT INTO page_captures (url, status, intel, requested_at, captured_at) "
        "VALUES (?, 'captured', ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET status = 'captured', intel = excluded.intel, "
        "captured_at = excluded.captured_at",
        (url, json.dumps(intel), now, now),
    )
    conn.commit()


def get_page_capture(conn: sqlite3.Connection, url: str) -> dict | None:
    """One page_captures row for a URL, with its intel JSON decoded; None
    when the site has never been captured or escalated."""
    row = conn.execute("SELECT * FROM page_captures WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    capture = dict(zip(row.keys(), row))
    if capture.get("intel"):
        try:
            capture["intel"] = json.loads(capture["intel"])
        except ValueError:
            capture["intel"] = None
    return capture


def get_pending_captures(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Site URLs awaiting capture, oldest request first — the queue the
    page-capture verb's next-action payload serves from."""
    rows = conn.execute(
        "SELECT url, requested_at FROM page_captures WHERE status = 'pending' "
        "ORDER BY requested_at, rowid LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(zip(row.keys(), row)) for row in rows]
