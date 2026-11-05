"""Full user-journey E2E: one continuous session across every surface.

The spec's Verification journey (art_oJ8ssNjF) as a single pass in a real
Chromium, both themes: token login → choice-first wizard (buttons only;
free text only where an answer genuinely needs one) → review with
needs-answer chips → confirm → find jobs in context → card actions
(dismiss/undo, tailor now, apply now with confirmation) → automation
preset start/stop, auto-apply toggle, daily-cap pause → ask-the-agent
starts and stops a preset → System Check with an in-place re-run.

Preset starts (button and agent) run a sleeping child so Stop always
finds the run still active; the board's empty-state find and the
single-job verbs keep the harmless fast child so their busy states clear
on their own.
"""

import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture()
def alive_presets(monkeypatch):
    """Presets spawn a sleeping child; every other run spawns a fast one.

    Stop (button or agent) must find the preset run still active — a
    `python -c "pass"` child exits in ~100 ms and turns both into races
    with the SSE idle swap. Single-job verbs (Tailor now / Apply now) and
    the board's empty-state find must clear their busy/strip states, so
    they keep the fast child.
    """
    from applypilot.portal import automation, runner

    original_start = runner.start_run
    original_preset = automation.start_preset
    slow = [sys.executable, "-c", "import time; time.sleep(60)"]
    # A 1s child: long enough that the POST-rendered busy/strip/toast
    # states are deterministically readable before the SSE clear lands,
    # short enough that detach waits stay quick.
    fast = [sys.executable, "-c", "import time; time.sleep(1)"]

    def start_slow(kind, params, command=None, emit_jsonl=None):
        return original_start(kind, params, command=slow, emit_jsonl=False)

    def start_fast(kind, params, command=None, emit_jsonl=None):
        return original_start(kind, params, command=fast, emit_jsonl=False)

    monkeypatch.setattr(runner, "start_run", start_fast)

    def preset_slow(name: str):
        monkeypatch.setattr(runner, "start_run", start_slow)
        try:
            return original_preset(name)
        finally:
            monkeypatch.setattr(runner, "start_run", start_fast)

    monkeypatch.setattr(automation, "start_preset", preset_slow)


def seed_journey_jobs() -> None:
    """Two scored jobs: A tailorable+applyable, B plain."""
    from applypilot.database import init_db

    conn = init_db()
    now = datetime.now(timezone.utc)
    rows = [
        ("https://jobs.example/journey-a", "Journey Job A", 9,
         (now - timedelta(minutes=1)).isoformat(), "/tmp/journey-resume.txt"),
        ("https://jobs.example/journey-b", "Journey Job B", 8,
         now.isoformat(), None),
    ]
    for url, title, score, discovered, tailored in rows:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (url, title, site, location, "
            "description, full_description, fit_score, discovered_at, "
            "tailored_resume_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, title, "ExampleBoard", "Remote", "short desc",
             "full description text", score, discovered, tailored))
    conn.commit()


def seed_applied(n: int) -> None:
    """n applications recorded right now (the daily-cap counter's input)."""
    import time as time_mod

    from applypilot.database import init_db

    conn = init_db()
    now = time_mod.strftime("%Y-%m-%dT%H:%M:%S+00:00", time_mod.gmtime())
    for i in range(n):
        conn.execute(
            "INSERT OR REPLACE INTO jobs (url, title, site, description, "
            "apply_status, applied_at, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"https://jobs.example/applied-{i}", "Applied Job",
             "ExampleBoard", "short desc", "applied", now, now))
    conn.commit()


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


