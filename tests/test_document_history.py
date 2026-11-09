"""Document history & regeneration tests (spec PR F, decisions D1/D2/D3).

One test per spec-named verification item:
- version-write (tailor + cover, history on)
- history-disabled overwrite identity
- regeneration clears-and-requeues
- apply-snapshot linkage
- migration idempotence (+ pre-history upgrade path, uniqueness contract)
- legacy-path first regeneration (legacy file never touched)
"""

import json
import sqlite3
from pathlib import Path

import pytest

from kestrel import config, database
from kestrel.scoring import docs
import kestrel.scoring.cover_letter as cover_letter
import kestrel.scoring.tailor as tailor

URL = "https://jobs.example/posting-1"


# ---------------------------------------------------------------------------
# Fixtures/helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def doc_dirs(app_dir, monkeypatch):
    """Repoint the scoring modules' bound output dirs at the isolated app dir."""
    monkeypatch.setattr(tailor, "TAILORED_DIR", config.TAILORED_DIR)
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", config.COVER_LETTER_DIR)
    return config.TAILORED_DIR, config.COVER_LETTER_DIR


@pytest.fixture(autouse=True)
def _pdf_stub(monkeypatch):
    """No Chromium in tests: patch the PDF converter the writers import at call time."""
    monkeypatch.setattr(
        "kestrel.scoring.pdf.convert_to_pdf",
        lambda p: Path(str(p)).with_suffix(".pdf"),
    )


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


def _prefix(url=URL):
    return docs.output_prefix({"title": "Engineer", "site": "acme", "url": url})


class _TailorLLM:
    """Valid tailored-resume JSON; the judge call (if any) passes."""

    def __init__(self, summary="Tailored summary"):
        self.summary = summary

    def chat(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "Judge this tailored resume" in user:
            return "VERDICT: PASS\nISSUES: none"
        return json.dumps({
            "title": "Senior Engineer",
            "summary": self.summary,
            "skills": {"languages": "Python, SQL"},
            "experience": [{"header": "Acme Corp", "subtitle": "2020-2024",
                            "bullets": ["Shipped the platform."]}],
            "projects": [{"header": "Side project", "subtitle": "",
                          "bullets": ["Built a thing."]}],
            "education": "State University",
        })


class _CoverLLM:
    """A clean cover letter that passes validate_cover_letter (lenient)."""

    def chat(self, messages, **kwargs):
        return ("Dear Hiring Manager,\n\nI am excited to apply for this role. "
                "My background fits well.\n\nSincerely,\nTest Candidate")


@pytest.fixture()
def tailor_env(isolated_db, doc_dirs, monkeypatch):
    """Isolated tailor inputs: schema, resume file, empty profile, canned LLM."""
    database.init_db(str(database.DB_PATH))
    resume = config.RESUME_PATH
    resume.write_text("Base resume text", encoding="utf-8")
    monkeypatch.setattr(tailor, "RESUME_PATH", resume)
    monkeypatch.setattr(tailor, "load_profile", lambda: {"personal": {"full_name": "Test"}})
    monkeypatch.setattr(tailor, "get_client", lambda: _TailorLLM())


@pytest.fixture()
def cover_env(isolated_db, doc_dirs, monkeypatch):
    """Isolated cover-letter inputs: schema, resume file, empty profile, canned LLM."""
    database.init_db(str(database.DB_PATH))
    resume = config.RESUME_PATH
    resume.write_text("Base resume text", encoding="utf-8")
    monkeypatch.setattr(cover_letter, "RESUME_PATH", resume)
    monkeypatch.setattr(cover_letter, "load_profile", lambda: {"personal": {"full_name": "Test"}})
    monkeypatch.setattr(cover_letter, "get_client", lambda: _CoverLLM())


# ---------------------------------------------------------------------------
# Migration idempotence
# ---------------------------------------------------------------------------


def test_migration_idempotent(isolated_db):
    conn = database.init_db(str(database.DB_PATH))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(document_versions)")}
    assert {"id", "job_url", "kind", "version", "file_path",
            "validation_status", "created_at"} <= cols
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_document_versions_job'"
    ).fetchone()
    assert idx is not None

    # Re-running init on the same database changes nothing.
    database.init_db(str(database.DB_PATH))
    assert {r[1] for r in conn.execute("PRAGMA table_info(document_versions)")} == cols


