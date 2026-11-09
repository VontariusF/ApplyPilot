"""Regression tests for the deferred round-3 protocol suggestions (findings
doc art_YLnPMn0i, task todo_NZUlyEKv — the follow-up wave after PRs #63/#64).

Each section maps to a finding:

- F8: apply lock ownership — apply-acquire stamps a one-time `claim:<uuid>`
  token into apply_task_id and returns it; apply-result must echo it as
  --claim. Wrong/missing claim → not_claim_owner, no state change. Locks
  without a token (old stores, engine paths, manual marks) keep the exact
  pre-claim behavior.
- F12: payload-shape drift — apply-result's next_eligible shares _job_block
  with apply-acquire (one job shape across the stage family) and is
  cap-aware: when the daily cap binds, it never advertises a job the next
  apply-acquire would refuse with cap_reached — it says cap_reached: true.
- F13: the tailor/cover write verbs enforce the attempts-exhaustion cap
  their queues enforce (stage_exhausted envelope) instead of writing a job
  the queue considers dead.
- F14: validation-order alignment — cover checks the job first, then the
  letter, so the same bad-URL mistake yields the same envelope code on both
  write verbs.
- F15: `agent start --backend host` forces the zero-LLM journey on a store
  with providers configured; ambient selection stays the default.
- F11 is a documentation-only resolution (the wire-contract docstring names
  the redaction line) — no behavior to test here.
"""

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from kestrel import config, database, engine_auth, onboarding
from kestrel.apply import launcher
from kestrel.apply.launcher import mark_result, new_claim_token
from kestrel.cli import app
from tests.test_agent_materials import (
    DESCRIPTION,
    GOOD_COVER,
    GOOD_TAILOR_DATA,
    PROFILE,
)
from tests.test_agent_protocol import (
    FAKE_CLAUDE as _FAKE_CLAUDE,
    SIGNED_IN_STATUS as _SIGNED_IN_STATUS,
    SIGNED_OUT_STATUS as _SIGNED_OUT_STATUS,
)

runner = CliRunner()


# The protocol suite's fixtures, local to this file (imported fixture
# functions collide with same-named test parameters under ruff's F811).

@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Install the scripted fake on a temp PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(_FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: str(fake))
    return fake


@pytest.fixture()
def signed_in(monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status",
                        lambda *a, **k: _SIGNED_IN_STATUS)


@pytest.fixture()
def no_backend(monkeypatch):
    monkeypatch.setattr(engine_auth, "claude_status",
                        lambda *a, **k: _SIGNED_OUT_STATUS)
    monkeypatch.setattr(onboarding, "provider_ready", lambda: False)


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


def run(*args):
    """Invoke `kestrel agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    return json.loads(result.stdout)


def _subprocess_env(app_dir):
    return {**os.environ, "KESTREL_DIR": str(app_dir)}


def _spawn_cli(app_dir, *args):
    return subprocess.Popen(
        [sys.executable, "-m", "kestrel.cli", "agent", *args],
        env=_subprocess_env(app_dir), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)


def _row(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


def _seed_apply_job(conn, url, fit_score=9):
    """A queue-eligible tailored job (the apply queue's minimum)."""
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, "
        "tailored_resume_path, cover_letter_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (url, "Engineer", "acme", "d" * 300, fit_score,
         "/tmp/tailored/x.txt", "/tmp/covers/x.txt"))
    conn.commit()


@pytest.fixture()
def store(app_dir, isolated_db, monkeypatch):
    """Profile + document dirs wired to app_dir, and an initialized db —
    the shared ground for the materials and apply probes below."""
    config.write_secret_text(config.PROFILE_PATH, json.dumps(PROFILE))
    import kestrel.scoring.cover_letter as cover_letter
    import kestrel.scoring.tailor as tailor

    monkeypatch.setattr(tailor, "TAILORED_DIR", config.TAILORED_DIR)
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", config.COVER_LETTER_DIR)
    return database.init_db()


# ---------------------------------------------------------------------------
# F8 — apply lock ownership: the one-time claim token
# ---------------------------------------------------------------------------

