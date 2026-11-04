"""Portal agent sign-in step: gate cards, polling, login spawn, timeout fallback.

Subprocess fakes patch engine_auth's status functions, so every gate state
renders without the real CLIs. No sleeps: the polling timeout is driven by
a stale `t0` epoch parameter, not by waiting.
"""

import time

from applypilot import engine_auth

SIGNED_OUT = engine_auth.EngineStatus("claude", engine_auth.SIGNED_OUT, None, "/usr/bin/claude")
SIGNED_IN = engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN, "claude.ai", "/usr/bin/claude")
NOT_INSTALLED = engine_auth.EngineStatus("claude", engine_auth.NOT_INSTALLED, None, None)
DROID_NOT_INSTALLED = engine_auth.EngineStatus("droid", engine_auth.NOT_INSTALLED, None, None)
DROID_KEY_MISSING = engine_auth.EngineStatus("droid", engine_auth.SIGNED_OUT, None, "/usr/bin/droid")


def patch_gates(monkeypatch, claude=SIGNED_OUT, droid=DROID_NOT_INSTALLED):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: claude)
    monkeypatch.setattr(engine_auth, "droid_status", lambda *a, **k: droid)


# ---------------------------------------------------------------------------
# Page render — one test per gate state
# ---------------------------------------------------------------------------

