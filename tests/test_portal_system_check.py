"""System Check page: states, fix routing, in-place re-run, themes.

The zero-terminal diagnostic surface (spec: System Check replaces the CLI
doctor). The check logic itself is imported at its source — engine_auth,
onboarding.readiness, config paths, composio_state — so these tests force
the underlying conditions (files present/absent, engine states, machine
probes) and assert the resulting states and copy, never a reimplementation.
"""

import pytest

from kestrel import config, engine_auth, llm, onboarding
from kestrel.discovery import composio
from kestrel.portal import system_check as sc

# The real composio check, captured before fixtures stub the module attr.
_REAL_COMPOSIO_STATE = composio.composio_state

CHECK_IDS = [c for _h, ids in sc._GROUPS for c in ids]


def _engine(engine: str, state: str) -> engine_auth.EngineStatus:
    return engine_auth.EngineStatus(engine, state, None, f"/usr/bin/{engine}")


@pytest.fixture
def capable_machine(monkeypatch):
    """Deterministic machine probes: everything the sandbox lacks is present."""
    monkeypatch.setattr(config, "get_chrome_path", lambda: "/usr/bin/chrome")
    monkeypatch.setattr(sc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sc.importlib.util, "find_spec", lambda name: object())
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.setattr(composio, "composio_state", lambda: ("optional", ""))


@pytest.fixture
def provider_reachable(monkeypatch):
    """The provider probe answers and parses (no network in tests)."""
    monkeypatch.setattr(llm, "probe_provider",
                        lambda: ("reachable",
                                 "The provider answered a test request."))


@pytest.fixture
def signed_in_everywhere(monkeypatch):
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda: _engine("claude", engine_auth.SIGNED_IN)
    )
    monkeypatch.setattr(
        engine_auth, "droid_status", lambda: _engine("droid", engine_auth.SIGNED_IN)
    )
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)


def _write_setup(app_dir, *, profile=True, resume="txt", searches=True):
    """Seed the onboarding outputs in the isolated app dir."""
    if profile:
        (app_dir / "profile.json").write_text("{}")
    if resume == "txt":
        (app_dir / "resume.txt").write_text("resume")
    elif resume == "pdf":
        (app_dir / "resume.pdf").write_bytes(b"%PDF-1.4")
    if searches:
        (app_dir / "searches.yaml").write_text("searches: []")


# ---------------------------------------------------------------------------
# run_checks: states under healthy, warning, and broken fixtures
# ---------------------------------------------------------------------------


def test_healthy_install_is_all_pass(app_dir, capable_machine,
                                     signed_in_everywhere,
                                     provider_reachable):
    _write_setup(app_dir)
    checks = {c.id: c for c in sc.run_checks()}
    assert sorted(checks) == sorted(CHECK_IDS)
    assert all(c.state == sc.PASS for c in checks.values()), {
        cid: c.state for cid, c in checks.items()
    }


def test_broken_install_fails_the_right_checks(app_dir, capable_machine, monkeypatch):
    """Nothing on disk, no provider, the default engine absent."""
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda: _engine("claude", engine_auth.NOT_INSTALLED)
    )
    monkeypatch.setattr(
        engine_auth, "droid_status", lambda: _engine("droid", engine_auth.NOT_INSTALLED)
    )
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["profile"].state == sc.FAIL
    assert checks["resume"].state == sc.FAIL
    assert checks["provider"].state == sc.FAIL
    assert checks["claude"].state == sc.FAIL
    # Genuinely-optional things stay green even in the broken fixture.
    assert checks["droid"].state == sc.PASS
    assert checks["composio"].state == sc.PASS
    assert checks["capsolver"].state == sc.PASS


