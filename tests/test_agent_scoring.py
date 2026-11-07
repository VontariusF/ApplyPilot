"""Contract tests for the host-driven scoring verbs (spec art_IMViYTMU, T1).

`agent job-next --stage score` and `agent score` turn the driving agent
into the scoring intelligence: the queue, the rubric, and the persistence
stay ApplyPilot's. The wire contract is the protocol's — one pure-JSON
success document, the {"error": {...}} envelope with exit 1, redaction
by default.

The centerpiece is the zero-LLM journey (the exploding-backends pattern
from tests/test_agent_protocol.py:362-407): every LLM/Claude seam is
patched to raise-and-record, a driver scores every eligible job through
the two verbs alone, and the recorded call count must be 0.
"""

import json

import pytest
from typer.testing import CliRunner

from applypilot import config, database, onboarding
from applypilot.cli import app

runner = CliRunner()

# Distinctive strings the default payloads must never leak: one in the
# resume document, one in the profile's resume_facts section.
RESUME_MARKER = "QUINTESSENTIAL_RESUME_MARKER_9F3A"
FACTS_MARKER = "GHOST_PROJECT_MARKER_7C2B"


def run(*args):
    """Invoke `applypilot agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def run_cli(*args):
    """Invoke a top-level `applypilot ...` verb (status)."""
    return runner.invoke(app, list(args))


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


@pytest.fixture()
def seeded_store(app_dir, isolated_db):
    """A store with a resume, a profile with resume_facts, three
    description-eligible jobs, and one stranded (description-less) job."""
    (app_dir / "resume.txt").write_text(
        f"Test Person — data engineer résumé. {RESUME_MARKER}", encoding="utf-8")
    config.write_secret_text(config.PROFILE_PATH, json.dumps({
        "personal": {"full_name": "Test Person"},
        "resume_facts": {"preserved_projects": [f"Built {FACTS_MARKER}"]},
    }))
    conn = database.init_db()
    _seed_job(conn, "https://jobs/1", title="Data Engineer",
              location="Austin, TX", salary="$140k")
    _seed_job(conn, "https://jobs/2", title="Analytics Engineer")
    _seed_job(conn, "https://jobs/3", title="Platform Engineer")
    _seed_job(conn, "https://jobs/stranded", title="No Description",
              full_description=None)
    return conn


@pytest.fixture()
def exploding_llm(monkeypatch):
    """Record-and-raise on every LLM/Claude seam the scoring paths could
    reach: the llm singleton (and its factory), scorer.get_client (scorer
    binds it at import time), and both onboarding backends — the PR #58
    fixture pattern. Any call fails the test loudly instead of passing
    by accident."""
    calls: list[str] = []

    class _Boom:
        def chat(self, *a, **k):
            calls.append("llm.chat")
            raise AssertionError("llm.chat ran in host mode")

        def ask(self, *a, **k):
            calls.append("llm.ask")
            raise AssertionError("llm.ask ran in host mode")

    def make_backend(name):
        class _BoomBackend:
            def __init__(self, *a, **k):
                pass

            def kickoff(self, *a, **k):
                calls.append(f"{name}.kickoff")
                raise AssertionError(f"{name}.kickoff ran in host mode")

            def send(self, *a, **k):
                calls.append(f"{name}.send")
                raise AssertionError(f"{name}.send ran in host mode")

        return _BoomBackend

    import applypilot.llm as llm
    import applypilot.scoring.scorer as scorer

    monkeypatch.setattr(llm, "_instance", _Boom())
    monkeypatch.setattr(llm, "get_client", lambda: _Boom())
    monkeypatch.setattr(scorer, "get_client", lambda: _Boom())
    monkeypatch.setattr(onboarding, "LLMBackend", make_backend("LLMBackend"))
    monkeypatch.setattr(onboarding, "ClaudePrintBackend",
                        make_backend("ClaudePrintBackend"))
    return calls


# ---------------------------------------------------------------------------
# The zero-LLM scoring journey (the acceptance centerpiece)
# ---------------------------------------------------------------------------

def test_host_scoring_journey_runs_zero_backend_calls(seeded_store, exploding_llm):
    """A driving agent scores every eligible job through job-next + score
    alone, with every LLM/Claude seam patched to raise-and-record: the
    loop terminates, makes zero backend calls, reports unscored == 0, and
    every default payload stayed redacted."""
    from applypilot.scoring.scorer import SCORE_PROMPT

    all_stdout: list[str] = []
    served: list[str] = []
    for _ in range(10):  # bounded: a broken loop must fail, not hang
        result = run("job-next", "--stage", "score")
        assert result.exit_code == 0, result.stdout
        all_stdout.append(result.stdout)
        payload = payload_of(result)
        if payload["job"] is None:
            break
        served.append(payload["job"]["url"])
        # The rubric rides the payload — the driver never guesses the scale.
        assert payload["rubric"] == SCORE_PROMPT
        result = run("score", "--url", payload["job"]["url"], "--score", "7",
                     "--keywords", "python, sql", "--reasoning", "Close fit.")
        assert result.exit_code == 0, result.stdout
        all_stdout.append(result.stdout)
    else:
        pytest.fail("the scoring loop never terminated")

    assert served == ["https://jobs/1", "https://jobs/2", "https://jobs/3"]
    assert exploding_llm == []

    # The score verb returns the NEXT pending payload; the final call
    # drains the queue — the loop's termination signal.
    final = payload_of(run("job-next", "--stage", "score"))
    assert final["job"] is None
    assert final["progress"]["unscored"] == 0

    # The stranded job never entered the score queue, but it is visible.
    assert final["progress"]["pending_detail"] >= 1

    # Provenance: every score is host-written, and status agrees.
    status = payload_of(run_cli("status", "--json"))
    assert status["unscored"] == 0
    assert status["score_provenance"] == {"host": 3, "provider": 0,
                                          "unattributed": 0}

    # The whole journey ran redacted: no resume text in any default payload.
    assert all(RESUME_MARKER not in chunk for chunk in all_stdout)
    assert all("resume_facts" not in chunk for chunk in all_stdout)


# ---------------------------------------------------------------------------
# Envelopes: invalid_score, already_scored, not_scoreable, invalid_usage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,reasoning", [
    ("0", "Because."),
    ("11", "Because."),
    ("banana", "Because."),
    ("7.5", "Because."),
    ("7", ""),
    ("7", "   "),
])
def test_invalid_score_envelopes(seeded_store, score, reasoning):
    argv = ["score", "--url", "https://jobs/1", "--score", score]
    if reasoning is not None:
        argv += ["--reasoning", reasoning]
    result = run(*argv)
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_score"
    # A failed verb never writes.
    assert _row(seeded_store, "https://jobs/1")["fit_score"] is None


def test_missing_url_or_score_are_invalid_usage(seeded_store):
    result = run("score", "--score", "5", "--reasoning", "Because.")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "invalid_usage"

    result = run("score", "--url", "https://jobs/1", "--reasoning", "Because.")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "invalid_usage"


def test_already_scored_then_rescore_overwrites(seeded_store):
    first = run("score", "--url", "https://jobs/1", "--score", "8",
                "--keywords", "k1, k2", "--reasoning", "First pass.")
    assert first.exit_code == 0, first.stdout

    again = run("score", "--url", "https://jobs/1", "--score", "6",
                "--reasoning", "Second look.")
    assert again.exit_code != 0
    envelope = payload_of(again)
    assert envelope["error"]["code"] == "already_scored"
    assert envelope["error"]["details"]["fit_score"] == 8
    # The refusal never wrote.
    assert _row(seeded_store, "https://jobs/1")["fit_score"] == 8

    overwrite = run("score", "--url", "https://jobs/1", "--score", "6",
                    "--reasoning", "Second look.", "--rescore")
    assert overwrite.exit_code == 0, overwrite.stdout
    row = _row(seeded_store, "https://jobs/1")
    assert row["fit_score"] == 6
    assert row["scored_by"] == "host"  # --rescore keeps the host stamp
    assert row["score_error"] is None
    # No keywords given: an empty first line degrades honestly downstream.
    assert row["score_reasoning"] == "\nSecond look."


def test_host_score_clears_prior_score_error(seeded_store):
    conn = seeded_store
    conn.execute(
        "UPDATE jobs SET score_error = 'prior LLM failure' WHERE url = ?",
        ("https://jobs/1",))
    conn.commit()

    result = run("score", "--url", "https://jobs/1", "--score", "7",
                 "--reasoning", "Host judged it.")
    assert result.exit_code == 0, result.stdout
    row = _row(conn, "https://jobs/1")
    assert row["score_error"] is None
    assert row["fit_score"] == 7


def test_score_refuses_unscoreable_targets(seeded_store):
    result = run("score", "--url", "https://jobs/none", "--score", "5",
                 "--reasoning", "Because.")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "not_scoreable"

    # Exists, but no captured description — it is not in the score queue.
    result = run("score", "--url", "https://jobs/stranded", "--score", "5",
                 "--reasoning", "Because.")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "not_scoreable"


def test_job_next_rejects_unimplemented_stages(seeded_store):
    """T1 implements score only; later spec PRs add the other stages."""
    for stage in ("enrich", "tailor", "cover"):
        result = run("job-next", "--stage", stage)
        assert result.exit_code != 0
        envelope = payload_of(result)
        assert envelope["error"]["code"] == "invalid_stage"
        assert envelope["error"]["details"]["stage"] == stage


# ---------------------------------------------------------------------------
# Queue semantics: stranding, the excerpt window, the termination signal
# ---------------------------------------------------------------------------

def test_job_next_skips_stranded_jobs_and_terminates(seeded_store):
    served = []
    for _ in range(5):
        payload = payload_of(run("job-next", "--stage", "score"))
        if payload["job"] is None:
            break
        served.append(payload["job"]["url"])
        result = run("score", "--url", payload["job"]["url"], "--score", "5",
                     "--reasoning", "Because.")
        assert result.exit_code == 0, result.stdout
    else:
        pytest.fail("the loop never terminated")

    assert served == ["https://jobs/1", "https://jobs/2", "https://jobs/3"]
    payload = payload_of(run("job-next", "--stage", "score"))
    assert payload["job"] is None
    assert payload["progress"]["unscored"] == 0
    # The stranded job stays visible in every payload — never silently dropped.
    assert payload["progress"]["pending_detail"] >= 1


def test_job_next_excerpt_caps_at_the_llm_window(seeded_store):
    from applypilot.scoring.scorer import SCORE_PROMPT

    long_desc = "x" * 9000
    seeded_store.execute(
        "UPDATE jobs SET full_description = ? WHERE url = ?",
        (long_desc, "https://jobs/1"))
    seeded_store.commit()

    payload = payload_of(run("job-next", "--stage", "score"))
    job = payload["job"]
    assert job["url"] == "https://jobs/1"
    assert len(job["description_excerpt"]) == 6000  # the LLM's exact window
    assert job["description_chars"] == 9000
    assert job["excerpt_truncated"] is True
    assert payload["rubric"] == SCORE_PROMPT
    assert job["site"] == "acme" and job["location"] == "Austin, TX"


# ---------------------------------------------------------------------------
# Redaction and persistence parity
# ---------------------------------------------------------------------------

def test_default_payload_is_redacted_and_reveal_opts_in(seeded_store):
    result = run("job-next", "--stage", "score")
    assert result.exit_code == 0
    assert RESUME_MARKER not in result.stdout
    assert "resume_facts" not in result.stdout
    payload = payload_of(result)
    assert payload["resume"] == {"provided": True,
                                 "chars": len(f"Test Person — data engineer résumé. {RESUME_MARKER}")}

    revealed = payload_of(run("job-next", "--stage", "score", "--reveal"))
    assert RESUME_MARKER in revealed["resume"]["text"]
    assert revealed["resume_facts"] == {"preserved_projects": [f"Built {FACTS_MARKER}"]}


def test_score_response_carries_next_payload_redacted(seeded_store):
    result = run("score", "--url", "https://jobs/1", "--score", "7",
                 "--keywords", "k", "--reasoning", "Because.")
    assert result.exit_code == 0, result.stdout
    assert RESUME_MARKER not in result.stdout
    payload = payload_of(result)
    # The response serves the NEXT pending job — one call per loop step.
    assert payload["job"]["url"] == "https://jobs/2"
    assert "text" not in payload["resume"]


def test_persistence_parity_host_vs_llm(seeded_store, monkeypatch):
    """A host-scored row and an LLM-scored row are identical except
    scored_by (and the wall-clock scored_at): same blob shape, same
    score_error clearing, same portal split."""
    import applypilot.scoring.scorer as scorer
    from applypilot.portal.queries import split_score_reasoning

    class _FakeClient:
        provider_name = "test-provider"

        def chat(self, *a, **k):
            return "SCORE: 8\nKEYWORDS: python, sql\nREASONING: Good fit."

    monkeypatch.setattr(scorer, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(scorer, "RESUME_PATH", config.RESUME_PATH)

    result = scorer.run_scoring(url="https://jobs/1")
    assert result["scored"] == 1 and result["errors"] == 0

    host = run("score", "--url", "https://jobs/2", "--score", "8",
               "--keywords", "python, sql", "--reasoning", "Good fit.")
    assert host.exit_code == 0, host.stdout

    conn = seeded_store
    llm_row = _row(conn, "https://jobs/1")
    host_row = _row(conn, "https://jobs/2")
    assert llm_row["fit_score"] == host_row["fit_score"] == 8
    assert llm_row["score_reasoning"] == host_row["score_reasoning"] == "python, sql\nGood fit."
    assert llm_row["score_error"] is None and host_row["score_error"] is None
    assert llm_row["scored_at"] and host_row["scored_at"]
    assert llm_row["scored_by"] == "test-provider"  # the provider name...
    assert host_row["scored_by"] == "host"          # ...vs the host stamp

    # The portal seam reads the identical blob shape for both writers.
    assert (split_score_reasoning(llm_row["score_reasoning"])
            == split_score_reasoning(host_row["score_reasoning"])
            == ("python, sql", "Good fit."))


# ---------------------------------------------------------------------------
# Provenance in status
# ---------------------------------------------------------------------------

def test_status_json_provenance_split(app_dir, isolated_db):
    conn = database.init_db()
    stamp = "2026-11-07T00:00:00+00:00"
    _seed_job(conn, "https://jobs/host", fit_score=7, scored_by="host",
              score_reasoning="k\nr", scored_at=stamp)
    _seed_job(conn, "https://jobs/llm", fit_score=9, scored_by="gemini",
              score_reasoning="k\nr", scored_at=stamp)
    # A pre-provenance row: scored, scored_by NULL — never backfilled.
    _seed_job(conn, "https://jobs/legacy", fit_score=5,
              score_reasoning="k\nr", scored_at=stamp)

    status = payload_of(run_cli("status", "--json"))
    assert status["score_provenance"] == {"host": 1, "provider": 1,
                                          "unattributed": 1}


def test_status_rich_label_reads_provenance(app_dir, isolated_db):
    """The 'Scored by ...' row renders the authorship, not an assumption."""
    from applypilot.cli import _scored_label

    assert _scored_label({"host": 0, "provider": 0, "unattributed": 0}) == "Scored by LLM"
    # Legacy rows keep today's wording — unattributed renders as it always has.
    assert _scored_label({"host": 0, "provider": 2, "unattributed": 3}) == "Scored by LLM"
    assert _scored_label({"host": 2, "provider": 0, "unattributed": 0}) == "Scored by host agent"
    assert _scored_label({"host": 1, "provider": 2, "unattributed": 3}) == "Scored (host 1, LLM 5)"

    # And the rendered table carries the honest label end to end.
    conn = database.init_db()
    _seed_job(conn, "https://jobs/1", fit_score=7, scored_by="host",
              score_reasoning="k\nr", scored_at="2026-11-07T00:00:00+00:00")
    result = run_cli("status")
    assert result.exit_code == 0
    assert "Scored by host agent" in result.output
