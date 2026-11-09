"""Contract tests for the `kestrel agent` JSON protocol (spec art_A71wyYS4).

The wire contract: every verb prints ONE pure-JSON document on stdout
alone — the authoritative next payload, or the
{"error": {"code", "message", "details"}} envelope with a nonzero exit —
and nothing contractual ever lands on stderr. The journey drives the
fake `claude` binary (the same rig as tests/test_onboarding_agent.py)
through real phases: start → next → resume → start → send → answer ×N
→ review → confirm.
"""

import json
import stat

import pytest
from typer.testing import CliRunner

from kestrel import engine_auth, onboarding
from kestrel.cli import app

runner = CliRunner()

# The same EngineStatus fakes the onboarding tests use — readiness() reads
# `.state` off a real EngineStatus, not a bare string.
SIGNED_IN_STATUS = engine_auth.EngineStatus("claude", engine_auth.SIGNED_IN, "claude.ai", "/usr/bin/claude")
SIGNED_OUT_STATUS = engine_auth.EngineStatus("claude", engine_auth.SIGNED_OUT, None, "/usr/bin/claude")

# Distinctive strings the default payloads must never leak: one in the
# resume document, one in the draft's resume_facts section (which rides
# the agent's fenced block on the extraction turn).
RESUME_MARKER = "QUINTESSENTIAL_RESUME_MARKER_9F3A"
FACTS_MARKER = "GHOST_PROJECT_MARKER_7C2B"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Scripted `claude -p` fake for the protocol journey (print-mode contract)."""
import json
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


def envelope(result, is_error=False):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                      "result": result, "session_id": "fake-agent-session"}))


PROFILE_BLOCK = """Here is your profile.

