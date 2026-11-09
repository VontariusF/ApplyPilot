"""Rename-compatibility contracts: data-dir migration and env-var fallbacks.

The 0.1.0 rename moved the APPLYPILOT_* environment namespace to KESTREL_*
and the data dir from ~/.applypilot to ~/.kestrel. These tests pin the
upgrade promises: a legacy deployment keeps working with no user-side
change (config.env_with_legacy, the typo'd auth opt-out), and the first
run under the new default adopts the legacy data dir wholesale — portal
token mode and SQLite state intact.
"""

import logging
import os
import sqlite3
import stat

import pytest

from kestrel import config
from kestrel.portal import auth

# ---------------------------------------------------------------------------
# env_with_legacy: new name honored, legacy name still honored
# ---------------------------------------------------------------------------


def test_env_new_name_honored(monkeypatch):
    monkeypatch.setenv("KESTREL_TEST_VAR", "new")
    assert config.env_with_legacy("KESTREL_TEST_VAR", "APPLYPILOT_TEST_VAR") == "new"


def test_env_legacy_name_honored(monkeypatch):
    monkeypatch.delenv("KESTREL_TEST_VAR", raising=False)
    monkeypatch.setenv("APPLYPILOT_TEST_VAR", "legacy")
    assert config.env_with_legacy("KESTREL_TEST_VAR", "APPLYPILOT_TEST_VAR") == "legacy"


def test_env_new_name_wins_on_conflict(monkeypatch):
    monkeypatch.setenv("KESTREL_TEST_VAR", "new")
    monkeypatch.setenv("APPLYPILOT_TEST_VAR", "legacy")
    assert config.env_with_legacy("KESTREL_TEST_VAR", "APPLYPILOT_TEST_VAR") == "new"


def test_env_new_name_set_but_empty_defers(monkeypatch):
    monkeypatch.setenv("KESTREL_TEST_VAR", "")
    monkeypatch.setenv("APPLYPILOT_TEST_VAR", "legacy")
    assert config.env_with_legacy("KESTREL_TEST_VAR", "APPLYPILOT_TEST_VAR") == "legacy"


# ---------------------------------------------------------------------------
# Data dir: first-run migration from ~/.applypilot to ~/.kestrel
# ---------------------------------------------------------------------------


def _make_legacy_app_dir(base):
    """Build a legacy ~/.applypilot with a 0600 token and a populated DB."""
    legacy = base / ".applypilot"
    legacy.mkdir()
    token = legacy / "portal_token"
    token.write_text("secret-token\n", encoding="utf-8")
    os.chmod(token, 0o600)
    conn = sqlite3.connect(legacy / "applypilot.db")
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO jobs (title) VALUES ('engineer')")
    conn.commit()
    conn.close()
    return legacy


@pytest.mark.skipif(os.name != "posix", reason="HOME-based home resolution is POSIX")
def test_resolver_migrates_legacy_home(monkeypatch, tmp_path, caplog):
    """No env override + legacy dir present: the resolver moves it, then the
    pre-rename DB name follows, so the first Kestrel run keeps all state."""
    _make_legacy_app_dir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KESTREL_DIR", raising=False)
    monkeypatch.delenv("APPLYPILOT_DIR", raising=False)
    with caplog.at_level(logging.WARNING, logger="kestrel.config"):
        resolved = config._resolve_app_dir()
    assert resolved == tmp_path / ".kestrel"
    assert not (tmp_path / ".applypilot").exists()
    assert (tmp_path / ".kestrel" / "kestrel.db").is_file()
    assert not (tmp_path / ".kestrel" / "applypilot.db").exists()
    token = resolved / "portal_token"
    assert token.read_text(encoding="utf-8") == "secret-token\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    conn = sqlite3.connect(resolved / "kestrel.db")
    assert conn.execute("SELECT title FROM jobs").fetchall() == [("engineer",)]
    conn.close()
    assert any("Migrated data directory" in r.message for r in caplog.records)


def test_migration_skipped_when_default_exists(tmp_path):
    """An existing Kestrel dir is used untouched; the legacy dir survives."""
    default = tmp_path / ".kestrel"
    default.mkdir()
    legacy = _make_legacy_app_dir(tmp_path)
    resolved = config._migrate_legacy_app_dir(default, legacy)
    assert resolved == default
    assert default.is_dir()
    assert legacy.exists(), "legacy dir must never be merged or deleted"


def test_resolver_env_overrides_skip_migration(monkeypatch, tmp_path):
    """KESTREL_DIR points wherever the user says; no dir is moved."""
    legacy = _make_legacy_app_dir(tmp_path)
    monkeypatch.setenv("KESTREL_DIR", str(tmp_path / "custom"))
    resolved = config._resolve_app_dir()
    assert resolved == tmp_path / "custom"
    assert legacy.exists()