def test_migration_upgrades_prehistory_db(isolated_db):
    """A database from before the history feature gains the table + snapshot columns."""
    raw = sqlite3.connect(str(database.DB_PATH))
    raw.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    raw.commit()
    raw.close()

    conn = database.init_db(str(database.DB_PATH))
    doc_cols = {r[1] for r in conn.execute("PRAGMA table_info(document_versions)")}
    assert "version" in doc_cols
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"applied_resume_version_id", "applied_cover_version_id"} <= job_cols


def test_document_versions_unique_contract(isolated_db):
    conn = database.init_db(str(database.DB_PATH))
    database.record_document_version(conn, URL, "resume", 1, "/p1")
    with pytest.raises(sqlite3.IntegrityError):
        database.record_document_version(conn, URL, "resume", 1, "/p2")


# ---------------------------------------------------------------------------
# Version writes (history on)
# ---------------------------------------------------------------------------


def test_tailor_version_write_history_on(tailor_env, doc_dirs, monkeypatch):
    monkeypatch.delenv("HISTORY_ENABLED", raising=False)  # default: enabled
    tailored_dir, _ = doc_dirs
    conn = database.get_connection()
    _seed_job(conn)

    result = tailor.run_tailoring()
    assert result["approved"] == 1

    prefix = _prefix()
    v1 = tailored_dir / f"{prefix}_v1.txt"
    assert v1.exists()
    assert (tailored_dir / f"{prefix}_v1_JOB.txt").exists()
    assert (tailored_dir / f"{prefix}_v1_REPORT.json").exists()

    # jobs pointer follows the latest version; apply stage unchanged.
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] == str(v1)

    versions = database.get_document_versions(conn, job_url=URL, kind="resume")
    assert len(versions) == 1
    v = versions[0]
    assert v["version"] == 1
    assert v["file_path"] == str(v1)
    assert v["validation_status"] == "approved"
    assert v["created_at"]


def test_cover_version_write_history_on(cover_env, doc_dirs, monkeypatch):
    monkeypatch.delenv("HISTORY_ENABLED", raising=False)
    _, cover_dir = doc_dirs
    conn = database.get_connection()
    _seed_job(conn, tailored_resume_path=str(config.TAILORED_DIR / "r.txt"))

    result = cover_letter.run_cover_letters()
    assert result["generated"] == 1

    prefix = _prefix()
    v1 = cover_dir / f"{prefix}_v1_CL.txt"
    assert v1.exists()
    row = conn.execute("SELECT cover_letter_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["cover_letter_path"] == str(v1)

    versions = database.get_document_versions(conn, job_url=URL, kind="cover_letter")
    assert len(versions) == 1
    v = versions[0]
    assert v["version"] == 1
    assert v["file_path"] == str(v1)
    assert v["validation_status"] == "approved"


# ---------------------------------------------------------------------------
# History disabled: exact legacy overwrite behavior
# ---------------------------------------------------------------------------


def test_history_disabled_overwrite_identity(tailor_env, doc_dirs, monkeypatch):
    monkeypatch.setenv("HISTORY_ENABLED", "off")
    tailored_dir, _ = doc_dirs
    conn = database.get_connection()
    _seed_job(conn)

    monkeypatch.setattr(tailor, "get_client", lambda: _TailorLLM(summary="Generation A"))
    tailor.run_tailoring()
    prefix = _prefix()
    legacy = tailored_dir / f"{prefix}.txt"
    assert legacy.exists()
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] == str(legacy)

    # Regenerate (clear pointer), run again: same deterministic path, no versions.
    assert database.reset_job_tailoring(conn, URL)
    monkeypatch.setattr(tailor, "get_client", lambda: _TailorLLM(summary="Generation B"))
    tailor.run_tailoring()

    assert row["tailored_resume_path"] == str(legacy)
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] == str(legacy)
    assert legacy.read_text(encoding="utf-8").find("Generation B") > -1
    assert not (tailored_dir / f"{prefix}_v1.txt").exists()
    assert database.get_document_versions(conn, job_url=URL) == []


