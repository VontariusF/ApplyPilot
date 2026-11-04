"""Smoke E2E: token login -> dashboard -> jobs board, in both themes.

The harness proof for the spec's browser-E2E layer: a real Chromium drives
the portal the way the user does — no token header, no TestClient.
"""

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_login_dashboard_and_jobs(run_journey, chromium, portal_server, seeded_job, theme):
    """Login with the token, then render dashboard and jobs in `theme`."""

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            # 1. An unauthenticated visit gets the login form; the token unlocks it.
            await page.goto(f"{base}/")
            await page.wait_for_selector("input[name='token']")
            await page.fill("input[name='token']", portal_server.token)
            await page.click("button:has-text('Open portal')")
            await page.wait_for_selector(".stat-card")  # landed on the dashboard

            # 2. Dashboard renders, with the pinned theme on <html data-theme=...>.
            await page.goto(f"{base}/?theme={theme}")
            assert await page.locator("html").get_attribute("data-theme") == theme
            assert await page.locator("h1").inner_text() == "Dashboard"

            # 3. Jobs board renders with the seeded job card.
            await page.goto(f"{base}/jobs?theme={theme}")
            assert await page.locator("h1").inner_text() == "Jobs"
            assert await page.locator("#jobs-board").get_by_text("Pwidget Engineer").count() == 1

    run_journey(journey())
