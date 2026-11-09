"""Portal onboarding surface: gate card, chat flow over HTTP, SSE push.

The same scripted `claude -p` fake from test_onboarding_agent drives the
HTTP flow; engine status functions are patched so gate states render
without the real CLIs. The SSE generator is collected directly (the
test_portal_sse pattern) with the poll interval accelerated.
"""

import asyncio
import json

import pytest

from kestrel import config, engine_auth, onboarding
from kestrel.portal import app as portal_app

from tests.test_onboarding_agent import FAKE_CLAUDE, FakeLLMClient

SIGNED_IN = engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN, "claude.ai", "/usr/bin/claude")
SIGNED_OUT = engine_auth.EngineStatus("claude", engine_auth.SIGNED_OUT, None, "/usr/bin/claude")
NOT_INSTALLED = engine_auth.EngineStatus("claude", engine_auth.NOT_INSTALLED, None, None)

DROID_QUIET = engine_auth.EngineStatus("droid", engine_auth.NOT_INSTALLED, None, None)


def patch_gates(monkeypatch, claude=SIGNED_OUT):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: claude)
    monkeypatch.setattr(engine_auth, "droid_status", lambda *a, **k: DROID_QUIET)


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(fake))
    return fake


@pytest.fixture()
def fake_llm(monkeypatch):
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    client = FakeLLMClient()
    monkeypatch.setattr(onboarding.LLMBackend, "_client", lambda self: client)
    return client


# ---------------------------------------------------------------------------
# Gate and choice states
# ---------------------------------------------------------------------------

def test_page_gate_locked_without_signin_or_provider(portal_client, monkeypatch):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=NOT_INSTALLED)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    r = client.get("/onboarding")
    assert r.status_code == 200
    assert "Agent account needed" in r.text
    assert 'action="/engine/login"' in r.text
    assert "/onboarding/start" not in r.text  # nothing startable while locked


def test_page_choice_card_with_provider_ready(portal_client, monkeypatch, fake_llm):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_OUT)
    r = client.get("/onboarding")
    assert r.status_code == 200
    assert "Choose how the interview runs" in r.text
    assert 'value="llm"' in r.text  # provider fallback start button
    assert 'name="next" value="/onboarding"' in r.text  # sign-in returns here


