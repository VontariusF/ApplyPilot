"""Automation page UI tests (spec Automation UI): preset cards, toggles,
adjust disclosures, the cap-pause surface, and the cards SSE stream.

Route-level companion to test_portal_automation.py (backend): here the
page and its forms are the subject — every automation action must be
button-reachable, values the UI offers must persist, and values it never
offered must be refused.
"""

import asyncio
import json
import re
import sys
import time
from html import unescape

import pytest

from kestrel.database import get_connection, init_db
from kestrel.portal import automation, runner


def seed_applied(n: int) -> None:
    """n applications recorded right now (today's UTC calendar day)."""
    conn = init_db()
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    for i in range(n):
        conn.execute(
            "INSERT OR REPLACE INTO jobs (url, title, site, description, "
            "apply_status, applied_at, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"https://jobs.example/applied-{i}", "Pwidget Engineer", "ExampleBoard",
             "short desc", "applied", now, now),
        )
    conn.commit()


def seed_run(run_id: str, kind: str, params: dict | None = None,
             status: str = "running", pid: int | None = None) -> dict:
    """Insert a registry row directly (no subprocess)."""
    runner.init_registry()
    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (id, kind, params, status, pid, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, kind, json.dumps(params or {}), status, pid,
         time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
    )
    conn.commit()
    return runner.get_run(run_id)


@pytest.fixture()
def real_spawns(monkeypatch):
    """Let presets spawn real subprocesses that exit immediately.

    The test-verified pattern from test_portal_pages.py: route -> real
    start_run, with the child swapped for `python -c "pass"` so no stage
    work ever runs.
    """
    original = runner.start_run

    def fake_start(kind, params, command=None, emit_jsonl=None):
        return original(kind, params, command=[sys.executable, "-c", "pass"],
                        emit_jsonl=False)

    monkeypatch.setattr(runner, "start_run", fake_start)


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

def test_automation_page_renders_preset_cards_and_toggles(portal_client):
    client, _ = portal_client
    r = client.get("/automation")
    assert r.status_code == 200
    assert r.text.count("<h1>Automation</h1>") == 1
    for title in ("Find jobs", "Apply to matches", "Refresh scores"):
        assert title in r.text
    # Both master toggles: clear off state, button-reachable.
    assert r.text.count('class="badge off"') == 2
    assert r.text.count("Turn on") == 2
    assert 'action="/automation/start"' in r.text
    assert 'action="/automation/toggle"' in r.text


def test_automation_sidebar_entry_and_active_state(portal_client):
    client, _ = portal_client
    r = client.get("/automation")
    assert 'href="/automation"' in r.text
    assert 'href="/automation" ' in r.text and "aria-current" in r.text


def test_refresh_scores_has_no_adjust_disclosure(portal_client):
    client, _ = portal_client
    r = client.get("/automation")
    # Two Adjust disclosures (find_jobs, apply_matches) — refresh_scores has none.
    assert r.text.count('<details class="adjust">') == 2


def test_adjust_disclosures_offer_buttons_and_selects_only(portal_client):
    client, _ = portal_client
    r = client.get("/automation")
    # The Adjust disclosures render selects and radio/checkbox buttons —
    # never free-text inputs. (The agent panel's composer is chat, not
    # an adjustment, so it may be a text input.)
    assert '<select name="results_level">' in r.text
    for score in ("5", "6", "7", "8", "9"):
        assert f'name="min_score" value="{score}"' in r.text
    for cap in ("5", "10", "25"):
        assert f'name="daily_cap" value="{cap}"' in r.text
    # No cap is the first, default-checked choice (spec art_NcNShMYR).
    assert 'name="daily_cap" value=""' in r.text
    assert "> No cap<" in r.text
    assert 'name="daily_cap" value="" checked' in r.text
    assert 'name="keep_going"' in r.text
    for block in re.findall(r'<details class="adjust">.*?</details>', r.text,
                            re.DOTALL):
        assert '<input type="text"' not in block


def test_both_themes_render_automation_page(portal_client):
    client, _ = portal_client
    for theme in ("light", "dark"):
        r = client.get(f"/automation?theme={theme}")
        assert r.status_code == 200
        assert f'data-theme="{theme}"' in r.text


# ---------------------------------------------------------------------------
# Start / stop buttons
# ---------------------------------------------------------------------------

