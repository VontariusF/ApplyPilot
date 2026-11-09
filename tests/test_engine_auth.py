"""Engine sign-in gate: status parsing, tri-state classification, login spawn.

Subprocess fakes stand in for the claude/droid binaries so every gate
state is exercised without the real CLIs (absent in CI). Patches target
engine_auth's module attributes (shutil/subprocess are shared modules, so
monkeypatch reverts them per test).
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from kestrel import engine_auth

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
        # Output is captured, not discarded — the OAuth URL the CLI prints
        # is the headless sign-in path. stderr is merged in: a URL printed
        # there is still a sign-in link.
        assert seen["kwargs"]["stdout"] == subprocess.PIPE
        assert seen["kwargs"]["stderr"] == subprocess.STDOUT
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
# OAuth URL capture — fake binaries reproduce `claude auth login` output
# ---------------------------------------------------------------------------

def _wait_for(getter, timeout: float = 2.0):
    """Bounded wait for the capture thread to surface output."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = getter()
        if value:
            return value
        time.sleep(0.02)
    return getter()


@pytest.fixture()
def clean_registry(monkeypatch):
    """Isolate the most-recent-login slot; monkeypatch restores it at teardown."""
    monkeypatch.setattr(engine_auth, "_active_login", None)


def _fake_claude(tmp_path, script: str):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(0o755)
    return fake


class TestExtractOauthUrl:
    def test_url_with_query_params(self):
        text = "Sign in at https://claude.ai/oauth/authorize?code=abc&state=x to continue."
        assert engine_auth.extract_oauth_url(text) == "https://claude.ai/oauth/authorize?code=abc&state=x"

    def test_bare_url(self):
        assert engine_auth.extract_oauth_url("https://claude.ai/x\nmore") == "https://claude.ai/x"

    def test_trailing_punctuation_stripped(self):
        assert engine_auth.extract_oauth_url("Open https://claude.ai/x.") == "https://claude.ai/x"

    def test_first_url_wins(self):
        text = "banner https://a.example/one then https://b.example/two"
        assert engine_auth.extract_oauth_url(text) == "https://a.example/one"

    def test_no_url(self):
        assert engine_auth.extract_oauth_url("nothing here\nreally") is None

    def test_empty(self):
        assert engine_auth.extract_oauth_url("") is None


class TestLoginCapture:
    def test_url_on_stdout_is_captured(self, tmp_path, monkeypatch, clean_registry):
        fake = _fake_claude(tmp_path, "echo 'claude v1.2.3'\n"
                                    "echo 'Browser did not open? Use the url below to log in.'\n"
                                    "echo 'https://claude.ai/oauth/authorize?code=abc123&state=xyz'\n")
        patch_which(monkeypatch, {"claude": str(fake)})
        login, error = engine_auth.start_login()
        assert error is None
        url = _wait_for(lambda: login.oauth_url())
        assert url == "https://claude.ai/oauth/authorize?code=abc123&state=xyz"
        login.proc.wait(timeout=5)

    def test_url_on_stderr_is_captured_too(self, tmp_path, monkeypatch, clean_registry):
        fake = _fake_claude(tmp_path, "echo 'https://claude.ai/oauth/authorize?code=err-path' >&2\n"
                                      "exec sleep 30\n")
        patch_which(monkeypatch, {"claude": str(fake)})
        login, _ = engine_auth.start_login()
        url = _wait_for(lambda: login.oauth_url())
        assert url == "https://claude.ai/oauth/authorize?code=err-path"
        login.proc.kill()  # keep the sleep from outliving the test
        login.proc.wait(timeout=5)

    def test_instant_death_includes_captured_output(self, tmp_path, monkeypatch, clean_registry):
        fake = _fake_claude(tmp_path, "echo 'error: unrecognized subcommand' >&2\nexit 2\n")
        patch_which(monkeypatch, {"claude": str(fake)})
        login, _ = engine_auth.start_login()
        _wait_for(lambda: login.text().strip() or None)
        message = engine_auth.login_instant_death(login, grace=1.0)
        assert message is not None
        assert "code 2" in message
        assert "unrecognized subcommand" in message

    def test_registry_tracks_latest_spawn(self, tmp_path, monkeypatch, clean_registry):
        fake = _fake_claude(tmp_path, "exec sleep 30\n")
        patch_which(monkeypatch, {"claude": str(fake)})
        first, _ = engine_auth.start_login()
        second, _ = engine_auth.start_login()
        assert engine_auth.active_login() is second
        assert engine_auth.active_login() is not first
        for login in (first, second):
            login.proc.kill()
            login.proc.wait(timeout=5)

    def test_missing_binary_leaves_registry_untouched(self, monkeypatch, clean_registry):
        patch_which(monkeypatch, {"claude": None})
        proc, error = engine_auth.start_login()
        assert proc is None
        assert error is not None and "not installed" in error
        assert engine_auth.active_login() is None


