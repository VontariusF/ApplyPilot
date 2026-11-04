"""Engine sign-in gate: status parsing, tri-state classification, login spawn.

Subprocess fakes stand in for the claude/droid binaries so every gate
state is exercised without the real CLIs (absent in CI). Patches target
engine_auth's module attributes (shutil/subprocess are shared modules, so
monkeypatch reverts them per test).
"""

import os
import subprocess

import pytest

from applypilot import engine_auth

CLAUDE_BIN = "/usr/local/bin/claude"
DROID_BIN = "/usr/local/bin/droid"


def patch_which(monkeypatch, installs: dict[str, str | None]):
    monkeypatch.setattr(engine_auth.shutil, "which", lambda name: installs.get(name))


def patch_status_run(monkeypatch, result=None, exc=None):
    def fake_run(*args, **kwargs):
        if exc is not None:
            raise exc
        return result
    monkeypatch.setattr(engine_auth.subprocess, "run", fake_run)


def completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# claude auth status — subprocess parsing and exit codes
# ---------------------------------------------------------------------------

class TestClaudeStatus:
    def test_not_installed(self, monkeypatch):
        patch_which(monkeypatch, {"claude": None})
        status = engine_auth.claude_status()
        assert status.engine == "claude"
        assert status.state == engine_auth.NOT_INSTALLED
        assert status.binary is None
        assert status.auth_method is None

    def test_signed_in_exit_zero(self, monkeypatch):
        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        patch_status_run(monkeypatch, completed(0, '{"authMethod": "claude.ai"}'))
        status = engine_auth.claude_status()
        assert status.state == engine_auth.SIGNED_IN
        assert status.auth_method == "claude.ai"
        assert status.binary == CLAUDE_BIN

    def test_signed_out_exit_one(self, monkeypatch):
        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        patch_status_run(monkeypatch, completed(1, '{"authMethod": "none"}'))
        status = engine_auth.claude_status()
        assert status.state == engine_auth.SIGNED_OUT
        assert status.auth_method == "none"

    def test_signed_in_without_parseable_json_still_signed_in(self, monkeypatch):
        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        patch_status_run(monkeypatch, completed(0, "logged in\n"))
        status = engine_auth.claude_status()
        assert status.state == engine_auth.SIGNED_IN
        assert status.auth_method is None

    def test_exit_one_with_no_json_is_signed_out(self, monkeypatch):
        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        patch_status_run(monkeypatch, completed(1, ""))
        status = engine_auth.claude_status()
        assert status.state == engine_auth.SIGNED_OUT
        assert status.auth_method is None

    def test_timeout_never_reads_as_ready(self, monkeypatch):
        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        patch_status_run(monkeypatch, exc=subprocess.TimeoutExpired(cmd="claude", timeout=10))
        status = engine_auth.claude_status()
        assert status.state == engine_auth.SIGNED_OUT
        assert status.error is not None and "10" in status.error

    def test_spawn_oserror_never_reads_as_ready(self, monkeypatch):
        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        patch_status_run(monkeypatch, exc=PermissionError(13, "permission denied"))
        status = engine_auth.claude_status()
        assert status.state == engine_auth.SIGNED_OUT
        assert status.error is not None and "permission denied" in status.error

    def test_status_command_shape(self, monkeypatch):
        """The check runs `<binary> auth status` capture-style, bounded by timeout."""
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return completed(0, "{}")

        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        monkeypatch.setattr(engine_auth.subprocess, "run", fake_run)
        engine_auth.claude_status()
        assert seen["argv"] == [CLAUDE_BIN, "auth", "status"]
        assert seen["kwargs"]["capture_output"] is True
        assert seen["kwargs"]["timeout"] == engine_auth.STATUS_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# authMethod parsing (JSON may ride with banner lines)
# ---------------------------------------------------------------------------

class TestAuthMethodParsing:
    def test_json_with_surrounding_banner_lines(self):
        out = "claude v1.2.3\nWelcome\n{\"authMethod\": \"oauth_token\"}\n"
        assert engine_auth._parse_auth_method(out) == "oauth_token"

    def test_object_without_method_key(self):
        assert engine_auth._parse_auth_method('{"ok": true}') is None

    def test_non_json_output(self):
        assert engine_auth._parse_auth_method("not json at all") is None

    def test_method_across_multiple_objects(self):
        out = '{"other": 1}\n{"authMethod": "api_key_helper"}'
        assert engine_auth._parse_auth_method(out) == "api_key_helper"

    def test_documented_method_values_round_trip(self):
        for method in ("none", "claude.ai", "oauth_token", "api_key",
                       "api_key_helper", "third_party"):
            assert engine_auth._parse_auth_method(f'{{"authMethod": "{method}"}}') == method


# ---------------------------------------------------------------------------
# Droid gate — FACTORY_API_KEY presence (no status subcommand)
# ---------------------------------------------------------------------------