# ---------------------------------------------------------------------------
# Regeneration: clears tailoring state and re-enqueues
# ---------------------------------------------------------------------------


def test_regenerate_clears_and_requeues(isolated_db):
    conn = database.init_db(str(database.DB_PATH))
    _seed_job(conn,
              tailored_resume_path="/old/resume.txt", tailored_at="2026-01-02T00:00:00+00:00",
              tailor_attempts=5,
              cover_letter_path="/old/cl.txt", cover_letter_at="2026-01-02T00:00:00+00:00",
              cover_attempts=3)

    assert database.reset_job_tailoring(conn, URL) is True
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] is None
    assert row["tailored_at"] is None
    assert row["tailor_attempts"] == 0
    # Cover state untouched without --cover.
    assert row["cover_letter_path"] == "/old/cl.txt"
    assert row["cover_attempts"] == 3

    # Re-enqueued for the tailor stage despite previously exhausted attempts.
    queue = database.get_jobs_by_stage(conn=conn, stage="pending_tailor")
    assert any(j["url"] == URL for j in queue)

    # --cover clears cover state too.
    assert database.reset_job_tailoring(conn, URL, include_cover=True) is True
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["cover_letter_path"] is None
    assert row["cover_letter_at"] is None
    assert row["cover_attempts"] == 0

    # Unknown URL reports False.
    assert database.reset_job_tailoring(conn, "https://jobs.example/missing") is False


def test_regenerate_cli_clears_and_requeues(isolated_db, doc_dirs, monkeypatch):
    """The CLI command clears state through the same path (typer wiring)."""
    from typer.testing import CliRunner

    from kestrel.cli import app as cli_app

    runner = CliRunner()
    conn = database.init_db(str(database.DB_PATH))
    _seed_job(conn, tailored_resume_path="/old/resume.txt")

    def _fake_bootstrap():
        database.init_db(str(database.DB_PATH))

    monkeypatch.setattr("kestrel.cli._bootstrap", _fake_bootstrap)
    result = runner.invoke(cli_app, ["regenerate", URL])
    assert result.exit_code == 0, result.output
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] is None


# ---------------------------------------------------------------------------
# Apply-time snapshot linkage
# ---------------------------------------------------------------------------


def test_apply_snapshot_linkage(isolated_db, doc_dirs):
    from kestrel.apply.launcher import acquire_job, mark_result

    conn = database.init_db(str(database.DB_PATH))
    _seed_job(conn, application_url=URL)
    vpath = str(config.TAILORED_DIR / "acme_Engineer_v1.txt")
    vid = database.record_document_version(conn, URL, "resume", 1, vpath,
                                           validation_status="approved")
    conn.execute("UPDATE jobs SET tailored_resume_path = ? WHERE url = ?", (vpath, URL))
    conn.commit()

    job = acquire_job(target_url=URL, min_score=7, worker_id=0, agent_label="test")
    assert job["resume_version_id"] == vid
    assert job["cover_version_id"] is None

    mark_result(URL, "applied", duration_ms=5,
                resume_version_id=job["resume_version_id"],
                cover_version_id=job["cover_version_id"])
    row = conn.execute(
        "SELECT apply_status, applied_resume_version_id, applied_cover_version_id "
        "FROM jobs WHERE url = ?", (URL,)
    ).fetchone()
    assert row["apply_status"] == "applied"
    assert row["applied_resume_version_id"] == vid
    assert row["applied_cover_version_id"] is None