def test_page_start_card_when_signed_in(portal_client, monkeypatch, fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    r = client.get("/onboarding")
    assert r.status_code == 200
    assert "Start the interview" in r.text


def test_engine_login_redirects_back_to_onboarding(portal_client, monkeypatch):
    import subprocess

    client, _ = portal_client

    class LiveProc:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(engine_auth, "start_login", lambda: (LiveProc(), None))
    patch_gates(monkeypatch)
    r = client.post("/engine/login", data={"next": "/onboarding"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/onboarding?")


def test_first_ready_page_opens_the_intake_screen(portal_client, monkeypatch, fake_llm):
    """Resume-first entry (spec art_ti3XJuNr): the first page a ready user
    sees opens intake — the resume upload — with the interview start kept
    as the explicit skip path. A locked-out user still sees only the gate."""
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    r = client.get("/onboarding")
    assert r.status_code == 200
    assert "Start with your resume" in r.text
    assert "/onboarding/resume" in r.text
    assert "Start the interview" in r.text  # skip affordance survives
    state = onboarding.load_state()
    assert state is not None and state.phase == onboarding.INTAKE

    patch_gates(monkeypatch, claude=SIGNED_OUT)  # claude gate off again
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)  # no provider left
    r = client.get("/onboarding")
    assert "Agent account needed" in r.text  # locked out: nothing startable


def test_intake_page_flow_drives_the_resume_first_kickoff(portal_client, monkeypatch, fake_llm):
    """The rendered intake screen feeds the real flow: page render opens
    intake, the upload stores the pending context, and starting the
    interview kicks off with the resume text reaching the provider."""
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    draft_reply = "Read your resume.\n\n```json\n" + json.dumps({
        "_kestrel_profile": {
            "personal": {"full_name": "Jane Doe", "email": "jane@example.com",
                         "country": "United States"},
            "work_authorization": {},
        },
        "_kestrel_search": {"queries": ["Data Engineer"], "location": "Austin, TX"},
    }) + "\n```"
    fake_llm.replies[:] = [draft_reply]  # first turn (the kickoff) returns the draft

    client.get("/onboarding")  # intake opens on the first ready render
    client.post("/onboarding/resume", data={"resume_text": "MARKER-RESUME-2468"})
    client.post("/onboarding/start", data={"mode": "llm"})
    state = onboarding.load_state()
    assert state.profile_draft["personal"]["full_name"] == "Jane Doe"
    # The kickoff prompt carried the uploaded resume text to the provider.
    assert any("MARKER-RESUME-2468" in m["content"] for m in fake_llm.calls[0])


def test_engine_login_rejects_unknown_destination(portal_client, monkeypatch):
    import subprocess

    client, _ = portal_client

    class LiveProc:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(engine_auth, "start_login", lambda: (LiveProc(), None))
    patch_gates(monkeypatch)
    r = client.post("/engine/login", data={"next": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/engine?")


# ---------------------------------------------------------------------------
# The full interview flow over HTTP
# ---------------------------------------------------------------------------

def test_full_flow_start_send_confirm_writes_files(portal_client, monkeypatch,
                                                   fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)

    r = client.post("/onboarding/start", data={"mode": "claude"})
    assert r.status_code == 200
    assert "Tell me about your background" in r.text  # greeting rendered
    assert "chat-composer" in r.text

    r = client.post("/onboarding/send",
                    data={"text": "Data engineer, Austin TX, $140k, authorized, no sponsorship"})
    assert r.status_code == 200
    assert "Review your profile" in r.text  # editable review form
    assert "Confirm" in r.text

    r = client.post("/onboarding/confirm")
    assert r.status_code == 200
    assert "Profile saved" in r.text
    assert config.PROFILE_PATH.exists()
    assert config.PROFILE_MD_PATH.exists()
    assert config.SEARCH_CONFIG_PATH.exists()
    assert json.loads(config.PROFILE_PATH.read_text())["personal"]["full_name"] == "Test Person"

    # Completion unlocks the dashboard (the root gate stops redirecting).
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_start_twice_resumes_instead_of_resetting(portal_client, monkeypatch,
                                                  fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    client.post("/onboarding/start", data={"mode": "claude"})
    client.post("/onboarding/send", data={"text": "I work as a data engineer"})
    # A second tab (or the post-sign-in refresh) hits start again.
    r = client.post("/onboarding/start", data={"mode": "claude"})
    assert "I work as a data engineer" in r.text  # transcript survived


def test_send_error_renders_banner_in_partial(portal_client, monkeypatch,
                                              fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    client.post("/onboarding/start", data={"mode": "claude"})
    # The CLI vanishes and no provider is configured: no fallback path.
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: None)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    r = client.post("/onboarding/send", data={"text": "hello"})
    assert r.status_code == 200
    assert "alert error" in r.text
    assert "claude" in r.text.lower()


def test_resume_upload_via_multipart(portal_client, monkeypatch, fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    client.post("/onboarding/start", data={"mode": "claude"})
    r = client.post(
        "/onboarding/resume",
        files={"resume_file": ("me.txt", b"SKILLS Python, SQL", "text/plain")},
        data={"resume_text": ""})
    assert r.status_code == 200
    assert config.RESUME_PATH.read_text() == "SKILLS Python, SQL"
    assert "Resume provided" in r.text  # note bubble confirms intake


def test_resume_oversize_rejected(portal_client, monkeypatch, fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    client.post("/onboarding/start", data={"mode": "claude"})
    r = client.post(
        "/onboarding/resume",
        files={"resume_file": ("big.pdf", b"x" * (portal_app.MAX_RESUME_UPLOAD_BYTES + 1),
                               "application/pdf")},
        data={"resume_text": ""})
    assert r.status_code == 200
    assert "capped at" in r.text
    assert not config.RESUME_PDF_PATH.exists()


def test_redo_from_done(portal_client, monkeypatch, fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    client.post("/onboarding/start", data={"mode": "claude"})
    client.post("/onboarding/send", data={"text": "background and preferences"})
    client.post("/onboarding/confirm")
    r = client.post("/onboarding/redo")
    assert "Start the interview" in r.text or "Tell me about your background" in r.text


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

def test_stream_pushes_partial_on_state_change(portal_client, monkeypatch,
                                               fake_claude):
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    monkeypatch.setattr(portal_app, "CHAT_POLL_SECONDS", 0.05)

    async def drive():
        baseline = await portal_app._chat_signature()
        agen = portal_app._stream_chat(baseline)
        try:
            # First event: the initial sync (P2-2) — the pre-flip surface.
            first = await agen.__anext__()
            # From here the stream pushes on change. Flip the state, expect
            # the kickoff surface as the next event.
            onboarding.start_interview("claude")
            second = await agen.__anext__()
        finally:
            await agen.aclose()
        return first, second

    first, second = asyncio.run(drive())
    assert first["event"] == "chat"
    assert "Tell me about your background" not in first["data"]
    assert second["event"] == "chat"
    assert "Tell me about your background" in second["data"]  # kickoff ran


def test_stream_initial_sync_then_quiet_without_changes(portal_client, monkeypatch,
                                                        fake_claude):
    """Connect: one initial-sync event, then quiet — zero further pushes
    while nothing changes. (The first event re-syncs a reconnecting page;
    on a no-change connect it is an identical-content swap, the accepted
    cost the page's data-chat-seq guard renders a no-op.)"""
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    monkeypatch.setattr(portal_app, "CHAT_POLL_SECONDS", 0.01)

    async def drive():
        baseline = await portal_app._chat_signature()
        agen = portal_app._stream_chat(baseline)
        events = [e async for e in agen]  # runs to the idle close
        return events

    onboarding.start_interview("claude")  # stable before the stream starts
    events = asyncio.run(drive())
    assert [e["event"] for e in events] == ["chat", "end"]


def test_stream_initial_sync_covers_reconnect_gap(portal_client, monkeypatch,
                                                  fake_claude):
    """A reconnect's baseline already contains whatever changed while no
    stream listened (the ~8s idle close) — the first event must re-sync
    the page to the present anyway (P2-2), or that change is swallowed
    until the next POST or reload."""
    client, _ = portal_client
    patch_gates(monkeypatch, claude=SIGNED_IN)
    monkeypatch.setattr(portal_app, "CHAT_POLL_SECONDS", 0.01)

    onboarding.start_interview("claude")  # lands in the idle-close gap

    async def drive():
        baseline = await portal_app._chat_signature()
        agen = portal_app._stream_chat(baseline)
        try:
            return await agen.__anext__()
        finally:
            await agen.aclose()

    event = asyncio.run(drive())
    assert event["event"] == "chat"
    assert "Tell me about your background" in event["data"]
