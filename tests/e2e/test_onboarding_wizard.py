"""Wizard E2E: the choice-first interview, agent toggle, and review form.

A real Chromium drives the portal the way the user does (the PR #29
harness). The journey answers every choice question with a button click
and types only into genuinely-free-text fields — the zero-chat promise —
then meets the needs-answer chips on review and confirms.

The walk is order-agnostic: the registry's depends_on rules decide which
question appears next, so the journey reads the live question card and
answers it from the plan below.
"""

import json

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


# ---------------------------------------------------------------------------
# Resume-first intake journeys (spec art_ti3XJuNr, part 4): upload at
# intake, gaps-only questions, editable chips, double-click guard.

# The scripted extraction: everything a rich resume would yield, bridged
# by draft_to_answers — the intake kickoff's reply.
E2E_DRAFT = "Read your resume.\n\n```json\n" + json.dumps({
    "_applypilot_profile": {
        "personal": {"full_name": "Jane Doe", "email": "jane@example.com",
                     "phone": "+1 555 0100", "city": "Austin",
                     "country": "United States"},
        "experience": {"education_level": "Bachelor's"},
    },
    "_applypilot_search": {"queries": ["Data Engineer"], "location": "Austin, TX",
                           "remote": True},
}) + "\n```"

BRIDGED = {"full_name", "email", "phone", "city", "country", "education",
           "work_style", "target_roles", "skills_languages", "skills_frameworks"}


async def _wait_idle(page):
    """Wait for the in-flight guard to release every control."""
    await page.wait_for_function(
        "!document.querySelector('#onboarding-surface [data-ap-busy]')")


async def _open_intake(page, base, theme, token):
    """Log in and land on the intake screen (the first ready surface)."""
    await page.goto(f"{base}/?theme={theme}")
    await page.wait_for_selector("input[name='token']")
    await page.fill("input[name='token']", token)
    await page.click("button:has-text('Open portal')")
    await page.wait_for_selector(".stat-card")
    await page.goto(f"{base}/onboarding?theme={theme}")
    assert await page.locator("html").get_attribute("data-theme") == theme
    await page.wait_for_selector("h2:has-text('Start with your resume')")
    # The skip path is announced on the intake screen itself.
    assert await page.locator("text=Skip the resume").count() == 1
    # Extraction progress copy exists (hidden until the upload is in flight).
    assert await page.locator("#intake-busy").count() == 1


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_intake_resume_then_chips_and_gaps(run_journey, chromium, portal_server,
                                           fake_llm, monkeypatch, theme):
    """Upload at intake, start with the resume in play: the chips panel
    shows what the resume bridged, every question card is a gap, editing
    a chip records the overwrite, clearing one flags the field on review,
    and the review screen badges resume-sourced values."""

    from applypilot import onboarding

    # A ready provider is what makes intake the first ready surface.
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)

    fake_llm.respond(E2E_DRAFT)  # the intake kickoff's extraction reply

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _open_intake(page, base, theme, portal_server.token)

            # The paste path: the resume lands before any question is asked.
            await page.fill("textarea[name='resume_text']", "Jane Doe — resume text")
            await page.click("button:has-text('Save resume')")
            await page.wait_for_selector("text=✓ Resume provided")

            # The start card is the transition out of intake (and the skip
            # path when no resume was saved).
            await page.click("button:has-text('Use the configured provider instead')")
            await page.wait_for_selector("[data-question-id]")

            # Gaps-only: the current question is never one the resume bridged.
            qid = await page.locator("[data-question-id]").first.get_attribute("data-question-id")
            assert qid not in BRIDGED, f"{qid} was bridged — the agenda must not ask it"

            # The chips panel shows the resume's answers, editable in place.
            assert await page.locator(".prefill-chip[data-chip-for]").count() > 0
            # The open agenda rides the question card as needs-answer chips.
            assert await page.locator(".question-card .needs-answer-chips .chip").count() > 0

            # Edit a chip: the resume's value prefilos the editor, the
            # overwrite records as an answer and retires the chip.
            chip = page.locator(".prefill-chip[data-chip-for='full_name']")
            await chip.locator("summary").click()
            field = chip.locator("input[name='free_text']")
            assert await field.input_value() == "Jane Doe"
            await field.fill("Jane Q. Doe")
            await chip.locator("button:has-text('Save answer')").click()
            await page.wait_for_selector(".prefill-chip[data-chip-for='full_name']",
                                         state="detached")
            state = onboarding.load_state()
            assert state.answers["full_name"] == "Jane Q. Doe"  # recorded, beats extraction

            # Clear a chip: the resume's value is removed, the field
            # returns to the review screen as add-later.
            chip = page.locator(".prefill-chip[data-chip-for='email']")
            await chip.locator("summary").click()
            await chip.locator("button:has-text('Clear — leave it empty')").click()
            await page.wait_for_selector(".prefill-chip[data-chip-for='email']",
                                         state="detached")
            assert onboarding.load_state().answers["email"] == ""

            # The remaining gaps walk to review (order-agnostic, per plan).
            await _walk_to_review(page, WALK)

            # Review: the edited name shows the recorded value with NO badge;
            # resume-sourced values still carry theirs; the cleared email is
            # flagged as add-later.
            assert await page.locator(
                "label[for='answer-full_name'] .chip.from-resume").count() == 0
            assert await page.locator("#answer-full_name").input_value() == "Jane Q. Doe"
            assert await page.locator(
                "label[for='answer-education'] .chip.from-resume").count() == 1
            await page.click("a.chip[href='#review-email']")
            assert await page.locator("#answer-email").input_value() == ""

            await page.click("button:has-text('Confirm')")
            await page.wait_for_selector("h2:has-text('Profile saved ✓')")

    run_journey(journey())


