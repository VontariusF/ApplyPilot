"""System Check E2E: sidebar navigation, states, and in-place re-run.

A real Chromium drives the page the way the user does: open from the
sidebar, see the dependency list, hit Check again and watch the list swap
in place — no page navigation.
"""

import pytest

pytestmark = pytest.mark.e2e


def test_system_check_renders_and_reruns_in_place(run_journey, chromium, portal_server):
    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await page.goto(f"{base}/")
            await page.wait_for_selector("input[name='token']")
            await page.fill("input[name='token']", portal_server.token)
            await page.click("button:has-text('Open portal')")
            await page.wait_for_selector(".stat-card")

            # 1. The sidebar entry opens the System Check page.
            await page.click("nav.side-nav a:has-text('System check')")
            await page.wait_for_selector("#system-check-list")
            assert await page.locator("h1").inner_text() == "System check"
            assert await page.locator(".syscheck").count() == 11
            assert await page.locator(".badge.syscheck-pass, .badge.syscheck-warn, "
                                      ".badge.syscheck-fail").count() == 11

            # 2. Fix affordances are portal links, never shell copy.
            first_fix = page.locator("#system-check-list a.button-link").first
            assert (await first_fix.get_attribute("href")).startswith("/")

            # 3. Check again swaps the list in place — the URL does not change.
            await page.click("button:has-text('Check again')")
            await page.wait_for_selector("#check-busy.htmx-indicator", state="attached")
            await page.wait_for_function(
                "document.querySelector('#check-busy').offsetParent === null"
            )  # indicator hidden again = swap finished
            assert await page.locator(".syscheck").count() == 11
            assert "system-check" in page.url

    run_journey(journey())
