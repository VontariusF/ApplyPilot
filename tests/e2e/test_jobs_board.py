"""Jobs-board E2E journeys (spec Jobs panel rework): real Chromium driving
the portal through the new card verbs — Tailor now, Apply now (confirm
modal), Dismiss/Undo — plus chips, pagination, and both themes.

The subprocesses behind Tailor now / Apply now fail fast in this sandbox
(no provider keys — the fake_llm contract keeps every surface off the
network), so the journeys verify the UI contract: the immediate busy card
and toast, the confirmation modal contents, and dismiss/undo round-trips.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.e2e


async def login(page, base, token):
    await page.goto(f"{base}/")
    await page.wait_for_selector("input[name='token']")
    await page.fill("input[name='token']", token)
    await page.click("button:has-text('Open portal')")
    await page.wait_for_selector(".stat-card")


def seed_jobs(count=1, **overrides):
    """Seed scored jobs in the isolated store; returns their urls."""
    from applypilot.database import init_db

    conn = init_db()
    now = datetime.now(timezone.utc)
    urls = []
    for i in range(count):
        url = f"https://jobs.example/e2e-{i}"
        row = {
            "url": url, "title": f"E2E Job {i}", "site": "ExampleBoard",
            "location": "Remote", "description": "short desc",
            "full_description": "full description text", "fit_score": 8,
            "discovered_at": (now - timedelta(minutes=i)).isoformat(),
        }
        row.update(overrides)
        conn.execute(
            "INSERT OR REPLACE INTO jobs (url, title, site, location, "
            "description, full_description, fit_score, discovered_at, "
            "score_error, tailored_resume_path, apply_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["url"], row["title"], row["site"], row["location"],
             row["description"], row["full_description"], row["fit_score"],
             row["discovered_at"], overrides.get("score_error"),
             overrides.get("tailored_resume_path"),
             overrides.get("apply_status")))
        urls.append(url)
    conn.commit()
    return urls


def test_tailor_now_shows_busy_card_and_toast(
        run_journey, chromium, portal_server, seeded_profile):
    """Clicking Tailor now swaps the card to a busy state with a toast."""

    async def journey():
        base = portal_server.base_url
        (url,) = seed_jobs(1)
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs")
            card = page.locator(".job-card", has_text="E2E Job 0")
            await card.get_by_role("button", name="Tailor now").click()
            # The verb responds immediately: the card shows its busy state
            # and the toast confirms the run started.
            await page.wait_for_selector("#jobs-board .card-busy")
            assert "Tailoring documents…" in await page.locator(
                "#jobs-board .card-busy").inner_text()
            assert "Tailoring started" in await page.locator(
                "#jobs-board .toast").inner_text()

    run_journey(journey())


def test_apply_now_modal_lists_documents_then_confirms(
        run_journey, chromium, portal_server, seeded_profile):
    """Apply now opens a confirmation card naming the documents, and
    confirming starts the one-job apply run (busy card)."""

    async def journey():
        base = portal_server.base_url
        seed_jobs(1, tailored_resume_path="/tmp/resume.txt")
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs")
            await page.click("label[for='apply-confirm-main-1']")
            modal = page.locator(".modal-card")
            await modal.wait_for()
            assert "Your tailored resume" in await modal.inner_text()
            await modal.get_by_role("button", name="Confirm & apply").click()
            await page.wait_for_selector("#jobs-board .card-busy")
            assert "Submitting your application…" in await page.locator(
                "#jobs-board .card-busy").inner_text()

    run_journey(journey())


def test_apply_now_without_documents_offers_tailor_now(
        run_journey, chromium, portal_server, seeded_profile):
    """No tailored documents: the card offers Tailor now — never Apply."""

    async def journey():
        base = portal_server.base_url
        seed_jobs(1)
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs")
            assert await page.locator(".job-card").get_by_text(
                "Apply now").count() == 0
            assert await page.locator(".job-card").get_by_role(
                "button", name="Tailor now").count() == 1

    run_journey(journey())


def test_dismiss_then_undo_roundtrip(
        run_journey, chromium, portal_server, seeded_profile):
    """Dismiss removes the card into the Dismissed section; Undo restores it."""

    async def journey():
        base = portal_server.base_url
        seed_jobs(1)
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs")
            board = page.locator("#jobs-board")
            await page.get_by_role("button", name="Dismiss").click()
            await board.locator(".dismissed-section .job-card").wait_for()
            # Undo restores the card to the MAIN grid — scope by direct child:
            # the dismissed section nests its own .card-grid.
            await board.locator(".dismissed-section summary").click()
            await page.get_by_role("button", name="Undo").click()
            await page.wait_for_selector("#jobs-board > .card-grid .job-card")
            assert await board.locator(".dismissed-section").count() == 0

    run_journey(journey())


def test_needs_attention_chip_filters_the_board(
        run_journey, chromium, portal_server, seeded_profile):
    """The Needs-attention chip shows failed jobs and renders as an
    active, dismissible chip."""

    async def journey():
        base = portal_server.base_url
        seed_jobs(2, fit_score=8)  # e2e-0 healthy, e2e-1 made broken below
        from applypilot.database import init_db

        conn = init_db()
        conn.execute("UPDATE jobs SET title='Broken Job', fit_score=NULL, "
                     "score_error='boom' WHERE url=?",
                     ("https://jobs.example/e2e-1",))
        conn.commit()
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs")
            board = page.locator("#jobs-board")
            # Default view: the healthy job, not the broken one.
            assert await board.get_by_text("E2E Job 0").count() == 1
            assert await board.get_by_text("Broken Job").count() == 0
            await page.click("a.chip:has-text('Needs attention')")
            await board.get_by_text("Broken Job").wait_for()
            assert await board.get_by_text("E2E Job 0").count() == 0
            # The active filter renders as a removable chip.
            active = page.locator("a.chip-active", has_text="Needs attention")
            assert await active.count() == 1

    run_journey(journey())


def test_pagination_next_and_count(
        run_journey, chromium, portal_server, seeded_profile):
    """Server-side pagination: count in this view, Next advances pages."""

    async def journey():
        base = portal_server.base_url
        seed_jobs(12)
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs?min_score=0&limit=5")
            board = page.locator("#jobs-board")
            assert "12 jobs in this view" in await board.locator(
                ".result-count").inner_text()
            assert "Page 1 of 3" in await board.locator(
                ".page-status").inner_text()
            await board.get_by_role("link", name="Next →").click()
            await board.get_by_text("Page 2 of 3").wait_for()

    run_journey(journey())


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_jobs_board_cards_render_in_both_themes(
        run_journey, chromium, portal_server, seeded_profile, theme):
    """The card grid, score pill and chips read correctly in both themes."""

    async def journey():
        base = portal_server.base_url
        seed_jobs(1)
        async with chromium() as page:
            await login(page, base, portal_server.token)
            await page.goto(f"{base}/jobs?theme={theme}")
            assert await page.locator("html").get_attribute("data-theme") == theme
            await page.wait_for_selector(".job-card")
            assert await page.locator(".score-pill.hot").inner_text() == "8/10"
            for chip in ("New", "Scored", "Ready to apply", "Applied",
                         "Needs attention"):
                assert await page.locator("a.chip", has_text=chip).count() >= 1

    run_journey(journey())