class TestDroidStatus:
    def test_not_installed(self, monkeypatch):
        patch_which(monkeypatch, {"droid": None})
        monkeypatch.setenv("FACTORY_API_KEY", "fk-even-a-key-does-not-help")
        status = engine_auth.droid_status()
        assert status.engine == "droid"
        assert status.state == engine_auth.NOT_INSTALLED

    def test_installed_without_key_is_signed_out(self, monkeypatch):
        patch_which(monkeypatch, {"droid": DROID_BIN})
        monkeypatch.delenv("FACTORY_API_KEY", raising=False)
        status = engine_auth.droid_status()
        assert status.state == engine_auth.SIGNED_OUT
        assert status.binary == DROID_BIN
        assert status.auth_method is None

    def test_installed_with_key_is_signed_in(self, monkeypatch):
        patch_which(monkeypatch, {"droid": DROID_BIN})
        monkeypatch.setenv("FACTORY_API_KEY", "fk-test")
        status = engine_auth.droid_status()
        assert status.state == engine_auth.SIGNED_IN
        assert status.auth_method == "api_key"


# ---------------------------------------------------------------------------
# claude auth login spawn
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def wait(self, timeout=None):
        if self._exit_code is None:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return self._exit_code


class TestStartLogin:
    def test_not_installed(self, monkeypatch):
        patch_which(monkeypatch, {"claude": None})
        proc, error = engine_auth.start_login()
        assert proc is None
        assert error is not None and "not installed" in error

    def test_spawns_login_detached(self, monkeypatch):
        seen: dict = {}

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _FakeProc()

        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        monkeypatch.setattr(engine_auth.subprocess, "Popen", fake_popen)
        proc, error = engine_auth.start_login()
        assert error is None
        assert seen["argv"] == [CLAUDE_BIN, "auth", "login"]
        assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
        if os.name != "posix":
            assert seen["kwargs"]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP

    def test_spawn_failure_returns_error(self, monkeypatch):
        def fake_popen(*args, **kwargs):
            raise OSError("no exec bit")

        patch_which(monkeypatch, {"claude": CLAUDE_BIN})
        monkeypatch.setattr(engine_auth.subprocess, "Popen", fake_popen)
        proc, error = engine_auth.start_login()
        assert proc is None
        assert error is not None and "no exec bit" in error


class TestLoginInstantDeath:
    def test_running_process_returns_none(self):
        assert engine_auth.login_instant_death(_FakeProc(exit_code=None), grace=0.01) is None

    def test_exited_process_reports_code(self):
        msg = engine_auth.login_instant_death(_FakeProc(exit_code=3), grace=0.01)
        assert msg is not None and "3" in msg


# ---------------------------------------------------------------------------
# Tier integration: get_tier requires a signed-in engine for tier 3
# ---------------------------------------------------------------------------

def _tier3_env(monkeypatch):
    from applypilot import config

    for key in ("LLM_PROVIDER", "LLM_URL", "LLM_MODEL", "GEMINI_API_KEY",
                "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_URL", "http://ep:8080/v1")
    monkeypatch.setattr(config, "get_chrome_path", lambda: "/usr/bin/chrome")


def _patch_statuses(monkeypatch, claude_state, droid_state):
    monkeypatch.setattr(
        engine_auth, "claude_status",
        lambda *a, **k: engine_auth.EngineStatus("claude", claude_state, None, CLAUDE_BIN),
    )
    monkeypatch.setattr(
        engine_auth, "droid_status",
        lambda *a, **k: engine_auth.EngineStatus("droid", droid_state, None, DROID_BIN),
    )


def test_get_tier_3_with_signed_in_claude(monkeypatch):
    from applypilot import config

    _tier3_env(monkeypatch)
    _patch_statuses(monkeypatch, engine_auth.SIGNED_IN, engine_auth.NOT_INSTALLED)
    assert config.get_tier() == 3


def test_get_tier_2_when_claude_installed_but_signed_out(monkeypatch):
    """The bug this PR fixes: binary presence alone used to grant tier 3."""
    from applypilot import config

    _tier3_env(monkeypatch)
    _patch_statuses(monkeypatch, engine_auth.SIGNED_OUT, engine_auth.NOT_INSTALLED)
    assert config.get_tier() == 2


def test_get_tier_3_with_keyed_droid_alone(monkeypatch):
    from applypilot import config

    _tier3_env(monkeypatch)
    _patch_statuses(monkeypatch, engine_auth.NOT_INSTALLED, engine_auth.SIGNED_IN)
    assert config.get_tier() == 3


def test_engine_gap_messages_name_the_signed_out_state(monkeypatch):
    from applypilot import config

    _patch_statuses(monkeypatch, engine_auth.SIGNED_OUT, engine_auth.SIGNED_OUT)
    gaps = config._engine_gap_messages()
    assert any("claude auth login" in g for g in gaps)
    assert any("FACTORY_API_KEY" in g for g in gaps)


def test_engine_gap_messages_install_hint_when_nothing_installed(monkeypatch):
    from applypilot import config

    _patch_statuses(monkeypatch, engine_auth.NOT_INSTALLED, engine_auth.NOT_INSTALLED)
    gaps = config._engine_gap_messages()
    assert len(gaps) == 1
    assert "claude.ai/code" in gaps[0]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
