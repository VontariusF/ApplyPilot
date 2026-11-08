"""Contract tests for the host-driven apply bookkeeping verbs (spec
art_IMViYTMU, T3).

`agent apply-acquire` and `agent apply-result` are bookkeeping ONLY: the
launcher's per-job lock and the outcome record. The driving agent performs
the real browser work itself with its own tooling — the guardrail
assertion is the exploding-seams journey (the PR #58 pattern): every
engine/browser/network/LLM seam is patched to raise-and-record, and the
full acquire → result loop must run to the cap with zero recorded calls
and no plan token staged or consumed.
"""

import json
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from applypilot import config, database, onboarding
from applypilot.cli import app

runner = CliRunner()

RESUME_MARKER = "QUINTESSENTIAL_RESUME_MARKER_9F3A"
FACTS_MARKER = "GHOST_PROJECT_MARKER_7C2B"

# The external-driver daily cap (planning.AGENT_DEFAULT_DAILY_CAP) — the
# guardrail the verbs may not lift.
CAP = 5


def run(*args):
    """Invoke `applypilot agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    """The one JSON document off stdout — the contract under test."""
    return json.loads(result.stdout)


def _seed_job(conn, url, title="Engineer", **extra):
    cols = {"url": url, "title": title, "site": "acme",
            "full_description": "d" * 300}
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


def _row(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


def _tailored(conn, url, fit_score=8, title="Engineer"):
    _seed_job(conn, url, title=title, fit_score=fit_score,
              tailored_resume_path=f"/tmp/tailored/{url.rsplit('/', 1)[-1]}.txt",
              cover_letter_path=f"/tmp/covers/{url.rsplit('/', 1)[-1]}.txt")


@pytest.fixture()
def seeded_apply_store(app_dir, isolated_db):
    """A store with a resume, a profile, and CAP + 1 tailored, scored jobs —
    one more than the cap, so the journey hits cap_reached with a proof job."""
    (app_dir / "resume.txt").write_text(
        f"Test Person — data engineer résumé. {RESUME_MARKER}", encoding="utf-8")
    config.write_secret_text(config.PROFILE_PATH, json.dumps({
        "personal": {"full_name": "Test Person"},
        "resume_facts": {"preserved_projects": [f"Built {FACTS_MARKER}"]},
    }))
    conn = database.init_db()
    # jobs/1 sits at fit 9 (acquired first — fit DESC); the rest at 8.
    _tailored(conn, "https://jobs/1", fit_score=9)
    for i in range(2, CAP + 2):  # CAP+1 jobs: the last one meets the cap
        _tailored(conn, f"https://jobs/{i}", fit_score=8)
    return conn


@pytest.fixture()
def exploding_apply(monkeypatch):
    """Record-and-raise on every engine, browser, network, and LLM seam the
    apply path could reach: if a bookkeeping verb (or a verb loop posing as
    a submission flow) invokes an engine, spawns a process, opens a socket,
    or calls an LLM, the test fails loudly instead of passing by accident."""
    calls: list[str] = []

    def boom(name):
        def _boom(*a, **k):
            calls.append(name)
            raise AssertionError(f"{name} ran during host apply bookkeeping")
        return _boom

    import applypilot.apply.chrome as chrome
    import applypilot.apply.launcher as launcher
    import applypilot.llm as llm
    import applypilot.scoring.scorer as scorer

    monkeypatch.setattr(launcher, "make_engine", boom("make_engine"))
    monkeypatch.setattr(chrome, "launch_chrome", boom("chrome.launch_chrome"))
    monkeypatch.setattr(subprocess, "Popen", boom("subprocess.Popen"))
    monkeypatch.setattr(socket, "create_connection", boom("socket.create_connection"))
    monkeypatch.setattr(socket, "socket", boom("socket.socket"))
    monkeypatch.setattr(urllib.request, "urlopen", boom("urllib.request.urlopen"))
    monkeypatch.setattr(llm, "_instance", boom("llm.instance"))
    monkeypatch.setattr(llm, "get_client", boom("llm.get_client"))
    monkeypatch.setattr(scorer, "get_client", boom("scorer.get_client"))
    monkeypatch.setattr(onboarding, "LLMBackend", boom("LLMBackend"))
    monkeypatch.setattr(onboarding, "ClaudePrintBackend",
                        boom("ClaudePrintBackend"))
    return calls


# ---------------------------------------------------------------------------
# The zero-submission journey (the guardrail centerpiece)
# ---------------------------------------------------------------------------

def test_apply_bookkeeping_journey_is_pure_bookkeeping(seeded_apply_store,
                                                       exploding_apply):
    """A driving agent acquires jobs one at a time and records applied
    outcomes, with every engine/browser/network/LLM seam patched to
    raise-and-record: the loop runs to the cap, takes and releases the
    per-job lock per job, decrements headroom per apply, stages and
    consumes NO plan token, and makes zero submission-flavored calls."""
    conn = seeded_apply_store
    plan_file = config.APP_DIR / "apply-plan.json"
    applied = 0
    for _ in range(20):  # bounded: a broken loop must fail, not hang
        result = run("apply-acquire")
        payload = payload_of(result)
        if result.exit_code != 0:
            assert payload["error"]["code"] == "cap_reached", payload
            assert payload["error"]["details"]["apply"]["headroom"] == 0
            break
        url = payload["job"]["url"]
        # The lock is on the row, visibly held by the host loop.
        assert payload["acquired_by"] == "host-worker-0"
        assert _row(conn, url)["apply_status"] == "in_progress"
        assert _row(conn, url)["agent_id"] == "host-worker-0"
        # Bookkeeping says the submission is the driver's own act.
        assert "no verb launches" in payload["guardrails"]["browser_work"]

        result = run("apply-result", "--url", url, "--status", "applied")
        assert result.exit_code == 0, result.stdout
        payload = payload_of(result)
        assert payload["recorded"]["outcome"] == "recorded"
        applied += 1
        assert payload["apply"]["applied_today"] == applied
        assert payload["apply"]["headroom"] == CAP - applied
        # Lock released: no in_progress rows survive a recorded result.
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE apply_status = 'in_progress'"
        ).fetchone()[0] == 0
    else:
        pytest.fail("the apply loop never hit the cap")

    assert applied == CAP

    # The plan-then-confirm guardrail was never staged or consumed: the
    # token file does not exist — bookkeeping cannot complete a submission
    # flow because there is no submission flow to complete.
    assert not plan_file.exists()
    assert exploding_apply == []

    # All CAP+1 jobs ended applied-or-queued honestly: exactly CAP applied,
    # the surplus untouched and still acquirable tomorrow.
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'"
    ).fetchone()[0] == CAP
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status IS NULL"
    ).fetchone()[0] == 1


def test_apply_verbs_run_without_provider_or_tier_gate(seeded_apply_store,
                                                       exploding_apply):
    """The verbs bypass check_tier the way the other agent verbs do: they
    run on a store with zero provider configuration (the exploding seams
    stand in for every backend), no engine CLI, no Claude sign-in."""
    result = run("apply-acquire")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["stage"] == "apply"
    assert exploding_apply == []


# ---------------------------------------------------------------------------
# Lock semantics
# ---------------------------------------------------------------------------

def test_acquire_of_a_locked_queue_returns_busy_without_corrupting(app_dir, isolated_db):
    """The only candidate under a fresh lock → queue_busy; the lock holder's
    state is untouched (not stolen, not reset, not double-locked)."""
    conn = database.init_db()
    _tailored(conn, "https://jobs/1")
    first = run("apply-acquire")
    assert first.exit_code == 0, first.stdout
    held_url = payload_of(first)["job"]["url"]
    before = dict(_row(conn, held_url))

    second = run("apply-acquire", "--min-score", "0")
    assert second.exit_code != 0
    envelope = payload_of(second)
    assert envelope["error"]["code"] == "queue_busy"
    assert held_url in envelope["error"]["details"]["locked"]

    after = dict(_row(conn, held_url))
    assert after["apply_status"] == before["apply_status"] == "in_progress"
    assert after["agent_id"] == before["agent_id"] == "host-worker-0"


def test_apply_result_on_a_non_acquired_job_errors_cleanly(seeded_apply_store):
    """apply-result without a held lock is refused — it is the worker's own
    report path, not a mark override — and writes nothing."""
    conn = seeded_apply_store
    result = run("apply-result", "--url", "https://jobs/1",
                 "--status", "applied")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "not_acquired"

    row = _row(conn, "https://jobs/1")
    assert row["apply_status"] is None
    assert row["applied_at"] is None

    result = run("apply-result", "--url", "https://jobs/none",
                 "--status", "applied")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "unknown_job"


# ---------------------------------------------------------------------------
# Cap refusal and min-score filtering
# ---------------------------------------------------------------------------

def test_cap_refused_before_any_lock_is_taken(seeded_apply_store):
    """The live daily-cap recheck runs before acquire: cap_reached takes no
    lock, changes no row."""
    conn = seeded_apply_store
    now = datetime.now(timezone.utc).isoformat()
    for i in range(1, CAP + 1):
        conn.execute("UPDATE jobs SET applied_at = ? WHERE url = ?",
                     (now, f"https://jobs/{i}"))
    conn.commit()

    result = run("apply-acquire")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "cap_reached"
    assert envelope["error"]["details"]["apply"] == {
        "cap": CAP, "applied_today": CAP, "headroom": 0}

    row = _row(conn, "https://jobs/6")
    assert row["apply_status"] is None  # no lock was taken


def test_min_score_filters_the_queue(seeded_apply_store):
    """--min-score narrows acquisition to jobs at or above the threshold."""
    conn = seeded_apply_store
    result = run("apply-acquire", "--min-score", "9")
    assert result.exit_code == 0, result.stdout
    url = payload_of(result)["job"]["url"]
    assert _row(conn, url)["fit_score"] == 9

    result = run("apply-result", "--url", url, "--status", "applied")
    assert result.exit_code == 0, result.stdout

    # Only fit-8 jobs remain; a min-score of 9 finds nothing to acquire.
    result = run("apply-acquire", "--min-score", "9")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "no_eligible_job"

    # Acquire one normally, then check the summary honors the filter: the
    # next-eligible summary at 9+ is honest about there being nothing.
    result = run("apply-acquire")
    assert result.exit_code == 0, result.stdout
    next_url = payload_of(result)["job"]["url"]
    result = run("apply-result", "--url", next_url,
                 "--status", "applied", "--min-score", "9")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["next_eligible"] is None


@pytest.mark.parametrize("bad", ["banana", "-5", "--5"])
def test_invalid_min_score_is_a_typed_refusal(seeded_apply_store, bad):
    # "-5"/"--5" are audit F5: the old lstrip("-") digit check admitted
    # them, and a negative threshold disabled the queue's quality gate.
    result = run("apply-acquire", "--min-score", bad)
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_min_score"


# ---------------------------------------------------------------------------
# Status vocabulary (PR #57): five values, captcha terminal, unknown refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["applied", "failed", "expired",
                                    "captcha", "login_issue"])
def test_status_vocabulary_persists(seeded_apply_store, status):
    conn = seeded_apply_store
    result = run("apply-acquire")
    assert result.exit_code == 0, result.stdout
    url = payload_of(result)["job"]["url"]

    argv = ["apply-result", "--url", url, "--status", status]
    result = run(*argv, "--reason", "driver observed it") \
        if status == "failed" else run(*argv)
    assert result.exit_code == 0, result.stdout
    payload = payload_of(result)
    assert payload["recorded"]["status"] == status

    row = _row(conn, url)
    assert row["apply_status"] == status
    if status == "applied":
        assert row["applied_at"] is not None
        assert row["apply_error"] is None
    elif status == "failed":
        assert row["apply_error"] == "driver observed it"
        assert row["apply_attempts"] == 1  # retryable: one attempt consumed
    else:  # terminal vocabulary: the attempts ban rides the record
        assert row["apply_attempts"] == 99


def test_captcha_is_terminal_and_never_requeued(app_dir, isolated_db):
    """A captcha outcome is recorded with the attempts ban and the queue
    predicate keeps the job out — reset_failed never requeues it either."""
    conn = database.init_db()
    _tailored(conn, "https://jobs/1")
    result = run("apply-acquire")
    url = payload_of(result)["job"]["url"]
    assert run("apply-result", "--url", url,
               "--status", "captcha").exit_code == 0
    assert _row(conn, url)["apply_status"] == "captcha"

    result = run("apply-acquire", "--min-score", "0")
    assert result.exit_code != 0
    # The captcha job exists but is excluded — the queue is genuinely empty.
    assert payload_of(result)["error"]["code"] == "no_eligible_job"

    from applypilot.apply.launcher import reset_failed
    assert reset_failed() == 0  # captcha rows are skipped by the sweep


def test_unknown_status_values_are_refused(seeded_apply_store):
    conn = seeded_apply_store
    result = run("apply-acquire")
    url = payload_of(result)["job"]["url"]

    result = run("apply-result", "--url", url, "--status", "hired")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_status"
    assert envelope["error"]["details"]["valid"] == [
        "applied", "failed", "expired", "captcha", "login_issue"]

    # The refusal wrote nothing: the lock still stands.
    assert _row(conn, url)["apply_status"] == "in_progress"


# ---------------------------------------------------------------------------
# Wire contract and redaction
# ---------------------------------------------------------------------------

def test_acquire_payload_shape_matches_job_next(seeded_apply_store):
    """Same job block as the stage payloads (excerpt-capped description),
    progress counts, resume metadata redacted by default, cap block."""
    result = run("apply-acquire")
    assert result.exit_code == 0, result.stdout
    assert result.stderr == ""
    payload = payload_of(result)
    job = payload["job"]
    assert job["url"] == "https://jobs/1"
    assert job["fit_score"] == 9
    assert job["description_excerpt"] == "d" * 300
    assert payload["application_url"] is None
    assert payload["resume_path"].endswith(".txt")
    assert payload["apply"]["cap"] == CAP
    assert payload["progress"]["total"] == CAP + 1
    assert RESUME_MARKER not in result.stdout
    assert "resume_facts" not in result.stdout

    revealed = payload_of(run("apply-acquire", "--reveal"))
    assert RESUME_MARKER in revealed["resume"]["text"]
    assert revealed["resume_facts"] == {
        "preserved_projects": [f"Built {FACTS_MARKER}"]}


def test_missing_flags_are_invalid_usage(seeded_apply_store):
    result = run("apply-result", "--status", "applied")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "invalid_usage"

    result = run("apply-result", "--url", "https://jobs/1")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "invalid_usage"