def test_manual_mark_applied_snapshots_version(isolated_db, doc_dirs):
    from kestrel.apply.launcher import mark_job

    conn = database.init_db(str(database.DB_PATH))
    _seed_job(conn)
    vpath = str(config.TAILORED_DIR / "acme_Engineer_v2.txt")
    vid = database.record_document_version(conn, URL, "resume", 2, vpath)
    conn.execute("UPDATE jobs SET tailored_resume_path = ? WHERE url = ?", (vpath, URL))
    conn.commit()

    mark_job(URL, "applied")
    row = conn.execute(
        "SELECT applied_resume_version_id FROM jobs WHERE url = ?", (URL,)
    ).fetchone()
    assert row["applied_resume_version_id"] == vid


# ---------------------------------------------------------------------------
# Legacy compatibility: first regeneration creates v2 alongside the legacy file
# ---------------------------------------------------------------------------


def test_legacy_first_regeneration(tailor_env, doc_dirs, monkeypatch):
    monkeypatch.delenv("HISTORY_ENABLED", raising=False)
    tailored_dir, _ = doc_dirs
    conn = database.get_connection()

    prefix = _prefix()
    legacy = tailored_dir / f"{prefix}.txt"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("LEGACY ORIGINAL", encoding="utf-8")
    _seed_job(conn, tailored_resume_path=str(legacy))

    # Regeneration trigger first (clears the pointer), then the tailor stage runs.
    assert database.reset_job_tailoring(conn, URL)
    result = tailor.run_tailoring()
    assert result["approved"] == 1

    # The legacy file is untouched; the new document lands at v2 alongside it.
    assert legacy.read_text(encoding="utf-8") == "LEGACY ORIGINAL"
    v2 = tailored_dir / f"{prefix}_v2.txt"
    assert v2.exists()

    versions = database.get_document_versions(conn, job_url=URL, kind="resume")
    assert [(v["version"], v["file_path"], v["validation_status"]) for v in versions] == [
        (1, str(legacy), "legacy"),
        (2, str(v2), "approved"),
    ]
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] == str(v2)


# ---------------------------------------------------------------------------
# History toggle
# ---------------------------------------------------------------------------


def test_history_enabled_default_and_env(monkeypatch):
    monkeypatch.delenv("HISTORY_ENABLED", raising=False)
    assert config.history_enabled() is True
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("HISTORY_ENABLED", off)
        assert config.history_enabled() is False
    for on in ("1", "true", "on", "yes"):
        monkeypatch.setenv("HISTORY_ENABLED", on)
        assert config.history_enabled() is True


# ---------------------------------------------------------------------------
# Portal Regenerate action
# ---------------------------------------------------------------------------


def test_portal_regenerate_action(portal_client):
    client, _ = portal_client
    conn = database.get_connection()
    _seed_job(conn, tailored_resume_path="/old/resume.txt")

    r = client.post("/jobs/regenerate", data={"url": URL}, follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (URL,)).fetchone()
    assert row["tailored_resume_path"] is None

    # include_cover clears the cover letter state too.
    conn.execute(
        "UPDATE jobs SET cover_letter_path = '/old/cl.txt', cover_attempts = 2 WHERE url = ?",
        (URL,),
    )
    conn.commit()
    r = client.post("/jobs/regenerate", data={"url": URL, "include_cover": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute(
        "SELECT cover_letter_path, cover_attempts FROM jobs WHERE url = ?", (URL,)
    ).fetchone()
    assert row["cover_letter_path"] is None
    assert row["cover_attempts"] == 0


def test_portal_regenerate_rejects_missing_url(portal_client):
    client, _ = portal_client
    r = client.post("/jobs/regenerate", data={})
    assert r.status_code == 400
