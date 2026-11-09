"""Protocol/portal coexistence on one store (spec art_A71wyYS4, Verification 2).

The portal and the `kestrel agent` protocol are two front doors over one
onboarding state file and one single-session lock. These tests prove the
shared-truth property in both directions and the busy refusal at both
doors:

- an interview started through the protocol is visible mid-flight to the
  reads the portal's onboarding surface composes (app._onboarding_context
  calls load_state / wizard_question / prefilled_answers /
  remaining_questions), and the two doors agree on the current question;
- an answer recorded through the portal service layer (record_answer —
  the function the portal's chat POST route calls) shows up in the
  protocol's next payload;
- while one surface holds a turn, the other gets the structured busy
  error — exit 1 with a JSON error envelope on the protocol, the same
  OnboardingError the portal routes already render — and the refused
  call changed nothing; once the turn finishes, the same verb proceeds.

Zero network, zero real keys: onboarding turns ride the scripted fake
`claude` binary (the rig from tests/test_onboarding_agent.py).
"""

import json
import stat
import threading
import time

import pytest
from typer.testing import CliRunner

from kestrel import engine_auth, onboarding
from kestrel.cli import app

from tests.test_onboarding_agent import FAKE_CLAUDE, SIGNED_IN

runner = CliRunner()

GREETING = "Hello! Tell me about your background."


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
    monkeypatch.setattr(
        engine_auth, "claude_status", lambda *a, **k: SIGNED_IN)


def run(*args):
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    return json.loads(result.stdout)


def test_protocol_started_interview_is_visible_in_the_portal_service_layer(
        app_dir, fake_claude, signed_in, tmp_path):
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.", encoding="utf-8")

    # The protocol opens intake and runs the kickoff turn.
    assert run("start").exit_code == 0
    result = run("resume", "--file", str(resume_file))
    assert result.exit_code == 0, result.stdout
    result = run("start")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["phase"] == "interview"

    # Mid-flight: the reads the portal's onboarding context composes see
    # the protocol's interview — phase, the question it would render, the
    # answer chips, and the live sequence number.
    state = onboarding.load_state()
    assert state is not None
    assert state.phase == onboarding.INTERVIEW
    assert state.mode == onboarding.MODE_CHOICES
    portal_question = onboarding.wizard_question(state)
    assert portal_question is not None
    assert portal_question.id == "full_name"
    assert onboarding.prefilled_answers(state) == []
    assert onboarding.remaining_questions(state) is not None
    assert state.seq > 0

    # The two doors agree on the current question.
    protocol_payload = payload_of(run("next"))
    assert protocol_payload["phase"] == "interview"
    assert protocol_payload["current_question"]["id"] == portal_question.id

    # The portal service layer records an answer (record_answer is the
    # function the portal's chat POST route calls) — the protocol sees it.
    result = onboarding.record_answer("full_name", free_text="Jane Q. Portal")
    assert result.answers["full_name"] == "Jane Q. Portal"
    protocol_payload = payload_of(run("next"))
    assert protocol_payload["progress"]["answered"] == 1
    assert protocol_payload["progress"]["remaining"] == 17
    # The next agenda question moved on for both doors.
    assert protocol_payload["current_question"]["id"] == "education"


def test_portal_started_interview_is_visible_to_the_protocol(
        app_dir, fake_claude, signed_in):
    # The portal opens the flow through the same service function its
    # onboarding context calls on a fresh store.
    state = onboarding.start_intake()
    assert state.phase == onboarding.INTAKE

    payload = payload_of(run("next"))
    assert payload["phase"] == "intake"
    assert payload["needs"]["resume"] is True


BLOCKING_FAKE = """#!/bin/sh
touch "$FAKE_TURN_STARTED"
deadline=$(( $(date +%s) + 20 ))
while [ ! -f "$FAKE_TURN_RELEASE" ] && [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 0.05
done
echo "{greeting}"
""".format(greeting=GREETING)


def test_second_surface_gets_structured_busy_while_a_turn_is_in_flight(
        app_dir, fake_claude, signed_in, monkeypatch, tmp_path):
    # A blocking fake: the kickoff turn hangs until the test releases it,
    # so surface 1 provably holds the single-session lock.
    bin_dir = tmp_path / "bin-blocking"
    bin_dir.mkdir()
    blocking = bin_dir / "claude"
    blocking.write_text(BLOCKING_FAKE)
    blocking.chmod(blocking.stat().st_mode | stat.S_IEXEC)
    # Re-point the backend at the blocking shim (the fixture's rig fake
    # answers instantly; surface 1 must HOLD the turn).
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(blocking))

    turn_started = tmp_path / "turn-started"
    turn_release = tmp_path / "turn-release"
    monkeypatch.setenv("FAKE_TURN_STARTED", str(turn_started))
    monkeypatch.setenv("FAKE_TURN_RELEASE", str(turn_release))
    # The busy refusal waits _STATE_TURN_TIMEOUT for the lock; shrink it
    # so the refusal is immediate and deterministic (onboarding._state_turn).
    monkeypatch.setattr(onboarding, "STATE_TURN_TIMEOUT", 0.05)

    outcome: dict = {}

    def first_surface():
        try:
            outcome["state"] = onboarding.start_interview("claude")
        except Exception as exc:  # surfaced below — never swallow it
            outcome["error"] = exc

    worker = threading.Thread(target=first_surface, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not turn_started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert turn_started.exists(), "the kickoff turn never started"

    # Surface 2, protocol door: a state-mutating verb refuses with the
    # structured busy envelope — stdout is the error document, exit 1,
    # stderr clean.
    result = run("answer", "--question", "full_name",
                 "--free-text", "Jane Q. Protocol")
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "busy"
    assert "another surface" in envelope["error"]["message"]
    assert result.stderr == ""

    # Surface 2, portal door: the service layer raises the same refusal
    # the portal routes already render through their OnboardingError catches.
    with pytest.raises(onboarding.OnboardingError,
                       match="already running on another surface"):
        onboarding.record_answer("full_name", free_text="Jane Q. Portal")

    # The refused verb changed nothing. (load_state() is still None here —
    # the first save happens when surface 1's kickoff turn finishes — so the
    # proof is the post-release retry below: it records full_name cleanly,
    # which the busy call would have prevented.)

    # Surface 1 wins: the released turn completes cleanly.
    turn_release.write_text("go")
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    state = outcome["state"]
    assert state.phase == onboarding.INTERVIEW
    assert any(GREETING in m["content"]
               for m in state.messages if m["role"] == "assistant")

    # Once the other turn finished, the same protocol verb proceeds — the
    # refusal is a retry-safe wait, not a failure.
    result = run("answer", "--question", "full_name",
                 "--free-text", "Jane Q. Protocol")
    assert result.exit_code == 0, result.stdout
    assert onboarding.load_state().answers["full_name"] == "Jane Q. Protocol"