async def _answer_current(page, plan):
    """Answer whatever question card the page shows, per the plan."""
    qid = await page.locator("[data-question-id]").first.get_attribute(
        "data-question-id")
    assert qid in plan, f"walk surfaced an unplanned question: {qid}"
    step = plan[qid]
    card = page.locator(f"[data-question-id='{qid}']")
    if step is None:
        await card.locator("button:has-text('Skip for now')").click()
    elif isinstance(step, str) and step.startswith("free:"):
        await card.locator(
            "input[name='free_text'], textarea[name='free_text']"
        ).fill(step[5:])
        await card.locator("button:has-text('Save answer')").click()
    else:
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
def test_full_user_journey_end_to_end(run_journey, chromium, portal_server,
                                      alive_presets, fake_llm, monkeypatch,
                                      theme):
    """The spec's whole journey in one session: login → wizard → review →
    confirm → find jobs → card actions → automation → agent → system check."""

    from applypilot import onboarding

    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)

    async def journey():
        base = portal_server.base_url
        async with chromium() as page:
            # -- 1. Token login --
            await page.goto(f"{base}/")
            await page.wait_for_selector("input[name='token']")
            await page.fill("input[name='token']", portal_server.token)
            await page.click("button:has-text('Open portal')")
            await page.wait_for_selector(".stat-card")

            # -- 2. Choice-first wizard: buttons only --
            await page.goto(f"{base}/onboarding?theme={theme}")
            assert await page.locator("html").get_attribute(
                "data-theme") == theme
            await page.wait_for_selector(
                "button:has-text('Use the configured provider instead')")
            await page.click(
                "button:has-text('Use the configured provider instead')")
            await page.wait_for_selector("[data-question-id]")

            first = page.locator("[data-question-id]").first
            assert await first.locator(".choice-grid button").count() >= 2

            await _walk_to_review(page, WALK)
            # Zero keyboard-only-chat answers: no user chat bubble at all.
            assert await page.locator(".chat-msg.user").count() == 0

            # -- 3. Review: the needs-answer chip flags the skipped gate --
            assert await page.locator(
                ".needs-answer-chips a.chip").count() == 1
            await page.click("a.chip[href='#review-work_authorized']")
            gated = page.locator("#answer-work_authorized")
            assert await gated.input_value() == ""  # never prefilled
            await gated.select_option("yes")
            await page.click("button:has-text('Save changes')")
            await page.wait_for_selector(".needs-answer-chips a.chip",
                                         state="detached")

            # -- 4. Confirm --
            await page.click("button:has-text('Confirm')")
            await page.wait_for_selector("h2:has-text('Profile saved ✓')")

            # -- 5. Find jobs in context --
            await page.goto(f"{base}/jobs?theme={theme}")
            assert await page.locator("html").get_attribute(
                "data-theme") == theme
            await page.wait_for_selector("#jobs-board .empty-state")
            await page.click("button:has-text('Find jobs now')")
            # The POST response re-renders the board with the live strip in
            # place (the SSE-fed page strip baselines at connect, so a short
            # harmless run fits between reconnects — the in-board strip is
            # the deterministic user-visible feedback).
            await page.wait_for_selector("#jobs-board .live-strip")
            assert "Finding jobs" in await page.locator(
                "#jobs-board .live-strip").inner_text()
            # The harmless child finishes; the board feed swaps it away.
            await page.wait_for_selector("#jobs-board .live-strip",
                                         state="detached")
            # Seeded jobs arrive over the board's SSE feed — still no reload.
            seed_journey_jobs()
            await page.wait_for_selector("#jobs-board .job-card")
            board_text = await page.locator("#jobs-board").inner_text()
            assert "Journey Job A" in board_text
            assert "Journey Job B" in board_text

            # -- 6. Card actions --
            card_a = page.locator(".job-card", has_text="Journey Job A")
            card_b = page.locator(".job-card", has_text="Journey Job B")

            # Tailor now on the plain job: busy card + toast, then done.
            await card_b.get_by_role("button", name="Tailor now").click()
            await page.wait_for_selector("#jobs-board .card-busy")
            assert "Tailoring documents…" in await page.locator(
                "#jobs-board .card-busy").inner_text()
            # The start toast is deliberately not asserted here: the SSE
            # refetch (run-started event) re-renders the board without it,
            # so its DOM lifetime is racy on the live board. The toast is
            # asserted deterministically by test_jobs_board.py.
            await page.wait_for_selector("#jobs-board .card-busy",
                                         state="detached")

            # Apply now on the tailored job: confirmation modal first.
            # The trigger is the card's primary label; the modal's backdrop
            # and Cancel labels share the same for= attribute.
            await card_a.locator("label.button.primary").click()
            modal = page.locator(".modal-card")
            await modal.wait_for()
            assert "Your tailored resume" in await modal.inner_text()
            await modal.get_by_role("button", name="Confirm & apply").click()
            await page.wait_for_selector("#jobs-board .card-busy")
            assert "Submitting your application…" in await page.locator(
                "#jobs-board .card-busy").inner_text()
            await page.wait_for_selector("#jobs-board .card-busy",
                                         state="detached")

            # Dismiss the plain job; Undo brings it back.
            await card_b.get_by_role("button", name="Dismiss").click()
            board = page.locator("#jobs-board")
            await board.locator(".dismissed-section .job-card").wait_for(
                state="attached")
            await board.locator(".dismissed-section summary").click()
            await page.get_by_role("button", name="Undo").click()
            # Wait for the SETTLED condition: the undo response removes the
            # section, and a stale in-flight SSE refetch (rendered from the
            # pre-undo state) is corrected by the undo's own board-changed
            # event moments later — an immediate read can catch that
            # transient.
            await page.wait_for_selector("#jobs-board .dismissed-section",
                                         state="detached")
            await page.wait_for_selector("#jobs-board > .card-grid .job-card")
            assert await board.locator(".dismissed-section").count() == 0

            # -- 7. Automation: preset start/stop by button --
            await page.goto(f"{base}/automation?theme={theme}")
            assert await page.locator("html").get_attribute(
                "data-theme") == theme
            await page.wait_for_selector(".preset-grid")

            preset = page.locator(".preset-card[data-preset='find_jobs']")
            await preset.locator("[data-action='start']").click()
            await page.wait_for_selector(
                ".preset-card[data-preset='find_jobs'] [data-action='stop']")
            assert "Working now" in await preset.inner_text()
            await preset.locator("[data-action='stop']").click()
            await page.wait_for_selector(
                ".preset-card[data-preset='find_jobs'] [data-action='stop']",
                state="detached")
            assert await preset.locator(
                "[data-action='start']").is_enabled()

            # The daily cap the user sets through the Adjust disclosure.
            apply_card = page.locator(
                ".preset-card[data-preset='apply_matches']")
            await apply_card.locator("details.adjust summary").click()
            await apply_card.locator(
                "input[name='daily_cap'][value='5']").check()
            await apply_card.locator("button:has-text('Save')").click()
            # A plain form post re-navigates; re-pin the theme and confirm
            # the value persisted.
            await page.wait_for_url("**/automation")
            await page.goto(f"{base}/automation?theme={theme}")
            apply_card = page.locator(
                ".preset-card[data-preset='apply_matches']")
            await apply_card.locator("details.adjust summary").click()
            assert await apply_card.locator(
                "input[name='daily_cap'][value='5']").is_checked()

            # -- 8. Ask the agent: start, then stop, a preset --
            fake_llm.respond('{"action": "start", "preset": "refresh_scores"}')
            await page.fill("#agent-panel input[name='text']",
                            "refresh my scores")
            await page.click("#agent-panel button:has-text('Ask')")
            await page.wait_for_selector(
                ".chat-msg.assistant:has-text('Started refresh scores')")

            fake_llm.respond('{"action": "stop", "preset": "refresh_scores"}')
            await page.fill("#agent-panel input[name='text']",
                            "stop refreshing")
            await page.click("#agent-panel button:has-text('Ask')")
            await page.wait_for_selector(
                ".chat-msg.assistant:has-text('Stopped refresh scores')")

            # -- 9. The daily-cap pause lands on the card (next tick) --
            seed_applied(5)
            auto = page.locator(
                ".toggle-card:has([data-toggle-state='auto_apply_enabled'])")
            await auto.locator("button").click()
            await page.wait_for_selector(
                "[data-toggle-state='auto_apply_enabled']:has-text('On')")
            pause = page.locator("[data-testid='cap-pause']")
            await pause.wait_for(timeout=90_000)
            assert "Daily cap reached" in await pause.inner_text()
            assert "5 of 5 applications used today." in await page.locator(
                "[data-testid='cap-usage']").inner_text()

            # -- 10. System Check: renders and re-runs in place --
            await page.click("nav.side-nav a:has-text('System check')")
            await page.wait_for_selector("#system-check-list")
            assert await page.locator("h1").inner_text() == "System check"
            assert await page.locator(".syscheck").count() == 11
            await page.click("button:has-text('Check again')")
            await page.wait_for_selector("#check-busy.htmx-indicator",
                                         state="attached")
            await page.wait_for_function(
                "document.querySelector('#check-busy').offsetParent === null")
            assert await page.locator(".syscheck").count() == 11
            assert "system-check" in page.url

    run_journey(journey())
