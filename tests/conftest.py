"""Shared fixtures for the ApplyPilot test suites.

Two layers of isolation, so no test can touch a real ~/.applypilot:

1. APPLYPILOT_DIR is redirected to a throwaway dir BEFORE the applypilot
   package is imported, so config.py binds APP_DIR (and every derived path)
   to it for tests that don't opt into the app_dir fixture.
2. Tests that need per-test isolation use `app_dir`, which repoints every
   ApplyPilot path constant at a fresh temp directory.

ApplyPilot resolves ~/.applypilot into module-level path constants at import
time; every consumer reads them as attributes at call time, so tests can
repoint the attributes at a temp directory instead of touching the real home
directory.
"""

import os
import tempfile
import threading

import pytest

os.environ.setdefault("APPLYPILOT_DIR", tempfile.mkdtemp(prefix="applypilot-test-"))

from applypilot import config, database  # noqa: E402  (after the env redirect)


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """Repoint every ApplyPilot path at a fresh temp app directory."""
    app = tmp_path / "applypilot"
    app.mkdir()
    paths = {
        "APP_DIR": app,
        "DB_PATH": app / "applypilot.db",
        "PROFILE_PATH": app / "profile.json",
        "PROFILE_MD_PATH": app / "profile.md",
        "RESUME_PATH": app / "resume.txt",
        "RESUME_PDF_PATH": app / "resume.pdf",
        "SEARCH_CONFIG_PATH": app / "searches.yaml",
        "ENV_PATH": app / ".env",
        "ONBOARDING_STATE_PATH": app / "onboarding-state.json",
        "TAILORED_DIR": app / "tailored_resumes",
        "COVER_LETTER_DIR": app / "cover_letters",
        "LOG_DIR": app / "logs",
        "CHROME_WORKER_DIR": app / "chrome-workers",
        "APPLY_WORKER_DIR": app / "apply-workers",
    }
    for name, path in paths.items():
        monkeypatch.setattr(config, name, path)
    # database.py binds DB_PATH via `from applypilot.config import DB_PATH`,
    # so it holds its own copy and must be repointed separately.
    monkeypatch.setattr(database, "DB_PATH", paths["DB_PATH"])
    return app


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point the database layer at a per-test SQLite file."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    yield db_path
    database.close_connection(str(db_path))


@pytest.fixture()
def barrier():
    """A reusable threading.Barrier factory."""
    def make(n: int) -> threading.Barrier:
        return threading.Barrier(n)
    return make


# ---------------------------------------------------------------------------
# Portal fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_env(app_dir, tmp_path, monkeypatch):
    """Full portal isolation: repointed paths, clean env, fresh writers."""
    from applypilot.apply import eventlog

    db_path = app_dir / "applypilot.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.delenv("APPLYPILOT_EVENT_LOG", raising=False)
    eventlog.reset_writer_cache()
    yield app_dir
    database.close_connection(str(db_path))
    eventlog.reset_writer_cache()


@pytest.fixture()
def portal_client(portal_env, monkeypatch):
    """Authenticated portal test client against the isolated store."""
    from fastapi.testclient import TestClient

    from applypilot.portal.app import create_app

    # Speed up SSE idle-close in tests (generators read these at runtime).
    from applypilot.portal import app as portal_app_module
    monkeypatch.setattr(portal_app_module, "POLL_SECONDS", 0.01)

    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers["x-portal-token"] = app.state.token
    yield client, app