```json
{"_kestrel_profile": {
    "personal": {"full_name": "Test Person", "email": "test@example.com",
                 "phone": "555-0100", "city": "Austin, TX"},
    "work_authorization": {"legally_authorized_to_work": "Yes",
                           "require_sponsorship": "No"},
    "availability": {"earliest_start_date": "Immediately"},
    "compensation": {"salary_expectation": "140000", "salary_currency": "USD"},
    "experience": {"current_title": "Data Engineer", "years_of_experience_total": "6",
                   "target_role": "Data Engineer", "education_level": "Bachelor's"},
    "skills_boundary": {"programming_languages": "Python, SQL",
                        "frameworks": "dbt", "tools": "Airflow"},
    "resume_facts": {"preserved_companies": ["Acme Corp"],
                     "preserved_projects": ["Built the GHOST_PROJECT_MARKER_7C2B data platform"]},
    "eeo_voluntary": {}},
 "_kestrel_search": {"queries": ["Data Engineer", "Analytics Engineer"],
                        "location": "Austin, TX", "remote": true}}
```
"""

if "opened onboarding" in prompt:
    envelope("Hello! Tell me about your background.")
elif resume == "fake-agent-session":
    envelope("Thanks! " + PROFILE_BLOCK)
else:
    sys.stderr.write("user turn without --resume\n")
    sys.exit(2)
'''


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Install the scripted fake on a temp PATH (the test_onboarding_agent rig)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(fake))
    return fake


@pytest.fixture()
def signed_in(monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: SIGNED_IN_STATUS)


@pytest.fixture()
def no_backend(monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status", lambda *a, **k: SIGNED_OUT_STATUS)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)


def run(*args):
    """Invoke `kestrel agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    """The one JSON document off stdout — the contract under test."""
    return json.loads(result.stdout)


def _start_interview_with_resume(tmp_path, resume_text):
    """start → resume → start (kickoff) → send (draft extraction)."""
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text(resume_text, encoding="utf-8")
    result = run("start")
    assert result.exit_code == 0, result.stdout
    result = run("resume", "--file", str(resume_file))
    assert result.exit_code == 0, result.stdout
    result = run("start")
    assert result.exit_code == 0, result.stdout
    result = run("send", "--text", "Data engineer in Austin, $140k minimum, remote ok")
    assert result.exit_code == 0, result.stdout


def _drain_agenda():
    """Walk the remaining agenda using only what the next payload shows."""
    for _ in range(len(onboarding.QUESTION_SPECS) + 1):
        question = payload_of(run("next"))["current_question"]
        if question is None:
            return
        if question["choices"]:
            argv = ("answer", "--question", question["id"],
                    "--choice", question["choices"][0]["value"])
        elif question["allow_free_text"]:
            argv = ("answer", "--question", question["id"],
                    "--free-text", "Something reasonable")
        else:
            argv = ("answer", "--question", question["id"], "--skip")
        result = run(*argv)
        assert result.exit_code == 0, result.stdout
    pytest.fail("the agenda never completed")


# ---------------------------------------------------------------------------
# The journey (acceptance 3): real phases, payload-driven, end to end
# ---------------------------------------------------------------------------

def test_protocol_journey_start_to_confirm(app_dir, fake_claude, signed_in, tmp_path):
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.", encoding="utf-8")

    # start on a fresh store opens intake with no agent turn; next is a pure read.
    result = run("start")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "intake"
    assert payload["needs"] == {"resume": True, "confirm": False, "provider_setup": False}
    assert payload["resume"] == {"provided": False, "chars": None, "pending": False}
    assert payload_of(run("next"))["phase"] == "intake"

    # resume --file queues the document; needs.resume flips off.
    result = run("resume", "--file", str(resume_file))
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "intake"
    assert payload["resume"]["provided"] is True
    assert payload["resume"]["pending"] is True
    assert payload["needs"]["resume"] is False

    # start again runs the kickoff turn → interview.
    result = run("start")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "interview"
    assert payload["mode"] == "choices"
    assert payload["last_agent_reply"] == "Hello! Tell me about your background."
    assert payload["current_question"]["id"] == "full_name"

    # send: turn 2 extracts the draft; the agenda is now gaps-only.
    result = run("send", "--text", "Data engineer in Austin, $140k minimum, remote ok")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "interview"
    chips = {chip["question_id"] for chip in payload["prefill_chips"]}
    assert {"full_name", "email", "salary_band"} <= chips
    assert payload["current_question"]["id"] not in chips
    assert payload["current_question"]["id"] == "experience_years"

    # answer ×N: every question answered from the payload's own choices.
    _drain_agenda()

    # review: the review form rides the payload; edits flow through review --set.
    payload = payload_of(run("next"))
    assert payload["phase"] == "review"
    assert payload["needs"]["confirm"] is True
    assert {f["id"] for f in payload["review_fields"]} == {s.id for s in onboarding.QUESTION_SPECS}

    result = run("review", "--set", "phone=555-0199", "--set", "email=")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    fields = {f["id"]: f for f in payload["review_fields"]}
    assert fields["phone"]["value"] == "555-0199"
    assert fields["email"]["value"] == ""
    assert fields["email"]["needs_answer"] is True
    assert "Email address" in payload["missing_review_fields"]

    # confirm: the only path out of REVIEW; the files land and the gate lifts.
    result = run("confirm")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "done"
    assert payload["written"]["profile"].endswith("profile.json")
    assert (app_dir / "profile.json").exists()

    # start on a finished interview resumes as-is, never wipes.
    result = run("start")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["phase"] == "done"


# ---------------------------------------------------------------------------
# Redaction (acceptance 2)
# ---------------------------------------------------------------------------

def test_redaction_defaults_hide_resume_text_and_facts(app_dir, fake_claude, signed_in, tmp_path):
    _start_interview_with_resume(tmp_path, f"Confidential: {RESUME_MARKER} — senior data engineer.")

    default_next = payload_of(run("next"))
    blob = json.dumps(default_next)
    assert RESUME_MARKER not in blob       # the resume document
    assert FACTS_MARKER not in blob        # resume_facts (rides the fenced draft)
    assert "resume_facts" not in default_next
    assert "text" not in default_next["resume"]
    assert default_next["resume"]["provided"] is True
    # The structured answers the wizard needs still travel.
    assert any(chip["question_id"] == "email" for chip in default_next["prefill_chips"])

    _drain_agenda()
    default_review = payload_of(run("next"))
    assert default_review["phase"] == "review"
    blob = json.dumps(default_review)
    assert RESUME_MARKER not in blob
    assert FACTS_MARKER not in blob
    assert "resume_facts" not in default_review
    assert "text" not in default_review["resume"]
    # Prose (no fence) still reaches the host agent by default.
    assert default_review["last_agent_reply"].startswith("Thanks!")


def test_redaction_reveal_opts_in_per_call(app_dir, fake_claude, signed_in, tmp_path):
    _start_interview_with_resume(tmp_path, f"Confidential: {RESUME_MARKER} — senior data engineer.")

    revealed = payload_of(run("next", "--reveal"))
    assert RESUME_MARKER in revealed["resume"]["text"]
    assert FACTS_MARKER in revealed["last_agent_reply"]  # the raw reply, fence intact

    _drain_agenda()
    revealed_review = payload_of(run("review", "--set", "phone=555-0199", "--reveal"))
    assert revealed_review["phase"] == "review"
    assert revealed_review["resume_facts"]["preserved_projects"] == [
        f"Built the {FACTS_MARKER} data platform"]
    assert RESUME_MARKER in revealed_review["resume"]["text"]

    # Redaction is per call: the next default call is redacted again.
    assert RESUME_MARKER not in json.dumps(payload_of(run("next")))


# ---------------------------------------------------------------------------
# The JSON contract, verb by verb (acceptance 1)
# ---------------------------------------------------------------------------

def test_gate_payload_when_no_state_exists(app_dir, no_backend):
    result = run("next")
    assert result.exit_code == 0
    payload = payload_of(result)
    assert payload["phase"] == "gate"
    assert payload["mode"] is None
    assert payload["current_question"] is None
    assert payload["last_agent_reply"] is None
    assert payload["prefill_chips"] == []
    assert payload["progress"] == {"answered": 0, "remaining": 0}
    assert payload["needs"]["provider_setup"] is True


def test_contract_error_paths_emit_envelope_nonzero(app_dir, no_backend):
    """Each failure: the envelope on stdout, exit nonzero, stderr clean."""
    cases = [
        ["start", "--mode", "bogus"],
        ["send"],                                          # no --text
        ["send", "--text", "hello"],                       # no interview
        ["answer", "--question", "full_name"],             # no selector
        ["answer", "--choice", "Yes"],                     # no --question
        ["answer", "--question", "full_name", "--choice", "Yes", "--skip"],
        ["resume"],                                        # no --file
        ["resume", "--file", "/nonexistent/resume.txt"],
        ["review"],                                        # no --set
        ["review", "--set", "email"],                      # no '='
        ["confirm"],
        ["keep-editing"],
        ["redo"],                                          # no backend
        ["agent-led", "sideways"],
        ["agent-led"],                                     # no action
    ]
    for argv in cases:
        result = run(*argv)
        assert result.exit_code != 0, argv
        envelope = payload_of(result)
        assert "error" in envelope, argv
        assert envelope["error"]["code"], argv
        assert envelope["error"]["message"], argv
        assert result.stderr == "", argv


def test_error_codes_are_semantic(app_dir, no_backend):
    cases = {
        ("start", "--mode", "bogus"): "invalid_mode",
        ("send", "--text", "hello"): "not_active",
        ("answer", "--question", "x", "--choice", "a", "--free-text", "b"): "invalid_answer",
        ("resume", "--file", "/nonexistent/resume.txt"): "file_not_found",
        ("confirm",): "nothing_to_confirm",
        ("keep-editing",): "nothing_to_edit",
        ("redo",): "provider_setup",
        ("agent-led", "sideways"): "invalid_action",
        ("review", "--set", "no_such_field=1"): "unknown_field",
    }
    for argv, code in cases.items():
        result = run(*argv)
        assert result.exit_code != 0, argv
        payload = payload_of(result)
        assert payload["error"]["code"] == code, (argv, payload)
        assert result.stderr == "", argv


def test_start_without_backend_starts_host_mode(app_dir, no_backend):
    """agent start no longer demands a backend (spec art_v7WOlPfz): intake
    opens, and the second start runs the interview on the host backend —
    needs.provider_setup stays true but is informational."""
    payload = payload_of(run("start"))
    assert payload["phase"] == "intake"
    assert payload["backend"] is None or payload["backend"] == ""
    result = run("start")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "interview"
    assert payload["backend"] == "host"
    assert payload["needs"]["provider_setup"] is True


# ---------------------------------------------------------------------------
# Host backend (spec art_v7WOlPfz): the driving agent IS the intelligence
# ---------------------------------------------------------------------------

@pytest.fixture()
def exploding_backends(monkeypatch):
    """Record-and-raise on every backend seam: any LLM or Claude call in
    host mode fails the test loudly instead of passing by accident."""
    calls: list[str] = []

    def make(name):
        class _Boom:
            def __init__(self, *a, **k):
                pass

            def kickoff(self, *a, **k):
                calls.append(f"{name}.kickoff")
                raise AssertionError(f"{name}.kickoff ran in host mode")

            def send(self, *a, **k):
                calls.append(f"{name}.send")
                raise AssertionError(f"{name}.send ran in host mode")

        return _Boom

    monkeypatch.setattr(onboarding, "LLMBackend", make("LLMBackend"))
    monkeypatch.setattr(onboarding, "ClaudePrintBackend", make("ClaudePrintBackend"))
    return calls


def _drain_agenda():
    """Walk the whole agenda using only what the next payload shows."""
    for _ in range(len(onboarding.QUESTION_SPECS) + 1):
        question = payload_of(run("next"))["current_question"]
        if question is None:
            return
        if question["choices"]:
            argv = ("answer", "--question", question["id"],
                    "--choice", question["choices"][0]["value"])
        elif question["allow_free_text"]:
            argv = ("answer", "--question", question["id"],
                    "--free-text", "Something reasonable")
        else:
            argv = ("answer", "--question", question["id"], "--skip")
        result = run(*argv)
        assert result.exit_code == 0, result.stdout
    pytest.fail("the agenda never completed")


def test_host_journey_runs_zero_backend_calls(app_dir, no_backend, exploding_backends, tmp_path):
    """The zero-backend journey: start → resume → start → all guided answers
    → confirm, with both backend classes patched to raise-and-record — the
    journey completes (phase done, the three files written) and the recorded
    backend call count is 0."""
    from kestrel import config

    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.", encoding="utf-8")
    assert payload_of(run("start"))["phase"] == "intake"
    assert payload_of(run("resume", "--file", str(resume_file)))["phase"] == "intake"
    payload = payload_of(run("start"))
    assert payload["backend"] == "host"

    _drain_agenda()
    assert payload_of(run("next"))["phase"] == "review"
    result = run("confirm")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "done"
    assert payload["written"]["profile"] == str(config.PROFILE_PATH)
    assert config.PROFILE_PATH.exists()
    assert config.PROFILE_MD_PATH.exists()
    assert config.SEARCH_CONFIG_PATH.exists()
    assert exploding_backends == []


def test_host_answers_get_local_notes_not_backend_acks(app_dir, no_backend, exploding_backends, tmp_path):
    """With backend host, an answer records and the transcript gains a local
    note — no ack turn, no exception swallowed."""
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.", encoding="utf-8")
    run("start")
    run("resume", "--file", str(resume_file))
    run("start")
    question = payload_of(run("next"))["current_question"]
    if question["choices"]:
        result = run("answer", "--question", question["id"],
                     "--choice", question["choices"][0]["value"])
    else:
        result = run("answer", "--question", question["id"],
                     "--free-text", "Something reasonable")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["backend"] == "host"
    state = onboarding.load_state()
    notes = [m for m in state.messages if m["role"] == onboarding._ROLE_NOTE]
    assert any("host-driven, no backend ack" in m["content"] for m in notes)
    assert exploding_backends == []


def test_send_in_host_mode_is_gated(app_dir, no_backend, exploding_backends):
    """Free-form chat stays backend-only in host mode: provider_setup code,
    guidance pointing at the answer/resume/review driving verbs."""
    run("start")
    run("start")
    result = run("send", "--text", "hello there")
    assert result.exit_code != 0
    payload = payload_of(result)
    assert payload["error"]["code"] == "provider_setup"
    assert "agent answer" in payload["error"]["message"]
    assert "resume" in payload["error"]["message"]
    assert "review" in payload["error"]["message"]


def test_agent_led_on_in_host_mode_is_gated(app_dir, no_backend, exploding_backends):
    """Agent-led mode is free-form chat's channel — gated in host mode like
    send; agent-led off stays available (a pure local switch back)."""
    run("start")
    run("start")
    result = run("agent-led", "on")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "provider_setup"
    assert payload_of(run("agent-led", "off"))["mode"] == "choices"


def test_redo_in_host_mode_is_a_pure_local_reset(app_dir, no_backend, exploding_backends):
    """Start-over in host mode needs no backend: it clears and reopens on
    host; with no state (the portal path) the backend gate stands."""
    run("start")
    run("start")
    result = run("redo")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "interview"
    assert payload["backend"] == "host"
    assert exploding_backends == []


def test_start_never_wipes_an_active_interview(app_dir, fake_claude, signed_in, tmp_path):
    _start_interview_with_resume(tmp_path, "Test Person — data engineer résumé.")
    before = payload_of(run("next"))
    result = run("start")
    assert result.exit_code == 0, result.stdout
    after = payload_of(result)
    assert after["phase"] == "interview"
    assert after["last_agent_reply"] == before["last_agent_reply"]
    assert len(after["prefill_chips"]) == len(before["prefill_chips"])


def test_start_mode_agent_hands_the_agenda_to_the_agent(app_dir, fake_claude, signed_in, tmp_path):
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.", encoding="utf-8")
    run("start")
    run("resume", "--file", str(resume_file))
    payload = payload_of(run("start", "--mode", "agent"))
    assert payload["phase"] == "interview"
    assert payload["mode"] == "agent"


def test_agent_led_switches_both_ways(app_dir, fake_claude, signed_in, tmp_path):
    _start_interview_with_resume(tmp_path, "Test Person — data engineer résumé.")
    payload = payload_of(run("agent-led", "on"))
    assert payload["mode"] == "agent"
    payload = payload_of(run("agent-led", "off"))
    assert payload["mode"] == "choices"


def test_answer_records_choices_and_progress(app_dir, fake_claude, signed_in, tmp_path):
    _start_interview_with_resume(tmp_path, "Test Person — data engineer résumé.")
    payload = payload_of(run("next"))
    before = payload["progress"]

    # A listed choice records and advances the agenda.
    question = payload["current_question"]
    choice = question["choices"][0]["value"]
    result = run("answer", "--question", question["id"], "--choice", choice)
    assert result.exit_code == 0, result.stdout
    after = payload_of(result)["progress"]
    assert after["answered"] == before["answered"] + 1
    assert after["remaining"] == before["remaining"] - 1

    # A bogus choice is rejected with its own code.
    question = payload_of(run("next"))["current_question"]
    result = run("answer", "--question", question["id"], "--choice", "not-a-listed-choice")
    assert result.exit_code != 0
    payload = payload_of(result)
    assert payload["error"]["code"] == "invalid_choice"

    # A skip records intentionally empty and still advances the agenda.
    question = payload_of(run("next"))["current_question"]
    result = run("answer", "--question", question["id"], "--skip")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["progress"]["answered"] == after["answered"] + 1