def test_double_click_cannot_re_answer(run_journey, chromium, portal_server,
                                       fake_llm, monkeypatch):
    """The guard (spec H5): while an answer request is in flight the
    whole grid is disabled — a repeat click cannot re-answer — and the
    controls come back when the swap lands."""

    import time

    from applypilot import onboarding

    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)

    def slow_ack(state, *args, **kwargs):
        ack_calls.append(1)
        time.sleep(1.0)  # hold the request in flight so the guard is visible
        return "Got it."

    ack_calls: list[int] = []
    monkeypatch.setattr(onboarding, "_agent_turn", slow_ack)

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            await _open_intake(page, base, "dark", portal_server.token)
            # Straight to the interview without a resume (the skip path).
            await page.click("button:has-text('Use the configured provider instead')")
            await page.wait_for_selector("[data-question-id]")

            # Identity first: the agenda may open on the free-text name
            # question. Walk the plan until a choice grid is on screen,
            # waiting for the guard to release between answers.
            for _ in range(len(WALK)):
                await _wait_idle(page)
                if await page.locator(".question-card .choice-grid").count():
                    break
                qid = await page.locator("[data-question-id]").first.get_attribute(
                    "data-question-id")
                await _answer_current(page, {qid: WALK[qid]})
            else:
                pytest.fail("no choice question surfaced")

            grid = page.locator(".question-card .choice-grid")
            first = grid.locator("form button").first
            other = grid.locator("form button").nth(1)
            n_before = len(ack_calls)
            posts: list[tuple[str, int]] = []
            page.on(
                "response",
                lambda r: posts.append((r.url, r.status))
                if r.request.method == "POST" else None,
            )
            # Resolve the handle BEFORE the submit: a locator would
            # re-resolve onto the NEXT card after the swap, and the
            # scripted click would answer the new question.
            other_handle = await other.element_handle()
            # Capture the current question BEFORE the click: the swap can
            # land faster than the steps below, and the wait must assert
            # the question changed since the click, not since the read.
            prev_qid = await page.locator("[data-question-id]").first.get_attribute(
                "data-question-id")
            await first.click()
            # In flight: every control on the surface is disabled — a
            # repeat click lands on a dead button.
            assert await other.is_disabled()
            # And a scripted DOM click on the disabled button does nothing.
            try:
                await other_handle.dispatch_event("click")
            except Exception:
                # The swap won the race and detached the old card; the
                # ack-count assertion below remains the authoritative
                # no-double-answer check.
                pass

            await page.wait_for_function(
                """prev => {
                    const el = document.querySelector('[data-question-id]');
                    return el && el.dataset.questionId !== prev;
                }""",
                arg=prev_qid,
            )
            # Settled: exactly one ack ran for the submitted answer (the
            # dispatched click on the disabled button enqueued nothing),
            # and the next card's controls are live again.
            assert len(ack_calls) == n_before + 1, posts
            # Patient: the unlock runs in the XHR loadend hook, which can
            # land a frame after the swap Playwright observed.
            from playwright.async_api import expect
            await expect(grid.locator("form button").first).to_be_enabled(
                timeout=5000)

    run_journey(journey())
