"""Onboarding interview agent: backends, state, profile write-back.

The scripted `claude -p` fake here is the A1 validation artifact: it pins
the print-mode contract verified live against claude 2.1.197 on 2026-11-04
(`claude -p --output-format json [--resume SID] PROMPT` -> one JSON envelope
with result/session_id/is_error; unsigned-in = exit 1 with is_error true)
and drives the multi-turn state transitions on top of it.
"""

import json
import os
import stat

import pytest
import yaml

from applypilot import config, engine_auth, onboarding

SIGNED_IN = engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN, "claude.ai", "/usr/bin/claude")
SIGNED_OUT = engine_auth.EngineStatus("claude", engine_auth.SIGNED_OUT, None, "/usr/bin/claude")

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Scripted `claude -p` fake — pins the print-mode contract.

argv:   claude -p --output-format json [--resume SESSION_ID] PROMPT
stdout: one JSON envelope {type, subtype, is_error, result, session_id}
Any argv outside that contract is a loud exit-2, so a test that passes
proves the real argv wiring.
"""
import json
import os
import sys

args = sys.argv[1:]
if args[:3] != ["-p", "--output-format", "json"]:
    sys.stderr.write("unexpected argv: %r\n" % (args,))
    sys.exit(2)
rest = args[3:]
resume = None
if len(rest) >= 2 and rest[0] == "--resume":
    resume, rest = rest[1], rest[2:]
prompt = rest[0] if rest else ""

log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"resume": resume, "prompt": prompt}) + "\n")


def envelope(result, is_error=False):
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "result": result, "session_id": "fake-session-1",
    }))


PROFILE_BLOCK = """Here is your profile.