def test_start_button_runs_the_preset(portal_client, real_spawns):
    client, _ = portal_client
    r = client.post("/automation/start", data={"preset": "find_jobs"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/automation"
    runs = runner.list_runs()
    assert len(runs) == 1
    assert runs[0]["kind"] == "pipeline"
    params = json.loads(runs[0]["params"])
    assert params["results_per_site"] == "50"  # the "standard" preset default


def test_start_button_refuses_when_daily_cap_is_spent(portal_client, real_spawns):
    automation.update({"presets": {"apply_matches": {"daily_cap": 10}}})
    seed_applied(10)  # fills the requested cap of 10
    client, _ = portal_client
    r = client.post("/automation/start", data={"preset": "apply_matches"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert runner.list_runs() == []


def test_stop_button_cancels_the_preset_run(portal_client, real_spawns):
    client, _ = portal_client
    client.post("/automation/start", data={"preset": "find_jobs"},
                follow_redirects=False)
    run = runner.list_runs()[0]
    r = client.post("/automation/stop", data={"preset": "find_jobs"},
                    follow_redirects=False)
    assert r.status_code == 303
    deadline = time.time() + 5
    while time.time() < deadline and runner.get_run(run["id"])["status"] == "running":
        time.sleep(0.05)
    assert runner.get_run(run["id"])["status"] == "cancelled"


def test_stop_button_without_active_run_redirects_cleanly(portal_client):
    client, _ = portal_client
    r = client.post("/automation/stop", data={"preset": "find_jobs"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/automation"


def test_stop_rejects_unknown_preset(portal_client):
    client, _ = portal_client
    r = client.post("/automation/stop", data={"preset": "nope"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ---------------------------------------------------------------------------
# Toggles: persist, render, survive a portal restart
# ---------------------------------------------------------------------------

def test_toggle_button_flips_and_persists(portal_client):
    client, _ = portal_client
    r = client.post("/automation/toggle",
                    data={"name": "auto_apply_enabled", "value": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert automation.load()["auto_apply_enabled"] is True
    r = client.get("/automation")
    assert 'data-toggle-state="auto_apply_enabled"' in r.text
    assert ">On<" in r.text


def test_toggles_survive_portal_restart(portal_env, portal_client):
    """A restart re-runs create_app against the same store; state must hold."""
    client, _ = portal_client
    client.post("/automation/toggle",
                data={"name": "schedule_enabled", "value": "on"},
                follow_redirects=False)
    # The "restart": a brand-new app instance + client over the same app dir.
    from fastapi.testclient import TestClient

    from kestrel.portal.app import create_app
    app = create_app()
    fresh = TestClient(app, base_url="http://127.0.0.1")
    fresh.headers["x-portal-token"] = app.state.token
    assert automation.load()["schedule_enabled"] is True
    r = fresh.get("/automation")
    assert 'data-toggle-state="schedule_enabled"' in r.text
    # The schedule card's badge is the On one (auto-apply is still Off).
    assert 'data-toggle-state="schedule_enabled">On<' in r.text


def test_toggle_rejects_unknown_name(portal_client):
    client, _ = portal_client
    r = client.post("/automation/toggle", data={"name": "evil", "value": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert automation.load()["auto_apply_enabled"] is False
    assert automation.load()["schedule_enabled"] is False


# ---------------------------------------------------------------------------
# Adjust disclosures
# ---------------------------------------------------------------------------

def test_adjust_saves_apply_preset_knobs(portal_client):
    client, _ = portal_client
    r = client.post("/automation/adjust", data={
        "preset": "apply_matches", "min_score": "5",
        "daily_cap": "25", "keep_going": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    cfg = automation.load()
    assert cfg["presets"]["apply_matches"] == {
        "min_score": 5, "daily_cap": 25, "keep_going": True}


def test_adjust_saves_find_jobs_results_level(portal_client):
    client, _ = portal_client
    r = client.post("/automation/adjust",
                    data={"preset": "find_jobs", "results_level": "high"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert automation.preset_params(automation.load(), "find_jobs")[
        "results_per_site"] == "100"


def test_adjust_keeps_going_unchecked_means_off(portal_client):
    client, _ = portal_client
    client.post("/automation/adjust", data={
        "preset": "apply_matches", "min_score": "7", "daily_cap": "10",
    }, follow_redirects=False)
    assert automation.load()["presets"]["apply_matches"]["keep_going"] is False


def test_adjust_rejects_values_the_ui_never_offers(portal_client):
    client, _ = portal_client
    for form in ({"preset": "apply_matches", "min_score": "3", "daily_cap": "10"},
                 {"preset": "apply_matches", "min_score": "7", "daily_cap": "50"},
                 {"preset": "find_jobs", "results_level": "max"},
                 {"preset": "bogus"}):
        r = client.post("/automation/adjust", data=form, follow_redirects=False)
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
    # Nothing was written by any of the rejected forms.
    assert automation.load()["presets"] == automation.DEFAULTS["presets"]


def test_adjust_error_surfaces_on_the_page(portal_client):
    client, _ = portal_client
    r = client.post("/automation/adjust",
                    data={"preset": "apply_matches", "min_score": "3",
                          "daily_cap": "10"},
                    follow_redirects=False)
    assert "min_score" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "min_score" in page.text  # the alert shows the reason
    assert 'class="alert error"' in page.text


# ---------------------------------------------------------------------------
# Daily-cap pause surface
# ---------------------------------------------------------------------------

def test_cap_pause_reason_shows_on_the_card(portal_client):
    """When the cap pauses auto-apply, the persisted reason is visible."""
    client, _ = portal_client
    client.post("/automation/toggle",
                data={"name": "auto_apply_enabled", "value": "on"},
                follow_redirects=False)
    automation.update({"presets": {"apply_matches": {"daily_cap": 10}}})
    seed_applied(10)  # fills the requested cap of 10
    outcome = automation.run_tick()
    assert outcome["paused"]  # the scheduler recorded the pause + reason
    r = client.get("/automation")
    assert "Daily cap reached" in r.text
    assert "data-testid=\"cap-pause\"" in r.text


def test_cap_usage_line_shows_used_of_cap(portal_client):
    seed_applied(4)
    client, _ = portal_client
    # No cap requested: the line says so plainly (spec art_NcNShMYR).
    r = client.get("/automation")
    assert "No daily cap — 4 applications used today." in r.text

    automation.update({"presets": {"apply_matches": {"daily_cap": 10}}})
    r = client.get("/automation")
    assert "4 of 10 applications used today." in r.text


# ---------------------------------------------------------------------------
# Cards SSE stream
# ---------------------------------------------------------------------------

def test_cards_stream_pushes_on_change(portal_client):
    """A change since connect is pushed as the cards partial (push-on-change)."""
    from kestrel.portal import app as portal_app

    last = portal_app._automation_signature()
    client, _ = portal_client
    client.post("/automation/toggle",
                data={"name": "schedule_enabled", "value": "on"},
                follow_redirects=False)

    async def first_event():
        gen = portal_app._stream_cards(last)
        try:
            return await gen.__anext__()
        finally:
            await gen.aclose()

    event = asyncio.run(first_event())
    assert event["event"] == "cards"
    assert 'data-toggle-state="schedule_enabled">On<' in event["data"]


# ---------------------------------------------------------------------------
# Run rows: plain language everywhere
# ---------------------------------------------------------------------------

def test_run_rows_use_plain_language_only(portal_client):
    """No kind names, model ids, workers, or headless in run rows."""
    seed_run("r1", "pipeline", {"min_score": "7", "workers": "2"}, status="failed")
    seed_run("r2", "apply",
             {"model": "haiku", "workers": "2", "headless": "1",
              "min_score": "7"}, status="failed")
    seed_run("r3", "rescore", {}, status="completed")
    client, _ = portal_client
    r = client.get("/runs")
    assert "Finding jobs" in r.text
    assert "Applying" in r.text
    assert "Refreshing scores" in r.text
    # No jargon anywhere on the page.
    for jargon in ("pipeline", "rescore", "haiku", "workers", "headless",
                   "min_score", "continuous"):
        assert jargon not in r.text


def test_run_summary_qualifies_one_job_and_keep_going(portal_client):
    seed_run("r4", "apply", {"url": "https://jobs.example/posting-1"},
             status="failed")
    seed_run("r5", "apply", {"continuous": "1"}, status="failed")
    client, _ = portal_client
    r = client.get("/runs")
    assert "Applying — one job" in r.text
    assert "Applying — keeps going" in r.text


def test_retry_button_only_on_failed_runs(portal_client):
    seed_run("r6", "pipeline", {}, status="failed")
    seed_run("r7", "apply", {}, status="running", pid=1)
    seed_run("r8", "rescore", {}, status="completed")
    client, _ = portal_client
    r = client.get("/runs")
    assert r.text.count('action="/runs/retry/r6"') == 1
    assert 'action="/runs/retry/r7"' not in r.text
    assert 'action="/runs/retry/r8"' not in r.text
    assert 'action="/runs/cancel/r7"' in r.text  # cancel stays as-is


def test_retry_button_replays_failed_run(portal_client, real_spawns):
    seed_run("r9", "apply", {"min_score": "7", "model": "haiku"},
             status="failed")
    client, _ = portal_client
    r = client.post("/runs/retry/r9", follow_redirects=False)
    assert r.status_code == 303
    retried = [x for x in runner.list_runs() if x["id"] != "r9"][0]
    assert retried["kind"] == "apply"
    assert json.loads(retried["params"]) == {"min_score": "7", "model": "haiku"}


def test_retry_error_surfaces_on_runs_page(portal_client):
    seed_run("r10", "rescore", {}, status="completed")
    client, _ = portal_client
    r = client.post("/runs/retry/r10", follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "only failed runs can be retried" in page.text


# ---------------------------------------------------------------------------
# Jobs-board progress strip
# ---------------------------------------------------------------------------

def seed_job_discovered(url: str, when: str) -> None:
    conn = init_db()
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, site, description, "
        "discovered_at) VALUES (?, ?, ?, ?, ?)",
        (url, "Pwidget Engineer", "ExampleBoard", "short desc", when))
    conn.commit()


def test_progress_strip_shows_working_label_and_count(portal_client):
    """Active pipeline run -> 'Working: Finding jobs — N found so far'."""
    seed_run("r11", "pipeline", {}, status="running", pid=1)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    seed_job_discovered("https://jobs.example/a", now)
    seed_job_discovered("https://jobs.example/b", now)
    client, _ = portal_client
    r = client.get("/jobs")
    assert 'data-testid="jobs-progress"' in r.text
    assert "Working: Finding jobs — 2 found so far" in r.text


def test_progress_strip_counts_rescored_jobs(portal_client):
    seed_run("r12", "rescore", {}, status="running", pid=1)
    conn = init_db()
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, site, description, "
        "scored_at) VALUES (?, ?, ?, ?, ?)",
        ("https://jobs.example/c", "Pwidget Engineer", "ExampleBoard",
         "short desc", now))
    conn.commit()
    client, _ = portal_client
    r = client.get("/jobs")
    assert "Working: Refreshing scores — 1 refreshed so far" in r.text


def test_progress_strip_hidden_when_idle(portal_client):
    seed_job_discovered("https://jobs.example/d",
                        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    client, _ = portal_client
    r = client.get("/jobs")
    assert "Working:" not in r.text


def test_rescore_all_returns_to_the_jobs_board(portal_client, real_spawns):
    client, _ = portal_client
    r = client.post("/jobs/rescore-all", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs"
    runs = runner.list_runs()
    assert len(runs) == 1 and runs[0]["kind"] == "rescore"


def test_rescore_all_refused_while_run_active(portal_client):
    """F-2: rescore-all is a manual start — refused with an error redirect
    (not an unhandled 500) while another run is active."""
    client, _ = portal_client
    from urllib.parse import unquote

    seed_run("active-1", "apply", status="running", pid=1)
    r = client.post("/jobs/rescore-all", follow_redirects=False)
    assert r.status_code == 303
    assert "already active" in unquote(r.headers["location"])
    assert len(runner.list_runs()) == 1  # nothing new spawned


def test_find_jobs_empty_state_returns_to_the_board(portal_client, real_spawns):
    client, _ = portal_client
    r = client.post("/runs/start",
                    data={"kind": "pipeline", "next": "/jobs"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs"


def test_find_jobs_refused_while_run_active(portal_client):
    """F-2: the empty state's Find-jobs button is a manual start too —
    refused with an error redirect on the board while a run is active."""
    client, _ = portal_client
    from urllib.parse import unquote

    seed_run("active-1", "apply", status="running", pid=1)
    r = client.post("/runs/start",
                    data={"kind": "pipeline", "next": "/jobs"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "already active" in unquote(r.headers["location"])
    assert len(runner.list_runs()) == 1  # nothing new spawned


def test_runs_start_rejects_offsite_next_targets(portal_client, real_spawns):
    client, _ = portal_client
    for next_ in ("//evil.example", "https://evil.example", "relative"):
        r = client.post("/runs/start",
                        data={"kind": "pipeline", "next": next_},
                        follow_redirects=False)
        assert r.headers["location"] == "/runs" or \
            r.headers["location"].startswith("/runs?error=")


def test_progress_stream_pushes_on_change(portal_client):
    """Run start since connect is pushed as the strip partial."""
    from kestrel.portal import app as portal_app

    last = portal_app._progress_signature()
    assert last == ("idle", None)
    seed_run("r13", "apply", {}, status="running", pid=1)

    async def first_event():
        gen = portal_app._stream_progress(last)
        try:
            return await gen.__anext__()
        finally:
            await gen.aclose()

    event = asyncio.run(first_event())
    assert event["event"] == "progress"
    assert "Working: Applying" in event["data"]


# ---------------------------------------------------------------------------
# Ask-the-agent panel
# ---------------------------------------------------------------------------

@pytest.fixture()
def agent_panel(monkeypatch):
    """Clean in-memory transcript + provider gate opened for the fake LLM."""
    from kestrel.portal import assistant
    from kestrel import onboarding

    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    assistant.clear_transcript()
    yield
    assistant.clear_transcript()


def test_agent_panel_renders_on_the_automation_page(portal_client):
    client, _ = portal_client
    r = client.get("/automation")
    assert 'id="agent-panel"' in r.text
    assert 'data-testid="assistant-transcript"' in r.text
    assert 'action="/automation/ask"' in r.text


def test_agent_starts_a_preset_via_the_button_path(portal_client, real_spawns,
                                                   agent_panel, fake_llm):
    fake_llm.respond('{"action": "start", "preset": "find_jobs"}')
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "find me jobs"})
    assert r.status_code == 200
    runs = runner.list_runs()
    assert len(runs) == 1 and runs[0]["kind"] == "pipeline"
    assert "Started find jobs" in r.text


def test_agent_flips_toggle_and_reports_status(portal_client, real_spawns,
                                               agent_panel, fake_llm):
    fake_llm.respond('{"action": "toggle", "name": "auto_apply_enabled", "value": "on"}')
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "turn on auto-apply"})
    assert "Auto-apply is now on." in r.text
    assert automation.load()["auto_apply_enabled"] is True

    fake_llm.respond('{"action": "status"}')
    seed_applied(4)
    r = client.post("/automation/ask", data={"text": "where do things stand?"})
    assert "auto-apply is on, uncapped (no daily cap set); 4 used today" in r.text


def test_agent_stop_reports_when_nothing_ran(portal_client, agent_panel, fake_llm):
    fake_llm.respond('{"action": "stop", "preset": "refresh_scores"}')
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "stop refreshing"})
    # The page HTML-escapes text; compare on the unescaped answer.
    assert "Refresh scores wasn't running." in unescape(r.text)


def test_agent_refuses_an_unknown_preset(portal_client, agent_panel, fake_llm):
    fake_llm.respond('{"action": "start", "preset": "delete_everything"}')
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "do something else"})
    assert "unknown preset" in r.text
    assert 'class="alert error"' in r.text
    assert runner.list_runs() == []


def test_agent_falls_back_for_out_of_scope_asks(portal_client, agent_panel,
                                                fake_llm):
    fake_llm.respond('{"action": "unknown"}')
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "rewrite my resume"})
    assert "I can start or stop Find jobs" in r.text
    assert runner.list_runs() == []


def test_agent_without_provider_shows_the_gate(portal_client):
    """No fake_llm, no provider configured: a visible error, no crash."""
    from kestrel.portal import assistant

    assistant.clear_transcript()
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "hello"})
    assert "No LLM provider is configured" in r.text


def test_agent_transcript_persists_across_page_views(portal_client, agent_panel,
                                                     fake_llm):
    fake_llm.respond('{"action": "status"}')
    client, _ = portal_client
    client.post("/automation/ask", data={"text": "status please"})
    r = client.get("/automation")
    assert "status please" in r.text
    assert "auto-apply is off" in r.text


def test_agent_empty_ask_is_refused(portal_client, agent_panel, fake_llm):
    client, _ = portal_client
    r = client.post("/automation/ask", data={"text": "   "})
    assert "Ask the agent something first." in r.text
