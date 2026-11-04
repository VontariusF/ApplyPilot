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
