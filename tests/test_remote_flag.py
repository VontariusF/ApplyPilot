"""Remote-flag tests (spec PR D): heuristic units, migration idempotence, backfill.

The is_remote column is an inference, not site data — these tests pin the
heuristic's behavior, the ensure_columns migration (including idempotence),
and the init_db backfill over pre-existing rows.
"""

import sqlite3

from kestrel.database import backfill_is_remote, ensure_columns, get_connection, init_db
from kestrel.enrichment.remote import infer_remote


# ---------------------------------------------------------------------------
# Remote heuristic unit tests
# ---------------------------------------------------------------------------

def test_infer_remote_positive_markers():
    assert infer_remote("Senior Data Engineer", "Remote (US)") is True
    assert infer_remote("Engineer", "Austin, TX", "Great remote culture") is True
    assert infer_remote("Backend dev", None, "Work from home friendly") is True
    assert infer_remote("wfh ok") is True
    assert infer_remote("telecommuting available") is True


def test_infer_remote_negatives():
    assert infer_remote("Engineer", "Austin, TX") is False
    assert infer_remote("Hybrid — 3 days onsite in Austin") is False
    assert infer_remote("Onsite role", "New York") is False
    assert infer_remote(None, "", None) is False
    assert infer_remote() is False


def test_infer_remote_word_boundary():
    # "remote" must be its own word: fragments don't trigger the flag.
    assert infer_remote("remotely close to downtown") is False
    assert infer_remote("hypothermia research") is False
    assert infer_remote("Remote-first team") is True


# ---------------------------------------------------------------------------
# Migration idempotence (ensure_columns)
# ---------------------------------------------------------------------------

def _legacy_jobs_table(conn: sqlite3.Connection) -> None:
    """A pre-PR-D jobs table: discovery columns only, no is_remote."""
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


def test_ensure_columns_adds_is_remote_to_legacy_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    _legacy_jobs_table(conn)

    added = ensure_columns(conn)
    assert "is_remote" in added
    names = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "is_remote" in names

    # Idempotence: a second pass adds nothing.
    assert ensure_columns(conn) == []


def test_ensure_columns_idempotent_on_current_schema(isolated_db):
    conn = init_db()
    assert ensure_columns(conn) == []


# ---------------------------------------------------------------------------
# Backfill: pre-existing rows get an inferred flag at init
# ---------------------------------------------------------------------------

def test_backfill_flags_null_rows_and_skews_nothing_twice(isolated_db):
    conn = init_db()
    for url, location in [("https://x/remote", "Remote (US)"),
                          ("https://x/onsite", "Austin, TX")]:
        conn.execute(
            "INSERT INTO jobs (url, title, location, site) VALUES (?, 'Eng', ?, 'T')",
            (url, location),
        )
    conn.commit()

    assert backfill_is_remote(conn) == 2
    flags = {r["url"]: r["is_remote"] for r in conn.execute(
        "SELECT url, is_remote FROM jobs")}
    assert flags["https://x/remote"] == 1
    assert flags["https://x/onsite"] == 0

    # Idempotent: nothing is NULL afterwards, so a re-run touches nothing.
    assert backfill_is_remote(conn) == 0


def test_init_db_backfills_on_startup(isolated_db):
    conn = init_db()
    conn.execute(
        "INSERT INTO jobs (url, title, location, description, site) "
        "VALUES ('https://x/1', 'Remote Platform Engineer', 'US', '', 'T')"
    )
    conn.commit()

    init_db()  # startup path runs the backfill
    row = get_connection().execute(
        "SELECT is_remote FROM jobs WHERE url = 'https://x/1'").fetchone()
    assert row["is_remote"] == 1