# ---------------------------------------------------------------------------
# Tier integration: get_tier requires a signed-in engine for tier 3
# ---------------------------------------------------------------------------

def _tier3_env(monkeypatch):
    from kestrel import config

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
    from kestrel import config

    _tier3_env(monkeypatch)
    _patch_statuses(monkeypatch, engine_auth.SIGNED_IN, engine_auth.NOT_INSTALLED)
    assert config.get_tier() == 3


def test_get_tier_2_when_claude_installed_but_signed_out(monkeypatch):
    """The bug this PR fixes: binary presence alone used to grant tier 3."""
    from kestrel import config

    _tier3_env(monkeypatch)
    _patch_statuses(monkeypatch, engine_auth.SIGNED_OUT, engine_auth.NOT_INSTALLED)
    assert config.get_tier() == 2


def test_get_tier_3_with_keyed_droid_alone(monkeypatch):
    from kestrel import config

    _tier3_env(monkeypatch)
    _patch_statuses(monkeypatch, engine_auth.NOT_INSTALLED, engine_auth.SIGNED_IN)
    assert config.get_tier() == 3


def test_engine_gap_messages_name_the_signed_out_state(monkeypatch):
    from kestrel import config

    _patch_statuses(monkeypatch, engine_auth.SIGNED_OUT, engine_auth.SIGNED_OUT)
    gaps = config._engine_gap_messages()
    assert any("claude auth login" in g for g in gaps)
    assert any("FACTORY_API_KEY" in g for g in gaps)


def test_engine_gap_messages_install_hint_when_nothing_installed(monkeypatch):
    from kestrel import config

    _patch_statuses(monkeypatch, engine_auth.NOT_INSTALLED, engine_auth.NOT_INSTALLED)
    gaps = config._engine_gap_messages()
    assert len(gaps) == 1
    assert "claude.ai/code" in gaps[0]


# ---------------------------------------------------------------------------
# claude_status_cached — the portal's TTL-shared probe (P1-1)
# ---------------------------------------------------------------------------

class TestClaudeStatusCache:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        engine_auth.reset_status_cache()
        yield
        engine_auth.reset_status_cache()

    def test_concurrent_callers_share_one_probe(self, monkeypatch):
        """Simultaneous callers within the TTL spawn ONE subprocess — the lock
        hands every waiter the same fresh result. The engine page's 2s poll,
        the onboarding chat signature and onboarding readiness all probe the
        gate; without the cache each would run its own `claude auth status`."""
        calls: list[int] = []

        def fake_status(*args, **kwargs):
            calls.append(1)
            return engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN,
                                            "claude.ai", CLAUDE_BIN)

        monkeypatch.setattr(engine_auth, "claude_status", fake_status)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: engine_auth.claude_status_cached(), range(8)))
        assert len(calls) == 1  # eight callers, one subprocess
        assert all(r.state == engine_auth.SIGNED_IN for r in results)

    def test_new_probe_identity_invalidates_cache(self, monkeypatch):
        """A changed probe (a newly installed fake, a patched function) always
        misses — a cached fake must never answer for a different one."""
        signed_out = engine_auth.EngineStatus("claude", engine_auth.SIGNED_OUT,
                                              None, CLAUDE_BIN)
        signed_in = engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN,
                                             "claude.ai", CLAUDE_BIN)
        monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: signed_out)
        assert engine_auth.claude_status_cached().state == engine_auth.SIGNED_OUT
        monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: signed_in)
        assert engine_auth.claude_status_cached().state == engine_auth.SIGNED_IN

    def test_expired_ttl_reprobes(self, monkeypatch):
        """Past the TTL the cached value is dropped and the probe re-runs —
        sign-in flips surface within TTL + one poll tick."""
        calls: list[int] = []

        def fake_status(*args, **kwargs):
            calls.append(1)
            return engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN,
                                            None, CLAUDE_BIN)

        monkeypatch.setattr(engine_auth, "claude_status", fake_status)
        engine_auth.claude_status_cached(ttl=500)
        engine_auth.claude_status_cached(ttl=0)  # expired the instant it landed
        assert len(calls) == 2

    def test_reset_forces_a_fresh_probe(self, monkeypatch):
        calls: list[int] = []

        def fake_status(*args, **kwargs):
            calls.append(1)
            return engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN,
                                            None, CLAUDE_BIN)

        monkeypatch.setattr(engine_auth, "claude_status", fake_status)
        engine_auth.claude_status_cached()
        engine_auth.reset_status_cache()
        engine_auth.claude_status_cached()
        assert len(calls) == 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