def test_warning_states(app_dir, capable_machine, signed_in_everywhere, monkeypatch):
    """PDF-only resume, missing search prefs, unsigned-in engines, and a
    Composio source enabled without its key (through the real check)."""
    _write_setup(app_dir, resume="pdf", searches=False)
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda: _engine("claude", engine_auth.SIGNED_OUT)
    )
    monkeypatch.setattr(
        engine_auth, "droid_status", lambda: _engine("droid", engine_auth.SIGNED_OUT)
    )
    monkeypatch.setattr(composio, "composio_state", _REAL_COMPOSIO_STATE)
    monkeypatch.setattr(composio.config, "load_search_config", lambda: {"composio": {}})
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["resume"].state == sc.WARN
    assert checks["search"].state == sc.WARN
    assert checks["claude"].state == sc.WARN
    assert checks["droid"].state == sc.WARN
    assert checks["composio"].state == sc.WARN


def test_missing_jobspy_reader_warns(
    app_dir, capable_machine, signed_in_everywhere, monkeypatch
):
    _write_setup(app_dir)
    monkeypatch.setattr(sc.importlib.util, "find_spec", lambda name: None)
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["jobspy"].state == sc.WARN


# ---------------------------------------------------------------------------
# Provider check contacts the endpoint (spec reliability fix 1)
# ---------------------------------------------------------------------------


def test_provider_check_fails_when_probe_cannot_reach(
        app_dir, capable_machine, signed_in_everywhere, monkeypatch):
    """A mispointed/unreachable endpoint shows red with the probe's plain
    reason — env-var presence alone is not 'set up'."""
    _write_setup(app_dir)
    reason = ("The provider did not answer within 5 seconds — the endpoint "
              "may be down or the URL mistyped.")
    monkeypatch.setattr(llm, "probe_provider", lambda: ("unreachable", reason))
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["provider"].state == sc.FAIL
    assert reason in checks["provider"].meaning
    assert checks["provider"].fix == sc.Fix("Open provider settings", "/settings")


def test_provider_check_pass_copies_the_probe(
        app_dir, capable_machine, signed_in_everywhere, provider_reachable):
    _write_setup(app_dir)
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["provider"].state == sc.PASS
    assert "answered a test request" in checks["provider"].meaning


def test_provider_check_warns_when_responses_are_unusable(
        app_dir, capable_machine, signed_in_everywhere, monkeypatch):
    """Alive but unusable (incompatible response schema): a green check
    would hide every later parse failure — warn with the probe detail."""
    _write_setup(app_dir)
    detail = ("The provider answered the test request, but the response was "
              "not usable — real runs may still fail. Check the model name "
              "in provider settings.")
    monkeypatch.setattr(llm, "probe_provider", lambda: ("degraded", detail))
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["provider"].state == sc.WARN
    assert detail in checks["provider"].meaning
    assert checks["provider"].fix == sc.Fix("Open provider settings", "/settings")


def test_provider_check_warns_when_provider_is_slow_but_working(
        app_dir, capable_machine, signed_in_everywhere, monkeypatch):
    """P2-5: a provider that answers too slowly for the probe but within
    the run timeout is red-checking the system as broken. A slow verdict
    warns — naming the mismatch — instead of failing with the
    mistyped-URL message."""
    _write_setup(app_dir)
    detail = ("The provider answered the test request, but too slowly for "
              "this check (over 5 s, then over 30 s on one retry). Real "
              "runs allow up to 120 s per request, so the endpoint may "
              "still work — check the model name in provider settings if "
              "runs are failing.")
    monkeypatch.setattr(llm, "probe_provider", lambda: ("slow", detail))
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["provider"].state == sc.WARN
    assert "may still work" in checks["provider"].meaning
    assert "may be down" not in checks["provider"].meaning
    assert checks["provider"].fix == sc.Fix("Open provider settings", "/settings")


# ---------------------------------------------------------------------------
# Fix affordances route to the page that can act
# ---------------------------------------------------------------------------


def test_fix_buttons_route_to_the_right_pages(app_dir, capable_machine, monkeypatch):
    _write_setup(app_dir, profile=False, resume="pdf", searches=False)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda: _engine("claude", engine_auth.SIGNED_OUT)
    )
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["profile"].fix == sc.Fix("Start your profile", "/onboarding")
    assert checks["resume"].fix == sc.Fix("Add your resume", "/onboarding")
    assert checks["search"].fix == sc.Fix("Finish setup", "/onboarding")
    assert checks["provider"].fix == sc.Fix("Open provider settings", "/settings")
    assert checks["claude"].fix == sc.Fix("Check agent sign-in", "/engine")
    assert checks["capsolver"].fix == sc.Fix("Add a key in Settings", "/settings")