def test_apply_result_requires_the_locks_claim(store):
    """The P1d probe, remediated: any loop could once report any in-progress
    job. Now a wrong or missing claim is refused before any write, and the
    lock holder's own token records the outcome."""
    conn = store
    _seed_apply_job(conn, "https://jobs/1", fit_score=9)
    acquired = payload_of(run("apply-acquire"))
    claim = acquired["claim"]
    # Distinguishable one-time token: claim:-prefixed UUID.
    assert claim.startswith("claim:") and len(claim) > len("claim:")
    assert _row(conn, "https://jobs/1")["apply_task_id"] == claim
    url = acquired["job"]["url"]

    for bad_claim in (None, "claim:00000000-0000-0000-0000-000000000000",
                      "banana"):
        argv = ["apply-result", "--url", url, "--status", "applied"]
        if bad_claim is not None:
            argv += ["--claim", bad_claim]
        result = run(*argv)
        assert result.exit_code != 0, result.stdout
        envelope = payload_of(result)
        assert envelope["error"]["code"] == "not_claim_owner", envelope
        # The refusal wrote nothing: the lock and its token stand.
        row = _row(conn, url)
        assert row["apply_status"] == "in_progress"
        assert row["apply_task_id"] == claim
        assert row["applied_at"] is None

    # The holder's own claim records the outcome — and the token is
    # one-time: cleared by the recording write.
    result = run("apply-result", "--url", url, "--status", "applied",
                 "--claim", claim)
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["recorded"]["outcome"] == "recorded"
    row = _row(conn, url)
    assert row["apply_status"] == "applied"
    assert row["apply_task_id"] is None


@pytest.mark.parametrize("marker", [None, "task-engine-123", "reclaimed",
                                    "manual-applied"])
def test_tokenless_locks_report_as_before(store, marker):
    """Backward compat (audit F8 note): apply_task_id values that are not
    claim tokens — old stores, engine task ids, reclaim and manual markers —
    keep the pre-claim behavior: no ownership check, reportable as before."""
    conn = store
    url = f"https://jobs/{marker or 'null'}"
    _seed_apply_job(conn, url)
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress', agent_id = ?, "
        "apply_task_id = ? WHERE url = ?",
        (f"claude-worker-{marker}", marker, url))
    conn.commit()

    result = run("apply-result", "--url", url, "--status", "applied")
    assert result.exit_code == 0, result.stdout


def test_claim_splits_two_loops_across_processes(app_dir):
    """The doc's two-process probe: loop A holds the lock, loop B's
    apply-result with a wrong claim is refused, A's own claim records its
    outcome — real processes, not threads (the coexistence suite's gap)."""
    conn = database.init_db()
    _seed_apply_job(conn, "https://jobs/1", fit_score=9)

    # Loop A acquires in its own process.
    proc = _spawn_cli(app_dir, "apply-acquire")
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, out + err
    claim = json.loads(out)["claim"]

    # Loop B, a different process, reports with a stolen guess.
    b = _spawn_cli(app_dir, "apply-result", "--url", "https://jobs/1",
                   "--status", "applied", "--claim", "claim:deadbeef")
    out_b, err_b = b.communicate(timeout=120)
    assert b.returncode == 1, out_b + err_b
    assert json.loads(out_b)["error"]["code"] == "not_claim_owner"
    assert _row(conn, "https://jobs/1")["apply_status"] == "in_progress"

    # A's own claim records its outcome.
    a2 = _spawn_cli(app_dir, "apply-result", "--url", "https://jobs/1",
                    "--status", "applied", "--claim", claim)
    out_a, err_a = a2.communicate(timeout=120)
    assert a2.returncode == 0, out_a + err_a
    assert json.loads(out_a)["recorded"]["outcome"] == "recorded"
    assert _row(conn, "https://jobs/1")["apply_status"] == "applied"


