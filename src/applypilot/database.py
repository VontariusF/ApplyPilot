"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot import config
from applypilot.config import DB_PATH, history_enabled

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
            f"ApplyPilot requires SQLite >= {'.'.join(map(str, _MIN_SQLITE_VERSION))} "
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


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - PDF:        pdf_generated_at, pdf_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

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

            -- Inferred remote-work flag (spec PR D): 0/1, set during
            -- enrichment and backfilled heuristically. NOT site data —
            -- the UI must label it inferred.
            is_remote             INTEGER,

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
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    backfill_is_remote(conn)

    return conn


def backfill_is_remote(conn: sqlite3.Connection) -> int:
    """Infer is_remote for rows the enrichment pass hasn't flagged yet.

    Rows discovered before the is_remote column (and rows from sites that
    skip detail scraping) would otherwise be invisible to the remote
    filter forever. The heuristic runs over the text the store already
    holds; the flag stays an inference (spec PR D). Idempotent: only rows
    with a NULL flag are touched. Returns the number of rows backfilled.
    """
    from applypilot.enrichment.remote import infer_remote

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


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Inferred remote-work flag (spec PR D) — heuristic, labeled inferred in the UI.
    "is_remote": "INTEGER",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Last scoring failure, cleared on the next successful score (E).
    "score_error": "TEXT",
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
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
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

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
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


# Stage -> WHERE condition, shared by get_jobs_by_stage and the portal's
# filtered_jobs (spec PR D). "?" placeholders are min-score binds.
STAGE_CONDITIONS: dict[str, str] = {
    "discovered": "1=1",
    "pending_detail": "detail_scraped_at IS NULL",
    "enriched": "full_description IS NOT NULL",
    "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
    "scored": "fit_score IS NOT NULL",
    "score_failed": "score_error IS NOT NULL",
    "pending_tailor": (
        "fit_score >= ? AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
    ),
    "tailored": "tailored_resume_path IS NOT NULL",
    "pending_apply": (
        "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ),
    "applied": "applied_at IS NOT NULL",
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
# Document version history (spec PR F, decisions D1/D2)
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """
    row = conn.execute(
        "SELECT MAX(version) FROM document_versions WHERE job_url = ? AND kind = ?",
        (job_url, kind),
    ).fetchone()
    max_version = row[0] if row else None
    if max_version is None and legacy_path and Path(legacy_path).exists():
        conn.execute(
            "INSERT INTO document_versions "
            "(job_url, kind, version, file_path, validation_status, created_at) "
            "VALUES (?, ?, 1, ?, 'legacy', ?)",
            (job_url, kind, str(legacy_path), _utc_now()),
        )
        conn.commit()
        return 2
    return (max_version or 0) + 1


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
               OR (apply_status = 'in_progress' AND last_attempted_at IS NOT NULL
                   AND last_attempted_at <= ?))
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
    regenerated resume overwrites the same .txt stem, so the previous .pdf
    at that stem is deleted — it renders the OLD resume and PDF
    reconciliation would otherwise mark the regenerated document as done
    from the stale file. With history on the replacement lands at a fresh
    _v{N} stem; the old .txt/.pdf pair stays on disk — history is never
    deleted. Version rows also stay on disk in both modes.
    Returns True when the job exists.
    """
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return False

    old_path = row["tailored_resume_path"]
    if old_path and old_path.lower().endswith(".txt") and not history_enabled():
        stale_pdf = Path(old_path).with_suffix(".pdf")
        try:
            stale_pdf.unlink()
        except FileNotFoundError:
            pass  # nothing to invalidate
        except OSError as e:
            log.warning("Could not remove stale PDF %s during re-tailor "
                        "reset: %s", stale_pdf, e)

    sets = ["tailored_resume_path = NULL", "tailored_at = NULL", "tailor_attempts = 0",
            "pdf_generated_at = NULL", "pdf_attempts = 0"]
    params: list = []
    if include_cover:
        sets += ["cover_letter_path = NULL", "cover_letter_at = NULL", "cover_attempts = 0"]
    params.append(url)
    cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", params)
    conn.commit()
    return cur.rowcount > 0
