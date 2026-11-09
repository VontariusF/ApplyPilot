"""Automation UI E2E: every automation action is reachable by button.

The spec's browser layer for the Automation half (art_oJ8ssNjF): a real
Chromium presses the same buttons the user does. Preset children are
harmless (`python -c "pass"` via `harmless_runs`) and the LLM is the
shared fake, so no journey touches the network or a real job board.
Toggle persistence across a portal restart is proven at the route level
(test_portal_automation.py); here the browser proves the buttons work.
"""

import json
import time

import pytest

pytestmark = pytest.mark.e2e

PRESET_TITLES = ("Find jobs", "Apply to matches", "Refresh scores")


def _login(page, base, token):
    async def journey_login():
        await page.goto(f"{base}/")
        await page.wait_for_selector("input[name='token']")
        await page.fill("input[name='token']", token)
        await page.click("button:has-text('Open portal')")
        await page.wait_for_selector(".stat-card")

    return journey_login()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_automation_action_is_button_reachable(
        run_journey, chromium, portal_server, harmless_runs, theme):
    """Cards, toggles, Adjust disclosures, start and stop — all by button."""

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _login(page, base, portal_server.token)

            # 1. The sidebar reaches the Automation page; pinned theme applied
            #    via the ?theme= param the way the smoke journey does.
            assert await page.locator(
                "a[href='/automation']").count() == 1
            await page.goto(f"{base}/automation?theme={theme}")
            await page.wait_for_selector(".preset-grid")
            assert await page.locator("html").get_attribute("data-theme") == theme

            # 2. Three preset cards, each with a plain-language description
            #    and a Start button.
            for title in PRESET_TITLES:
                card = page.locator(f".preset-card:has(h2:text('{title}'))")
                assert await card.count() == 1
                assert await card.locator("[data-action='start']").count() == 1
                description = await card.locator("p").first.inner_text()
                assert description.strip()  # not an empty stub

            # 3. Both toggles render with a clear state badge and a button.
            for toggle in ("auto_apply_enabled", "schedule_enabled"):
                badge = page.locator(f"[data-toggle-state='{toggle}']")
                assert (await badge.inner_text()).strip() in {"On", "Off"}
                assert await page.locator(
                    f".toggle-card:has([data-toggle-state='{toggle}']) button"
                ).count() == 1

            # 4. Adjust disclosures start collapsed and offer selects/buttons.
            for title in ("Find jobs", "Apply to matches"):
                card = page.locator(f".preset-card:has(h2:text('{title}'))")
                details = card.locator("details.adjust")
                assert await details.get_attribute("open") is None
                await details.locator("summary").click()
                assert await details.locator("select, input[type='radio'], "
                                             "input[type='checkbox']").count() > 0

            # 5. Start Find jobs by button: the card flips to its running
            #    state (Working now + Stop appears, Start disabled).
            card = page.locator(".preset-card[data-preset='find_jobs']")
            await card.locator("[data-action='start']").click()
            await page.wait_for_selector(
                ".preset-card[data-preset='find_jobs'] [data-action='stop']")
            assert "Working now" in await card.inner_text()
            assert await card.locator(
                "[data-action='start']").is_disabled()

            # 6. Stop by button: back to idle.
            await card.locator("[data-action='stop']").click()
            await page.wait_for_selector(
                ".preset-card[data-preset='find_jobs'] [data-action='stop']",
                state="detached")
            assert await card.locator(
                "[data-action='start']").is_enabled()

            # 7. Flip auto-apply on by button; the badge says On.
            auto = page.locator(
                ".toggle-card:has([data-toggle-state='auto_apply_enabled'])")
            await auto.locator("button").click()
            await page.wait_for_selector(
                "[data-toggle-state='auto_apply_enabled']:has-text('On')")
            assert (await page.locator(
                "[data-toggle-state='auto_apply_enabled']").inner_text()).strip() == "On"

    run_journey(journey())


def test_agent_panel_actions_via_fake_llm_and_plain_run_rows(
        run_journey, chromium, portal_server, harmless_runs, fake_llm,
        monkeypatch):
    """The panel drives the same actions; run history reads in plain language."""
    from kestrel import onboarding
    from kestrel.portal import runner

    # The panel's provider gate detects env providers; the fake sits at the
    # client seam, so open the gate the same way the route tests do.
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _login(page, base, portal_server.token)
            await page.goto(f"{base}/automation")

            # 1. Ask the agent to start Refresh scores (fake_llm scripts the
            #    intent; the panel runs the same start_preset the button calls).
            fake_llm.respond('{"action": "start", "preset": "refresh_scores"}')
            await page.fill("#agent-panel input[name='text']", "refresh my scores")
            await page.click("#agent-panel button:has-text('Ask')")
            await page.wait_for_selector(".chat-msg.assistant:has-text('Started refresh scores')")

            # 2. The run it started shows on run history in plain language —
            #    no model ids, workers, or stage names.
            await page.goto(f"{base}/runs")
            await page.wait_for_selector(".runs-table, #runs-table, table")
            body = await page.inner_text("body")
            assert "Refreshing scores" in body
            for jargon in ("gpt-", "claude-", "worker", "headless",
                           "stage_", "rescore"):
                assert jargon not in body

            # 3. Wait for the harmless child to finish, then the row is done.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                runs = runner.list_runs()
                if runs and runs[0]["status"] in ("completed", "failed",
                                                  "cancelled"):
                    break
                await page.wait_for_timeout(250)
            assert runs[0]["status"] == "completed"

    run_journey(journey())


def _seed_run(run_id: str, kind: str, params: dict | None,
              status: str, **extra) -> None:
    """Insert a registry row directly (same helper shape as the route tests)."""
    from kestrel.database import get_connection
    from kestrel.portal import runner

    runner.init_registry()
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO runs (id, kind, params, status, started_at, "
        "ended_at, exit_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, kind, json.dumps(params or {}), status, now,
         extra.get("ended_at"), extra.get("exit_code")),
    )
    conn.commit()


def test_failed_run_shows_retry_button_in_browser(
        run_journey, chromium, portal_server, harmless_runs):
    """A failed run row carries a Retry button; pressing it starts a new run."""
    _seed_run("e2e-failed-run", "rescore", {}, "failed", exit_code=1)

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _login(page, base, portal_server.token)
            await page.goto(f"{base}/runs")
            row = page.locator("tr:has-text('Refreshing scores')").first
            assert await row.count() == 1

            retry = row.locator("button:has-text('Retry')")
            assert await retry.count() == 1
            await retry.click()
            await page.wait_for_selector("tr:has-text('Refreshing scores')")
            assert await page.locator(
                "tr:has-text('Refreshing scores')").count() >= 2

    run_journey(journey())


def test_progress_strip_on_the_jobs_board(run_journey, chromium,
                                          portal_server, harmless_runs):
    """A running find-jobs pipeline shows 'Working: Finding jobs — N found'."""
    from datetime import datetime, timezone

    from kestrel.database import init_db

    _seed_run("e2e-pipeline", "pipeline", {"results_level": "standard"},
              "running")
    conn = init_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, site, location, description, "
        "full_description, fit_score, score_reasoning, discovered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("https://jobs.example/e2e-1", "Pwidget Engineer", "ExampleBoard",
         "Remote", "short desc", "full description text", 9,
         "Strong match on pwidget skills.", now),
    )
    conn.commit()

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs")
            strip = page.locator("[data-testid='jobs-progress']")
            await strip.wait_for()
            text = await strip.inner_text()
            assert "Working: Finding jobs" in text
            assert "1 found so far" in text

    run_journey(journey())