def test_gate_page_signed_out(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/engine")
    assert r.status_code == 200
    assert "signed out" in r.text
    assert 'action="/engine/login"' in r.text  # the Sign-in button
    assert "every 2s" in r.text  # polling armed while waiting


def test_gate_page_signed_in_shows_method_and_stops_polling(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    r = client.get("/engine")
    assert r.status_code == 200
    assert "claude.ai" in r.text  # authMethod displayed verbatim (spec A2)
    assert "every 2s" not in r.text
    assert "/engine/login" not in r.text


def test_gate_page_not_installed(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=NOT_INSTALLED)
    r = client.get("/engine")
    assert r.status_code == 200
    assert "not installed" in r.text
    assert "claude.ai/code" in r.text  # install hint
    assert "/engine/login" not in r.text  # nothing to sign in to


def test_droid_card_key_missing_points_at_settings(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, droid=DROID_KEY_MISSING)
    r = client.get("/engine")
    assert r.status_code == 200
    assert "key missing" in r.text
    assert "FACTORY_API_KEY" in r.text
    assert 'href="/settings"' in r.text


def test_droid_card_not_installed(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, droid=DROID_NOT_INSTALLED)
    r = client.get("/engine")
    assert "docs.factory.ai" in r.text


# ---------------------------------------------------------------------------
# Polling endpoint and timeout fallback
# ---------------------------------------------------------------------------

def test_status_poll_returns_partial_with_polling(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/engine/status", params={"t0": time.time()})
    assert r.status_code == 200
    assert 'id="engine-gates"' in r.text
    assert "every 2s" in r.text
    # The poll URL preserves the login flag so the waiting hint survives swaps.
    assert "login=1" in r.text or "login=0" in r.text


def test_poll_timeout_falls_back_to_terminal_instructions(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    stale = time.time() - 10_000  # window opened long ago
    r = client.get("/engine/status", params={"t0": stale})
    assert r.status_code == 200
    assert "every 2s" not in r.text  # polling stopped
    assert "claude auth login" in r.text  # terminal command shown
    assert "Check again" in r.text  # one-shot recheck offered
    assert "<details" in r.text and "open" in r.text  # instructions expanded


def test_poll_timeout_did_not_fire_within_window(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/engine/status", params={"t0": time.time()})
    assert "didn't complete" not in r.text  # no premature timeout alert


# ---------------------------------------------------------------------------
# POST /engine/login — spawn, instant-death, spawn failure
# ---------------------------------------------------------------------------

def test_login_route_spawns_and_redirects_to_polling(portal_client, monkeypatch):
    import subprocess

    client, _ = portal_client

    class LiveProc:
        def wait(self, timeout):  # still running after the grace beat
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(engine_auth, "start_login", lambda: (LiveProc(), None))
    r = client.post("/engine/login", follow_redirects=False)
    assert r.status_code == 303
    assert "/engine?login=1" in r.headers["location"]
    assert "t0=" in r.headers["location"]


def test_login_route_reports_instant_death(portal_client, monkeypatch):
    client, _ = portal_client

    class DeadProc:
        def wait(self, timeout):
            return 1

    monkeypatch.setattr(engine_auth, "start_login", lambda: (DeadProc(), None))
    r = client.post("/engine/login", follow_redirects=False)
    assert r.status_code == 303
    assert "login_error=" in r.headers["location"]


def test_login_route_spawn_failure(portal_client, monkeypatch):
    client, _ = portal_client
    monkeypatch.setattr(engine_auth, "start_login", lambda: (None, "could not start"))
    r = client.post("/engine/login", follow_redirects=False)
    assert r.status_code == 303
    assert "login_error=" in r.headers["location"]


def test_login_error_renders_on_page(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/engine", params={"login_error": "boom happened"})
    assert r.status_code == 200
    assert "boom happened" in r.text


# ---------------------------------------------------------------------------
# Onboarding parses the login outcome contract (the dropped-params bug)
# ---------------------------------------------------------------------------

def _login_with_url(url: str) -> engine_auth.LoginProcess:
    """A registry entry whose captured output carries the OAuth URL."""
    login = engine_auth.LoginProcess(proc=object())
    login.output.append(f"Sign in: {url}\n")
    return login


def test_onboarding_renders_login_error(portal_client, monkeypatch):
    """The regression for the dead-button bug: /engine/login redirects here
    with ?login_error=, which this page used to silently drop."""
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/onboarding", params={"login_error": "boom happened"})
    assert r.status_code == 200
    assert "boom happened" in r.text


def test_onboarding_renders_waiting_state_after_login_start(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/onboarding", params={"login": 1, "t0": time.time()})
    assert r.status_code == 200
    assert "Waiting for you to finish the Claude sign-in" in r.text
    assert "boom" not in r.text  # no error alert on the success path


def test_onboarding_accepts_t0_without_timeout_state(portal_client, monkeypatch):
    """t0 parses (the shared contract) but onboarding keeps no timeout
    machinery — its SSE channel flips the gate on the sign-in itself."""
    client, _ = portal_client
    patch_gates(monkeypatch)
    stale = time.time() - 10_000
    r = client.get("/onboarding", params={"login": 1, "t0": stale})
    assert r.status_code == 200
    assert "didn't complete" not in r.text


def test_failed_login_followed_to_onboarding_shows_error(portal_client, monkeypatch):
    """End-to-end over the redirect seam — POST, 303, GET — the exact path
    the owner's click takes; a param-name drift between emitter and page
    fails here."""
    client, _ = portal_client
    monkeypatch.setattr(engine_auth, "start_login", lambda: (None, "could not start"))
    patch_gates(monkeypatch)
    r = client.post("/engine/login", data={"next": "/onboarding"}, follow_redirects=True)
    assert r.status_code == 200
    assert "could not start" in r.text


# ---------------------------------------------------------------------------
# Headless OAuth link — the captured URL renders on both gate pages
# ---------------------------------------------------------------------------

CAPTURED_URL = "https://claude.ai/oauth/authorize?code=abc123&state=xyz"
# Jinja autoescapes & to &amp; inside attribute values (XSS-safe rendering).
ESCAPED_HREF = f'href="{CAPTURED_URL.replace("&", "&amp;")}"'


def test_gate_page_renders_captured_oauth_link(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    monkeypatch.setattr(engine_auth, "_active_login", _login_with_url(CAPTURED_URL))
    r = client.get("/engine")
    assert r.status_code == 200
    assert ESCAPED_HREF in r.text
    assert "No browser window opened?" in r.text


def test_gate_page_hides_oauth_link_when_signed_in(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    monkeypatch.setattr(engine_auth, "_active_login", _login_with_url(CAPTURED_URL))
    r = client.get("/engine")
    assert CAPTURED_URL not in r.text  # signed in — no sign-in link left around


def test_gate_page_without_captured_login_has_no_link(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    r = client.get("/engine")
    assert "No browser window opened?" not in r.text


def test_onboarding_renders_captured_oauth_link_and_waiting_state(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch)
    monkeypatch.setattr(engine_auth, "_active_login", _login_with_url(CAPTURED_URL))
    r = client.get("/onboarding", params={"login": 1, "t0": time.time()})
    assert r.status_code == 200
    assert ESCAPED_HREF in r.text
    assert "Waiting for you to finish the Claude sign-in" in r.text


# ---------------------------------------------------------------------------
# POST /engine/droid-key — the Droid key card (Factory CLI has no login)
# ---------------------------------------------------------------------------

FAKE_KEY = "fk-test-4242-abcd"


def _only_droid_binary(monkeypatch):
    """droid_status()'s which() sees a droid binary; nothing else installed."""
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/droid" if name == "droid" else real_which(name))


def test_droid_card_signed_out_shows_key_form(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, droid=DROID_KEY_MISSING)
    r = client.get("/engine")
    assert r.status_code == 200
    assert 'action="/engine/droid-key"' in r.text
    assert 'type="password"' in r.text  # never renders the value in the clear
    assert "docs.factory.com" in r.text  # where to generate the key
    assert "export FACTORY_API_KEY" in r.text  # terminal fallback
    assert 'href="/settings"' in r.text  # Settings path still offered


def test_droid_card_signed_in_has_no_key_form(portal_client, monkeypatch):
    client, _ = portal_client
    ready = engine_auth.EngineStatus("droid", engine_auth.SIGNED_IN, "api_key", "/usr/bin/droid")
    patch_gates(monkeypatch, droid=ready)
    r = client.get("/engine")
    assert "/engine/droid-key" not in r.text
    assert "Ready" in r.text


def test_droid_key_route_saves_and_flips_signed_in(portal_client, monkeypatch):
    client, _ = portal_client
    _only_droid_binary(monkeypatch)

    r = client.post("/engine/droid-key", data={"factory_key": FAKE_KEY},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/engine"

    from applypilot import config

    env_text = config.ENV_PATH.read_text(encoding="utf-8")
    assert "FACTORY_API_KEY='fk-test-4242-abcd'" in env_text  # merged into .env, quoted (F11)
    assert (config.ENV_PATH.stat().st_mode & 0o777) == 0o600
    import os
    assert os.environ["FACTORY_API_KEY"] == FAKE_KEY  # live refresh, no restart

    # The status flip is observable through the real status function.
    status = engine_auth.droid_status()
    assert status.state == engine_auth.SIGNED_IN
    assert status.auth_method == "api_key"


def test_droid_key_route_flips_onboarding_gate_too(portal_client, monkeypatch):
    """The same flow from the onboarding embed returns to onboarding."""
    client, _ = portal_client
    _only_droid_binary(monkeypatch)
    r = client.post("/engine/droid-key",
                    data={"factory_key": FAKE_KEY, "next": "/onboarding"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding"


def test_droid_key_route_rejects_empty(portal_client, monkeypatch):
    client, _ = portal_client
    r = client.post("/engine/droid-key", data={"factory_key": "   "},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "login_error=" in r.headers["location"]
    import os
    assert "FACTORY_API_KEY" not in os.environ  # nothing written, nothing refreshed
    from applypilot import config
    assert not config.ENV_PATH.exists() or "FACTORY_API_KEY" not in config.ENV_PATH.read_text()


def test_droid_key_route_allows_only_allowlisted_next(portal_client, monkeypatch):
    client, _ = portal_client
    _only_droid_binary(monkeypatch)
    r = client.post("/engine/droid-key",
                    data={"factory_key": FAKE_KEY, "next": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/engine"  # non-allowlisted target falls back


def test_droid_key_never_appears_in_response(portal_client, monkeypatch):
    """Redaction boundary: the pasted key shows up nowhere in the response
    bodies or redirect targets around the save."""
    client, _ = portal_client
    _only_droid_binary(monkeypatch)
    patch_gates(monkeypatch)  # render whatever the cards show after the save

    r = client.post("/engine/droid-key", data={"factory_key": FAKE_KEY},
                    follow_redirects=False)
    assert FAKE_KEY not in r.headers["location"]
    assert FAKE_KEY not in r.text

    followed = client.get("/engine")
    assert followed.status_code == 200
    assert FAKE_KEY not in followed.text
    # The masked settings view keeps only the last four characters visible.
    assert "fk-test" not in followed.text