def test_displaced_holders_report_cannot_overwrite_a_reclaim(store, monkeypatch):
    """V2 (audit art_G3Mz5okZ): mark_result's F8 ownership check is
    read-time only; the recording UPDATE is now claim-conditional. A
    stale-lock reclaim that lands between the check and the write wins —
    the displaced holder's report becomes a superseded no-op instead of
    clobbering the reclaimer's row."""
    conn = store
    url = "https://jobs/1"
    _seed_apply_job(conn, url)
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress', agent_id = 'claude', "
        "apply_task_id = ? WHERE url = ?",
        (new_claim_token(), url))
    conn.commit()
    claim = _row(conn, url)["apply_task_id"]

    reclaimer_token = new_claim_token()
    state = {"reclaimed": False}

    class _ReclaimBetweenCheckAndWrite:
        """Fault injection (the audit's suggested shape): the reclaimer's
        token lands the moment mark_result issues its recording UPDATE —
        the exact window between the read-time check and the write."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if (not state["reclaimed"]
                    and sql.lstrip().upper().startswith("UPDATE")):
                state["reclaimed"] = True
                self._inner.execute(
                    "UPDATE jobs SET apply_task_id = ? WHERE url = ?",
                    (reclaimer_token, url))
                # A real reclaim runs on its own connection; commit so the
                # displaced holder's rollback cannot undo it either.
                self._inner.commit()
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(launcher, "get_connection",
                        lambda: _ReclaimBetweenCheckAndWrite(conn))

    for status in ("applied", "failed"):
        state["reclaimed"] = False
        # Reset the row between runs: the reclaim token stands.
        conn.execute(
            "UPDATE jobs SET apply_status = 'in_progress', "
            "apply_task_id = ? WHERE url = ?", (claim, url))
        conn.commit()

        outcome = mark_result(url, status, error="boom", claim=claim,
                              task_id="task-1")
        assert outcome == "superseded", status

        # The displaced report wrote nothing: the reclaimer's token stands.
        row = _row(conn, url)
        assert row["apply_task_id"] == reclaimer_token, status
        assert row["apply_status"] == "in_progress", status
        assert row["applied_at"] is None, status


# ---------------------------------------------------------------------------
# F12 — one job shape, cap-aware lookahead
# ---------------------------------------------------------------------------

def test_next_eligible_shares_one_job_shape(store):
    """apply-acquire and apply-result now share _job_block: the apply stage
    family speaks one job shape (the stage payloads' shape)."""
    conn = store
    _seed_apply_job(conn, "https://jobs/1", fit_score=9)
    _seed_apply_job(conn, "https://jobs/2", fit_score=8)
    acquired = payload_of(run("apply-acquire"))

    payload = payload_of(run("apply-result", "--url", acquired["job"]["url"],
                             "--status", "applied", "--claim",
                             acquired["claim"]))
    entries = payload["next_eligible"]
    assert len(entries) == 1
    assert set(entries[0]) == set(acquired["job"])
    assert entries[0]["url"] == "https://jobs/2"
    assert payload["cap_reached"] is False


def test_next_eligible_is_cap_aware(store):
    """The lookahead applies the same daily-cap headroom check the next
    apply-acquire runs: when the cap binds, it advertises nothing and says
    so (the termination signals can no longer contradict each other)."""
    conn = store
    from kestrel.apply.planning import AGENT_DEFAULT_DAILY_CAP

    now = datetime.now(timezone.utc).isoformat()
    # AGENT_DEFAULT_DAILY_CAP - 1 applications recorded today, two jobs
    # still in the queue: after recording one more applied outcome the cap
    # binds while the queue is non-empty — exactly the case the old
    # lookahead lied about.
    for i in range(AGENT_DEFAULT_DAILY_CAP - 1):
        _seed_apply_job(conn, f"https://jobs/seen/{i}", fit_score=8)
        conn.execute("UPDATE jobs SET applied_at = ? WHERE url = ?",
                     (now, f"https://jobs/seen/{i}"))
    # The two live queue jobs: one taken and recorded below (fit 9), one
    # still waiting (fit 8) — the queue is non-empty when the cap binds.
    _seed_apply_job(conn, "https://jobs/last", fit_score=9)
    _seed_apply_job(conn, "https://jobs/waiting", fit_score=8)
    conn.commit()

    acquired = payload_of(run("apply-acquire"))  # fit 9 first
    assert acquired["job"]["url"] == "https://jobs/last"
    payload = payload_of(run("apply-result", "--url", "https://jobs/last",
                             "--status", "applied", "--claim",
                             acquired["claim"]))
    assert payload["apply"]["headroom"] == 0
    assert payload["cap_reached"] is True
    assert payload["next_eligible"] == []


# ---------------------------------------------------------------------------
# F13 — the write verbs honor the attempts-exhaustion cap
# ---------------------------------------------------------------------------

def test_tailor_refuses_an_attempts_exhausted_job(store):
    conn = store
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, "
        "tailor_attempts) VALUES (?, ?, ?, ?, ?, ?)",
        ("https://x/t1", "Engineer", "acme", DESCRIPTION, 8, 5))
    conn.commit()

    result = run("tailor", "--url", "https://x/t1", "--data", GOOD_TAILOR_DATA)
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "stage_exhausted", envelope
    details = envelope["error"]["details"]
    assert details["stage"] == "tailor"
    assert details["attempts"] == 5

    # Nothing was written, and the queue predicate agrees: the job is
    # invisible to job-next too — one truth, both directions.
    assert _row(conn, "https://x/t1")["tailored_resume_path"] is None
    assert _row(conn, "https://x/t1")["tailor_attempts"] == 5
    assert payload_of(run("job-next", "--stage", "tailor"))["job"] is None