def test_engine_missing_offers_provider_instead(app_dir, capable_machine, monkeypatch):
    """The not-installed engine's fix is the provider path, per the engine gate."""
    _write_setup(app_dir)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda: _engine("claude", engine_auth.NOT_INSTALLED)
    )
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["claude"].fix == sc.Fix("Use my AI provider instead", "/settings")


def test_machine_only_fixes_have_no_button(
    app_dir, capable_machine, signed_in_everywhere, monkeypatch
):
    """Chrome, Node, and the board reader can only be fixed on the machine —
    plain language, no stub button."""
    _write_setup(app_dir)
    monkeypatch.setattr(sc.importlib.util, "find_spec", lambda name: None)
    checks = {c.id: c for c in sc.run_checks()}
    assert checks["jobspy"].fix is None
    assert checks["chrome"].fix is None
    assert checks["node"].fix is None


# ---------------------------------------------------------------------------
# Rendering: every check in every state, badge labels, page shell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [sc.PASS, sc.WARN, sc.FAIL])
def test_every_check_renders_in_every_state(state):
    """The template renders each check id under each of the three states."""
    groups = [
        (heading, [sc.Check(cid, f"Label {cid}", state, f"Meaning for {cid}.") for cid in ids])
        for heading, ids in sc._GROUPS
    ]
    from kestrel.portal.app import templates

    html = templates.env.get_template("partials/system_checks.html").render(
        groups=groups, state_labels=sc.STATE_LABELS
    )
    assert sc.STATE_LABELS[state] in html
    for cid in CHECK_IDS:
        assert f"check-{cid}" in html
        assert f"Label {cid}" in html
        assert f"Meaning for {cid}." in html


def test_fix_buttons_render_as_links(
    portal_client, app_dir, capable_machine, monkeypatch
):
    _write_setup(app_dir, profile=False, searches=False)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda: _engine("claude", engine_auth.SIGNED_OUT)
    )
    client, _app = portal_client
    r = client.get("/system-check")
    assert r.status_code == 200
    assert 'href="/onboarding"' in r.text
    assert 'href="/settings"' in r.text
    assert 'href="/engine"' in r.text


# ---------------------------------------------------------------------------
# Re-run stays on the page; both themes render; nav + cross-links
# ---------------------------------------------------------------------------


def test_rerun_is_in_place(portal_client, app_dir, capable_machine, signed_in_everywhere):
    _write_setup(app_dir)
    client, _app = portal_client
    page = client.get("/system-check")
    assert page.status_code == 200
    assert 'hx-get="/system-check/checks"' in page.text
    assert 'hx-disabled-elt="this"' in page.text
    assert "Checking…" in page.text
    assert 'hx-target="#system-check-list"' in page.text

    partial = client.get("/system-check/checks")
    assert partial.status_code == 200
    assert "<aside" not in partial.text  # the partial, not a full page
    assert "Check again" not in partial.text  # the button stays on the page


def test_page_renders_in_both_themes(
    portal_client, app_dir, capable_machine, signed_in_everywhere
):
    _write_setup(app_dir)
    client, _app = portal_client
    for theme in ("light", "dark"):
        r = client.get(f"/system-check?theme={theme}")
        assert r.status_code == 200
        assert f'data-theme="{theme}"' in r.text


def test_sidebar_entry_and_cross_links(
    portal_client, app_dir, capable_machine, signed_in_everywhere
):
    _write_setup(app_dir)
    client, _app = portal_client
    page = client.get("/system-check").text
    assert 'href="/system-check"' in page  # sidebar nav
    assert "System check" in client.get("/onboarding").text
    assert "System check" in client.get("/settings").text
