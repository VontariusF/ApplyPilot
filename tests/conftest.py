"""Shared fixtures for the remediation test suite.

All DB-touching tests run against a throwaway SQLite file so they never touch
a real ~/.applypilot directory.
"""

import threading

import pytest

import applypilot.database as database


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
