"""Wizard E2E: the choice-first interview, agent toggle, and review form.

A real Chromium drives the portal the way the user does (the PR #29
harness). The journey answers every choice question with a button click
and types only into genuinely-free-text fields — the zero-chat promise —
then meets the needs-answer chips on review and confirms.

The walk is order-agnostic: the registry's depends_on rules decide which
question appears next, so the journey reads the live question card and
answers it from the plan below.
"""

import pytest

pytestmark = pytest.mark.e2e

# Per-question plan. Choices answer by the hidden value the button
# submits; "free:" entries are typed into the per-kind widget; None
# entries are skipped (their review chips are the point).
WALK = {
    "full_name": "free:Test Person",
    "education": "Bachelor's",
    "experience_years": "3–5",
    "work_style": "remote",
    "country": "United States",
    "work_authorized": None,  # skipped: gated field, gets the review chip
    "sponsorship": "no",
    "permit_type": "Citizen",
    "currency": "USD",
    "salary_band": "120000",
    "start_date": "Immediately",
    "target_roles": "free:Data Engineer\nAnalytics Engineer",
    "location": "free:Austin, TX",
    "skills_languages": "free:Python\nSQL",
    "skills_frameworks": "free:FastAPI",
    "skills_tools": "free:Docker",
    "email": "free:test@example.com",
    "phone": "free:+1 555 0100",
    "city": "free:Austin, TX",
    "linkedin_url": "free:https://linkedin.com/in/test",
    "github_url": None,
    "website_url": None,
    "portfolio_url": None,
}


async def _start_interview(page, base, theme, token):
    await page.goto(f"{base}/")
    await page.wait_for_selector("input[name='token']")
    await page.fill("input[name='token']", token)
    await page.click("button:has-text('Open portal')")
    await page.wait_for_selector(".stat-card")  # logged in
    await page.goto(f"{base}/onboarding?theme={theme}")
    assert await page.locator("html").get_attribute("data-theme") == theme
    await page.wait_for_selector("button:has-text('Use the configured provider instead')")
    await page.click("button:has-text('Use the configured provider instead')")
    await page.wait_for_selector("[data-question-id]")  # identity first


async def _answer_current(page, plan):
    """Answer whatever question card the page shows, per the plan."""
    qid = await page.locator("[data-question-id]").first.get_attribute("data-question-id")
    assert qid in plan, f"walk surfaced an unplanned question: {qid}"
    step = plan[qid]
    card = page.locator(f"[data-question-id='{qid}']")
    if step is None:
        # Skipped: re-flagged for review via needs-answer chips.
        await card.locator("button:has-text('Skip for now')").click()
    elif isinstance(step, str) and step.startswith("free:"):
        # The typed path: the per-kind widget inside the question card.
        await card.locator("input[name='free_text'], textarea[name='free_text']").fill(step[5:])
        await card.locator("button:has-text('Save answer')").click()
    else:
        # The button path: click the choice whose hidden value matches.
        # Value in double quotes: labels like Bachelor's survive.
        await card.locator(
            f'.choice-grid form:has(input[name="value"][value="{step}"]) button'
        ).click()
    return qid


async def _walk_to_review(page, plan) -> list[str]:
    """Answer every question the wizard presents, in its live order."""
    visited: list[str] = []
    while await page.locator(".review-form").count() == 0:
        qid = await _answer_current(page, plan)
        visited.append(qid)
        # The card swaps to the next question (or review); wait for it.
        await page.wait_for_function(
            """prev => {
                const review = document.querySelector('.review-form');
                if (review) return true;
                const el = document.querySelector('[data-question-id]');
                return el && el.dataset.questionId !== prev;
            }""",
            arg=qid,
        )
    return visited


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_full_wizard_journey_buttons_first(run_journey, chromium, portal_server, monkeypatch, theme):
    """The whole interview by buttons (typed only for free-text fields),
    needs-answer chips for skips, quiet review edit, confirm."""

    from applypilot import onboarding

    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _start_interview(page, base, theme, portal_server.token)

            # Every answer went through wizard widgets: the transcript holds
            # NO user chat bubble at all — button picks, typed fields, and
            # skips all render as choice-kind entries (zero-chat by design).
            await _walk_to_review(page, WALK)
            assert await page.locator(".chat-msg.user").count() == 0

            # The skipped gated field (work authorization) is flagged with
            # a chip that jumps to its widget; the widget itself renders
            # unanswered (fabrication guard: never prefilled).
            assert await page.locator(".needs-answer-chips a.chip").count() == 1
            await page.click("a.chip[href='#review-work_authorized']")
            gated = page.locator("#answer-work_authorized")
            assert await gated.input_value() == ""
            await gated.select_option("yes")

            # A quiet save re-renders the review with no chips left — wait
            # for the swap to remove the chip (the old form satisfies a
            # bare .review-form wait while the swap is still in flight).
            await page.click("button:has-text('Save changes')")
            await page.wait_for_selector(".needs-answer-chips a.chip", state="detached")
            await page.wait_for_selector(".review-form")
            assert await page.locator(".needs-answer-chips a.chip").count() == 0

            # Confirm ends the wizard.
            await page.click("button:has-text('Confirm')")
            await page.wait_for_selector("h2:has-text('Profile saved ✓')")

    run_journey(journey())


def test_agent_toggle_round_trip(run_journey, chromium, portal_server, monkeypatch):
    """'Let the agent ask' hands over to free-form chat; 'Back' returns
    to the same choice-first question, agenda intact."""

    from applypilot import onboarding

    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _start_interview(page, base, "dark", portal_server.token)

            # The collapsed ask-card sits on the question screen.
            await page.click("details.agent-assist summary")
            await page.wait_for_selector("details.agent-assist textarea[name='text']")

            # Hand the agenda to the agent…
            await page.click("button:has-text('Let the agent ask')")
            await page.wait_for_selector("button:has-text('Back to guided questions')")
            assert await page.locator("[data-question-id]").count() == 0  # no choice card
            await page.fill("textarea[name='text']", "I have a Master's degree")
            await page.click("button:has-text('Send')")
            await page.wait_for_selector(".chat-msg.assistant")  # the agent replied

            # …and come back to the same question's buttons.
            await page.click("button:has-text('Back to guided questions')")
            await page.wait_for_selector("[data-question-id='full_name']")

    run_journey(journey())
