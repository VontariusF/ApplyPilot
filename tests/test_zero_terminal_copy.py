"""Zero-terminal copy scan (spec surface table).

The portal is the only interface most users ever see: no rendered surface
may show a shell command, a config-file path, or terminal/wizard wording.
Two layers — the template sources (catch copy before it ever renders) and
every rendered portal page (catch copy built dynamically in routes) —
plus the login page's Reveal-token behavior.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from applypilot import engine_auth, onboarding
from applypilot.portal.app import create_app

TEMPLATES = sorted((Path(__file__).parents[1] / "src" / "applypilot" / "portal" / "templates").rglob("*.html"))

# Patterns that must never reach user-facing copy.
FORBIDDEN = [
    ("shell command", r"claude\s+auth|claude\s+-p\b"),
    ("shell variable export", r"export\s+[A-Z_]{2,}="),
    ("package-manager command", r"\b(?:pip3?|npm|bun)\s+(?:install|add|i)\b"),
    ("CLI invocation", r"\bapplypilot\s+(?:init|doctor|onboard)\b"),
    ("config file path", r"~/\.applypilot"),
    ("terminal wording", r"\bterminal\b"),
    ("wizard wording", r"\bwizard\b"),
]


def _violations(text: str) -> list[str]:
    out = []
    for why, pattern in FORBIDDEN:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            line_no = text[: m.start()].count("\n") + 1
            snippet = text[max(0, m.start() - 40) : m.end() + 40].replace("\n", " ")
            out.append(f"{why}: ...{snippet}... (line {line_no})")
    return out


# ---------------------------------------------------------------------------
# Layer 1: template sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_sources_are_zero_terminal(template):
    found = _violations(template.read_text())
    assert not found, f"{template.name}: " + "; ".join(found)


# ---------------------------------------------------------------------------
# Layer 2: rendered pages (worst-case gate states and both onboarding modes)
# ---------------------------------------------------------------------------

PAGES = ["/", "/jobs", "/runs", "/onboarding", "/engine", "/settings", "/system-check"]


def _patch_gates_worst_case(monkeypatch):
    """Guidance-heavy states: claude unsigned-in, droid key missing."""
    status = engine_auth.EngineStatus
    monkeypatch.setattr(
        engine_auth,
        "claude_status",
        lambda: status("claude", engine_auth.SIGNED_OUT, None, "/usr/bin/claude", "probe failed"),
    )
    monkeypatch.setattr(
        engine_auth, "droid_status", lambda: status("droid", engine_auth.SIGNED_OUT, None, "/usr/bin/droid")
    )


def test_rendered_pages_are_zero_terminal(portal_client, monkeypatch):
    client, _ = portal_client
    _patch_gates_worst_case(monkeypatch)

    for provider_ready in (False, True):  # gate panel vs. full wizard
        monkeypatch.setattr(onboarding, "provider_ready", lambda v=provider_ready: v)
        for path in PAGES:
            r = client.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code}"
            found = _violations(r.text)
            assert not found, f"{path} (provider_ready={provider_ready}): " + "; ".join(found)


# ---------------------------------------------------------------------------
# Login page: Reveal-token replaces file-path references
# ---------------------------------------------------------------------------


def test_login_page_hides_token_but_offers_reveal(portal_env):
    app = create_app()
    bare = TestClient(app, base_url="http://127.0.0.1")  # no token: 401 page
    r = bare.get("/")
    assert r.status_code == 401
    assert "Reveal token" in r.text
    assert app.state.token not in r.text  # the token itself is not on the page
    assert "~/.applypilot" not in r.text  # no file-path references
    assert "terminal" not in r.text.lower()


def test_reveal_token_shows_token_on_login_page(portal_env):
    app = create_app()
    bare = TestClient(app, base_url="http://127.0.0.1")
    r = bare.post("/login/reveal")
    assert r.status_code == 200
    assert "Reveal token" in r.text
    assert app.state.token in r.text  # now shown, once
    assert r.text.count(app.state.token) == 1
    assert 'onfocus="this.select()"' in r.text  # copy-friendly field


def test_reveal_token_rejected_for_non_local_host(portal_env):
    app = create_app()
    bare = TestClient(app, base_url="http://evil.example")
    r = bare.post("/login/reveal")
    assert r.status_code == 400  # localhost-only guard fires before any reveal


# ---------------------------------------------------------------------------
# Layer 3: the resume-first chip surfaces (spec part 4) — the interview
# renders prefilled chips and gap chips, the review renders badges; both
# are new dynamically-built copy the source scan can't see.


def _walk_to_review_by_skipping(client):
    """Skip every open question (skips are quiet: no agent turn) until the
    review form renders."""
    for _ in range(len(onboarding.QUESTION_SPECS) + 1):
        page = client.get("/onboarding/chat").text
        if "Review your profile" in page:
            return page
        qid = re.search(r'data-question-id="([a-z_]+)"', page)
        assert qid, "question card or review form expected"
        client.post("/onboarding/answer", data={"question_id": qid.group(1), "skip": "1"})
    raise AssertionError("never reached review")


def test_chip_surfaces_are_zero_terminal(portal_client, fake_llm, monkeypatch):
    """The interview's chip panel and the review's from-your-resume badges
    render plain-language copy only — new dynamically-built copy the
    template-source scan cannot see (layer 2 only walks static pages)."""
    client, _ = portal_client
    draft_reply = "Read your resume.\n\n```json\n" + json.dumps({
        "_applypilot_profile": {
            "personal": {"full_name": "Jane Doe", "email": "jane@example.com",
                         "country": "United States"},
            "experience": {"education_level": "Bachelor's"},
        },
        "_applypilot_search": {"queries": ["Data Engineer"], "location": "Austin, TX",
                               "remote": True},
    }) + "\n```"
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    fake_llm.respond(draft_reply)  # the kickoff turn's extraction reply

    onboarding.start_intake()
    client.post("/onboarding/resume", data={"resume_text": "resume"})
    client.post("/onboarding/start", data={"mode": "llm"})

    r = client.get("/onboarding/chat")
    assert r.status_code == 200
    assert "From your resume" in r.text  # the chip panel actually rendered
    found = _violations(r.text)
    assert not found, "interview chips: " + "; ".join(found)

    review = _walk_to_review_by_skipping(client)
    assert "from your resume" in review  # the badge actually rendered
    found = _violations(review)
    assert not found, "review badges: " + "; ".join(found)