def test_resolver_legacy_env_override_honored(monkeypatch, tmp_path):
    """APPLYPILOT_DIR (pre-rename deployments) still picks the data dir."""
    monkeypatch.delenv("KESTREL_DIR", raising=False)
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path / "legacy-env"))
    assert config._resolve_app_dir() == tmp_path / "legacy-env"


# ---------------------------------------------------------------------------
# DB file: applypilot.db -> kestrel.db inside the (adopted) data dir
# ---------------------------------------------------------------------------


def test_legacy_db_rename_carries_wal_siblings(tmp_path):
    """journal_mode=WAL: the -wal/-shm siblings are part of the database."""
    (tmp_path / "applypilot.db").write_bytes(b"main")
    (tmp_path / "applypilot.db-wal").write_bytes(b"wal")
    (tmp_path / "applypilot.db-shm").write_bytes(b"shm")
    config._migrate_legacy_db(tmp_path)
    assert (tmp_path / "kestrel.db").read_bytes() == b"main"
    assert (tmp_path / "kestrel.db-wal").read_bytes() == b"wal"
    assert (tmp_path / "kestrel.db-shm").read_bytes() == b"shm"
    assert not list(tmp_path.glob("applypilot.db*"))


def test_legacy_db_rename_skipped_when_new_exists(tmp_path):
    (tmp_path / "applypilot.db").write_bytes(b"old")
    (tmp_path / "kestrel.db").write_bytes(b"new")
    config._migrate_legacy_db(tmp_path)
    assert (tmp_path / "kestrel.db").read_bytes() == b"new"
    assert (tmp_path / "applypilot.db").read_bytes() == b"old"


def test_legacy_db_rename_noop_without_legacy_file(tmp_path):
    config._migrate_legacy_db(tmp_path)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Per-variable fallbacks at their real call sites
# ---------------------------------------------------------------------------


def test_event_log_new_and_legacy_env(monkeypatch, tmp_path):
    from kestrel.apply import eventlog

    monkeypatch.delenv("KESTREL_EVENT_LOG", raising=False)
    monkeypatch.delenv("APPLYPILOT_EVENT_LOG", raising=False)
    assert eventlog.event_log_path_from_env() is None
    monkeypatch.setenv("APPLYPILOT_EVENT_LOG", str(tmp_path / "legacy.jsonl"))
    assert eventlog.event_log_path_from_env() == tmp_path / "legacy.jsonl"
    monkeypatch.setenv("KESTREL_EVENT_LOG", str(tmp_path / "new.jsonl"))
    assert eventlog.event_log_path_from_env() == tmp_path / "new.jsonl"


def test_droid_factory_dir_new_and_legacy_env(monkeypatch, tmp_path):
    from kestrel.apply.droid import DroidEngine

    monkeypatch.delenv("KESTREL_FACTORY_DIR", raising=False)
    monkeypatch.delenv("APPLYPILOT_FACTORY_DIR", raising=False)
    monkeypatch.setenv("APPLYPILOT_FACTORY_DIR", str(tmp_path / "legacy-factory"))
    assert DroidEngine().factory_dir == tmp_path / "legacy-factory"
    monkeypatch.setenv("KESTREL_FACTORY_DIR", str(tmp_path / "new-factory"))
    assert DroidEngine().factory_dir == tmp_path / "new-factory"


# ---------------------------------------------------------------------------
# Auth opt-out: renamed fix of the original APPLIPILOT typo, legacy kept
# ---------------------------------------------------------------------------


def test_auth_disable_new_var(monkeypatch):
    monkeypatch.delenv("APPLIPILOT_PORTAL_DISABLE_AUTH", raising=False)
    monkeypatch.setenv("KESTREL_PORTAL_DISABLE_AUTH", "1")
    assert auth.auth_disabled() is True


def test_auth_disable_legacy_typo_var(monkeypatch):
    """The deployed typo'd var keeps opting the token check out."""
    monkeypatch.delenv("KESTREL_PORTAL_DISABLE_AUTH", raising=False)
    monkeypatch.setenv("APPLIPILOT_PORTAL_DISABLE_AUTH", "true")
    assert auth.auth_disabled() is True


def test_auth_disable_new_var_beats_legacy(monkeypatch):
    monkeypatch.setenv("KESTREL_PORTAL_DISABLE_AUTH", "0")
    monkeypatch.setenv("APPLIPILOT_PORTAL_DISABLE_AUTH", "1")
    assert auth.auth_disabled() is False


def test_auth_disable_unset(monkeypatch):
    monkeypatch.delenv("KESTREL_PORTAL_DISABLE_AUTH", raising=False)
    monkeypatch.delenv("APPLIPILOT_PORTAL_DISABLE_AUTH", raising=False)
    assert auth.auth_disabled() is False