```json
{"_applypilot_profile": {
    "personal": {"full_name": "Test Person", "email": "test@example.com"},
    "work_authorization": {"legally_authorized_to_work": "Yes",
                           "require_sponsorship": "No"},
    "availability": {"earliest_start_date": "Immediately"},
    "compensation": {"salary_expectation": "140000", "salary_currency": "USD"},
    "experience": {"current_title": "Data Engineer",
                   "years_of_experience_total": "6",
                   "target_role": "Data Engineer"},
    "skills_boundary": {"programming_languages": "Python, SQL"},
    "resume_facts": {},
    "eeo_voluntary": {}},
 "_applypilot_search": {"queries": ["Data Engineer", "Analytics Engineer"],
                        "location": "Austin, TX", "remote": true}}
```
"""

if os.environ.get("FAKE_CLAUDE_MODE") == "unsigned_in":
    # The real envelope observed live on an unsigned-in CLI (exit 1).
    envelope("Not logged in · Please run /login", is_error=True)
    sys.exit(1)
if "opened onboarding" in prompt:       # turn 1: kickoff
    envelope("Hello! Tell me about your background.")
elif resume == "fake-session-1":        # turn 2+: resumed session
    envelope("Thanks! " + PROFILE_BLOCK)
elif resume is not None:                # unknown session id = contract break
    sys.stderr.write("unknown session: %s\n" % resume)
    sys.exit(2)
else:                                   # user turn without --resume = contract break
    sys.stderr.write("user turn without --resume\n")
    sys.exit(2)
'''


class FakeLLMClient:
    """Stand-in for llm.get_client(): records turns, returns a fixed reply."""

    def __init__(self, replies=None):
        self.replies = list(replies or ["Hello from the provider."])
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Install the scripted fake on a temp PATH and point the backend at it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(fake))
    return fake


@pytest.fixture()
def signed_in(monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: SIGNED_IN)


@pytest.fixture()
def fake_llm(monkeypatch):
    """Provider configured + a fake client behind LLMBackend._client."""
    monkeypatch.setattr(onboarding, "provider_ready", lambda: True)
    client = FakeLLMClient()
    monkeypatch.setattr(onboarding.LLMBackend, "_client", lambda self: client)
    return client


# ---------------------------------------------------------------------------
# A1: the scripted two-turn claude -p exchange (fake pinned to the live contract)
# ---------------------------------------------------------------------------

def test_a1_two_turn_exchange_drives_state_transitions(
        app_dir, fake_claude, signed_in, monkeypatch, tmp_path):
    prompt_log = tmp_path / "prompts.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(prompt_log))

    state = onboarding.start_interview("claude")
    assert state.backend == "claude"
    assert state.phase == onboarding.INTERVIEW
    assert state.claude_session_id == "fake-session-1"
    assert any("background" in m["content"] for m in state.messages
               if m["role"] == "assistant")

    state = onboarding.send_message(
        "Data engineer, Austin TX, $140k min, authorized, no sponsorship, remote ok")
    assert state.phase == onboarding.REVIEW  # draft extracted from turn 2
    assert state.profile_draft["personal"]["full_name"] == "Test Person"

    profile = onboarding.confirm_profile()
    assert set(profile) == set(onboarding._PROFILE_KEYS)  # eight-key schema
    assert profile["personal"]["password"] == ""  # never collected in chat
    assert onboarding.load_state().phase == onboarding.DONE

    # Contract evidence: turn 1 had no --resume, turn 2 resumed the session.
    turns = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    assert turns[0]["resume"] is None
    assert turns[1]["resume"] == "fake-session-1"
    assert "opened onboarding" in turns[0]["prompt"]


def test_a1_unsigned_in_envelope_falls_back_to_provider(
        app_dir, fake_claude, signed_in, fake_llm, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "unsigned_in")
    state = onboarding.start_interview("claude")
    # Sign-in looked green but print mode failed (the observed exit-1
    # envelope) — the provider takes over and the user sees a note.
    assert state.backend == "llm"
    assert any("continuing with the configured provider" in m["content"]
               for m in state.messages if m["role"] == "note")
    assert state.messages[-1]["content"] == "Hello from the provider."


def test_a1_mid_interview_fallback_seeds_provider_transcript(
        app_dir, fake_claude, signed_in, fake_llm, monkeypatch):
    state = onboarding.start_interview("claude")
    assert state.backend == "claude"
    # The CLI vanishes mid-interview (upgrade, PATH change).
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: None)
    state = onboarding.send_message("I'm a data engineer from Austin")
    assert state.backend == "llm"
    assert state.claude_session_id is None
    assert state.messages[-1]["content"] == "Hello from the provider."
    # The rebuilt transcript carries the system prompt + the user's message.
    first = fake_llm.calls[-1]
    assert first[0]["role"] == "system"
    assert any(m["content"] == "I'm a data engineer from Austin" for m in first)


# ---------------------------------------------------------------------------
# Gate and readiness
# ---------------------------------------------------------------------------

def test_start_refused_until_gate_passes(app_dir, monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: SIGNED_OUT)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    with pytest.raises(onboarding.OnboardingError, match="not signed in"):
        onboarding.start_interview("claude")
    with pytest.raises(onboarding.OnboardingError, match="No LLM provider"):
        onboarding.start_interview("llm")


def test_provider_skip_requires_configured_llm(app_dir, monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: SIGNED_OUT)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)
    with pytest.raises(onboarding.OnboardingError, match="No LLM provider"):
        onboarding.start_interview("llm")


def test_llm_start_seeds_full_message_list(app_dir, fake_llm):
    state = onboarding.start_interview("llm")
    assert state.backend == "llm"
    first_call = fake_llm.calls[0]
    assert first_call[0]["role"] == "system"
    assert first_call[0]["content"] == onboarding.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_state_roundtrip_survives_restart(app_dir, fake_llm):
    onboarding.start_interview("llm")
    onboarding.send_message("six years of data engineering")
    state = onboarding.load_state()
    assert state.phase == onboarding.INTERVIEW
    # The transcript holds only what the user sees: greeting, their reply,
    # the agent's answer — the system prompt and kickoff prompt are rebuilt
    # per turn, not stored.
    roles = [m["role"] for m in state.messages]
    assert roles == ["assistant", "user", "assistant"]
    assert state.messages[-2]["content"] == "six years of data engineering"
    assert state.updated_at  # stamped on save


def test_state_file_is_0600(app_dir, fake_llm):
    onboarding.start_interview("llm")
    mode = config.ONBOARDING_STATE_PATH.stat().st_mode & 0o777
    assert mode == 0o600


def test_unreadable_state_starts_fresh(app_dir):
    config.ONBOARDING_STATE_PATH.write_text("not json{")
    assert onboarding.load_state() is None


# ---------------------------------------------------------------------------
# Profile draft extraction and normalization
# ---------------------------------------------------------------------------

def _draft_reply(**profile_overrides):
    profile = {
        "personal": {"full_name": "Test Person"},
        "work_authorization": {"legally_authorized_to_work": True,
                               "require_sponsorship": "false"},
        "compensation": {"salary_expectation": "140000"},
        "experience": {"current_title": "Data Engineer", "target_role": "Data Engineer"},
    }
    profile.update(profile_overrides)
    return ("Confirmed.\n```json\n" + json.dumps({
        "_applypilot_profile": profile,
        "_applypilot_search": {"queries": ["Data Engineer"],
                               "location": "Austin, TX", "remote": True},
    }) + "\n```")


def test_extract_accepts_wrapper_and_bare_blocks():
    assert onboarding.extract_profile_draft("text\n```json\n" + json.dumps(
        {"_applypilot_profile": {"personal": {}}, "_applypilot_search": {}}) + "\n```")
    bare = {"personal": {}, "work_authorization": {}, "compensation": {}}
    draft = onboarding.extract_profile_draft("```json\n" + json.dumps(bare) + "\n```")
    assert draft["_applypilot_profile"] == bare  # wrapper-less LLM drift accepted
    assert onboarding.extract_profile_draft("no block here") is None
    assert onboarding.extract_profile_draft("```json\n{broken\n```") is None


def test_normalize_fills_eight_keys_and_coerces():
    profile, meta = onboarding.normalize_profile(
        {"_applypilot_profile": {
            "personal": {"full_name": "Test Person", "password": "SHOULD_NOT_SURVIVE"},
            "work_authorization": {"legally_authorized_to_work": True,
                                   "require_sponsorship": "false"},
         },
         "_applypilot_search": {"queries": "Data Engineer, Analytics Engineer",
                                "location": "Austin, TX", "remote": True}})
    assert set(profile) == set(onboarding._PROFILE_KEYS)
    # Wizard field shape inside every section:
    assert set(profile["personal"]) == {
        "full_name", "preferred_name", "email", "phone", "city", "province_state",
        "country", "postal_code", "address", "linkedin_url", "github_url",
        "portfolio_url", "website_url", "password"}
    assert profile["personal"]["password"] == ""  # boundary: never from chat
    assert profile["work_authorization"]["legally_authorized_to_work"] == "Yes"
    assert profile["work_authorization"]["require_sponsorship"] == "No"
    assert profile["availability"]["earliest_start_date"] == "Immediately"
    assert profile["compensation"]["salary_currency"] == "USD"
    assert set(profile["eeo_voluntary"].values()) == {"Decline to self-identify"}
    assert meta["queries"] == ["Data Engineer", "Analytics Engineer"]  # string split
    assert meta["remote"] is True


def test_missing_review_fields_named_for_add_later(app_dir, fake_llm):
    state = onboarding.start_interview("llm")
    state.profile_draft = {**onboarding.normalize_profile(
        {"_applypilot_profile": {}, "_applypilot_search": {}})[0],
        "_applypilot_search": {}}
    missing = onboarding.missing_review_fields(state.profile_draft)
    assert "Full name" in missing and "Email address" in missing


# ---------------------------------------------------------------------------
# Write-back on confirm
# ---------------------------------------------------------------------------

def test_confirm_writes_all_three_files(app_dir, fake_claude, signed_in):
    onboarding.start_interview("claude")
    onboarding.send_message("background and preferences")
    onboarding.confirm_profile()

    profile = json.loads(config.PROFILE_PATH.read_text())
    assert set(profile) == set(onboarding._PROFILE_KEYS)
    assert profile["experience"]["target_role"] == "Data Engineer"

    md = config.PROFILE_MD_PATH.read_text()
    assert "profile.md" in md and "Test Person" in md and "140000" in md

    searches = yaml.safe_load(config.SEARCH_CONFIG_PATH.read_text())
    assert searches["defaults"]["location"] == "Austin, TX"
    assert searches["defaults"]["distance"] == 0  # remote-only
    assert searches["locations"][0]["remote"] is True
    assert [q["query"] for q in searches["queries"]] == [
        "Data Engineer", "Analytics Engineer"]
    assert [q["tier"] for q in searches["queries"]] == [1, 2]

    # profile data files stay owner-only (S2); searches.yaml follows the wizard.
    assert config.PROFILE_PATH.stat().st_mode & 0o777 == 0o600
    assert config.PROFILE_MD_PATH.stat().st_mode & 0o777 == 0o600


def test_confirm_preserves_existing_password(app_dir, fake_claude, signed_in):
    existing = {"personal": {"full_name": "Old", "password": "site-secret"},
                "work_authorization": {}}
    config.write_secret_text(config.PROFILE_PATH, json.dumps(existing))

    onboarding.start_interview("claude")
    onboarding.send_message("background and preferences")
    profile = onboarding.confirm_profile()
    # The interview never saw the password — a redo must not wipe it.
    assert profile["personal"]["password"] == "site-secret"
    on_disk = json.loads(config.PROFILE_PATH.read_text())
    assert on_disk["personal"]["password"] == "site-secret"


def test_searches_yaml_falls_back_without_remote(app_dir):
    text = onboarding.render_searches_yaml(
        {"queries": ["Data Engineer"], "location": "Austin, TX", "remote": False})
    searches = yaml.safe_load(text)
    assert searches["defaults"]["distance"] == 25
    assert searches["locations"][0]["remote"] is False


# ---------------------------------------------------------------------------
# Resume intake
# ---------------------------------------------------------------------------

def test_resume_paste_feeds_next_agent_turn(app_dir, fake_claude, signed_in,
                                            monkeypatch, tmp_path):
    prompt_log = tmp_path / "prompts.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(prompt_log))
    onboarding.start_interview("claude")
    state = onboarding.add_resume(text="SKILLS Python, SQL", filename="pasted resume")
    assert config.RESUME_PATH.exists()
    assert config.RESUME_PATH.stat().st_mode & 0o777 == 0o600
    assert any("Resume provided" in m["content"] for m in state.messages
               if m["role"] == "note")
    onboarding.send_message("that's me")
    turns = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    assert "SKILLS Python, SQL" in turns[1]["prompt"]  # injected into turn 2
    assert "that's me" in turns[1]["prompt"]


def test_resume_pdf_upload_is_saved_not_parsed(app_dir, fake_llm):
    onboarding.start_interview("llm")
    state = onboarding.add_resume(pdf_bytes=b"%PDF-1.4 fake", filename="me.pdf")
    assert config.RESUME_PDF_PATH.read_bytes().startswith(b"%PDF")
    assert config.RESUME_PDF_PATH.stat().st_mode & 0o777 == 0o600
    assert any("PDF saved" in m["content"] for m in state.messages
               if m["role"] == "note")
    assert state.pending_context == ""  # nothing text-shaped to inject


def test_resume_rejected_outside_interview(app_dir, fake_llm):
    with pytest.raises(onboarding.OnboardingError, match="not active"):
        onboarding.add_resume(text="no interview yet")


# ---------------------------------------------------------------------------
# Review phase transitions
# ---------------------------------------------------------------------------

def test_keep_editing_returns_to_interview(app_dir, fake_claude, signed_in):
    onboarding.start_interview("claude")
    onboarding.send_message("background and preferences")
    assert onboarding.load_state().phase == onboarding.REVIEW
    state = onboarding.keep_editing()
    assert state.phase == onboarding.INTERVIEW
    assert state.profile_draft  # kept for context


def test_confirm_requires_review_phase(app_dir, fake_llm):
    onboarding.start_interview("llm")
    with pytest.raises(onboarding.OnboardingError, match="no profile draft"):
        onboarding.confirm_profile()


def test_redo_clears_state_and_restarts(app_dir, fake_claude, signed_in):
    onboarding.start_interview("claude")
    onboarding.send_message("background and preferences")
    onboarding.confirm_profile()
    profile_before = config.PROFILE_PATH.read_text()

    state = onboarding.redo_interview()
    assert state.phase == onboarding.INTERVIEW
    assert state.backend == "claude"
    assert state.claude_session_id == "fake-session-1"  # fresh kickoff ran
    assert config.PROFILE_PATH.read_text() == profile_before  # profile untouched
