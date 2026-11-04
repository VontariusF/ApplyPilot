"""S2: plaintext secret files are written owner-only (0600) and tightened on load.

Covers config.write_secret_text, ensure_dirs' app-dir tightening, the
load-time tightening of existing loose files, and the wizard's wiring.
"""

import inspect
import json
import os
import stat

import pytest

from applypilot import config

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@posix_only
def test_write_secret_text_creates_owner_only_file(app_dir):
    target = app_dir / ".env"
    config.write_secret_text(target, "GEMINI_API_KEY=abc123\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == "GEMINI_API_KEY=abc123\n"


@posix_only
def test_write_secret_text_tightens_existing_loose_file(app_dir):
    target = app_dir / "profile.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    config.write_secret_text(target, '{"k": "v"}')
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == '{"k": "v"}'


@posix_only
def test_ensure_dirs_tightens_app_dir(app_dir):
    app_dir.chmod(0o755)
    config.ensure_dirs()
    assert stat.S_IMODE(app_dir.stat().st_mode) == 0o700


@posix_only
def test_load_profile_tightens_loose_file(app_dir):
    # Simulate a file written by an older version with default modes.
    target = app_dir / "profile.json"
    target.write_text(json.dumps({"personal": {"full_name": "T"}}), encoding="utf-8")
    target.chmod(0o644)

    profile = config.load_profile()

    assert profile["personal"]["full_name"] == "T"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_load_env_tightens_loose_file(app_dir, monkeypatch):
    target = app_dir / ".env"
    target.write_text("GEMINI_API_KEY=abc123\n", encoding="utf-8")
    target.chmod(0o644)
    # Keep the CWD .env fallback from reading anything else / polluting env.
    monkeypatch.chdir(app_dir)

    config.load_env()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_wizard_secret_writes_route_through_helper():
    """The wizard flow is interactive, so pin its wiring instead: every
    secret-file write goes through config.write_secret_text, never bare
    write_text on PROFILE_PATH/ENV_PATH."""
    from applypilot.wizard import init as wizard

    source = inspect.getsource(wizard)
    assert source.count("write_secret_text(") >= 3
    assert "PROFILE_PATH.write_text" not in source
    assert "ENV_PATH.write_text" not in source