def test_tailor_under_the_cap_still_writes(store):
    """Boundary: 4 attempts is one left — the cap check is not off by one."""
    conn = store
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, "
        "tailor_attempts) VALUES (?, ?, ?, ?, ?, ?)",
        ("https://x/t2", "Engineer", "acme", DESCRIPTION, 8, 4))
    conn.commit()

    result = run("tailor", "--url", "https://x/t2", "--data", GOOD_TAILOR_DATA)
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["written"]["path"]
    assert _row(conn, "https://x/t2")["tailored_resume_path"]


def test_cover_refuses_an_attempts_exhausted_job(store):
    conn = store
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, "
        "tailored_resume_path, cover_attempts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("https://x/c1", "Engineer", "acme", DESCRIPTION, 8,
         "/tmp/tailored/c1.txt", 5))
    conn.commit()

    result = run("cover", "--url", "https://x/c1", "--text", GOOD_COVER)
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "stage_exhausted", envelope
    details = envelope["error"]["details"]
    assert details["stage"] == "cover"
    assert details["attempts"] == 5
    assert _row(conn, "https://x/c1")["cover_letter_path"] is None


# ---------------------------------------------------------------------------
# F14 — validation-order alignment: job first, then the materials
# ---------------------------------------------------------------------------

def test_same_bad_url_mistake_yields_the_same_code_on_both_verbs(store):
    """A driver pointing either verb at a URL the store does not have now
    gets unknown_job from both — the repair loop sees one code class
    instead of tailor's and cover's disagreeing."""
    for argv in (
        ["tailor", "--url", "https://x/none", "--data", GOOD_TAILOR_DATA],
        ["cover", "--url", "https://x/none", "--text", GOOD_COVER],
    ):
        result = run(*argv)
        assert result.exit_code == 1, result.stdout
        assert payload_of(result)["error"]["code"] == "unknown_job"


def test_same_bad_url_plus_malformed_data_yields_the_same_code_on_both_verbs(store):
    """V1 (audit art_G3Mz5okZ): the alignment holds even when the materials
    are malformed too. Tailor used to parse --data before the job load and
    answered invalid_resume_data for an unknown-URL + malformed-data
    mistake where cover answered unknown_job — the job check now comes
    first on both verbs, so the repair loop re-anchors on the URL."""
    for argv in (
        ["tailor", "--url", "https://x/none", "--data", "{not json"],
        ["tailor", "--url", "https://x/none", "--data", "null"],
        ["cover", "--url", "https://x/none", "--text", "{not json"],
    ):
        result = run(*argv)
        assert result.exit_code == 1, result.stdout
        assert payload_of(result)["error"]["code"] == "unknown_job"


# ---------------------------------------------------------------------------
# F15 — agent start --backend host: the deterministic zero-LLM journey
# ---------------------------------------------------------------------------

def test_start_backend_host_forces_zero_llm(app_dir, fake_claude, signed_in,
                                            exploding_backends, tmp_path):
    """On a store with a provider configured, --backend host starts the
    interview on the host backend and sends nothing to any provider; the
    ambient default would have chosen claude and run a real kickoff."""
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.",
                           encoding="utf-8")

    result = run("start", "--backend", "host")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["phase"] == "intake"

    result = run("resume", "--file", str(resume_file))
    assert result.exit_code == 0, result.stdout

    # The INTAKE → INTERVIEW transition is where the backend is chosen.
    result = run("start", "--backend", "host")
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["phase"] == "interview"
    assert payload["backend"] == "host"
    assert exploding_backends == []  # zero provider calls, end to end


def test_start_ambient_backend_still_prefers_a_provider(
        app_dir, fake_claude, signed_in, tmp_path):
    """The control: without the flag, the same configured store picks the
    ambient backend (claude) — the flag is what suppressed the kickoff."""
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Test Person — data engineer résumé.",
                           encoding="utf-8")
    assert run("start").exit_code == 0
    assert run("resume", "--file", str(resume_file)).exit_code == 0
    result = run("start")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["backend"] == "claude"


def test_start_backend_flag_is_validated(app_dir, no_backend):
    result = run("start", "--backend", "claude")
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_backend", envelope
    assert "host" in envelope["error"]["message"]
