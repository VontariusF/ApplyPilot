"""Contract tests for the host-driven enrich/tailor/cover verbs (spec
art_IMViYTMU, T2).

`agent job-next --stage enrich|tailor|cover` plus the matching persist
verbs turn the driving agent into the intelligence for the three
remaining LLM seams. The wire contract is the protocol's — one pure-JSON
success document, the {"error": {...}} envelope with exit 1, redaction
by default.

The centerpiece is the zero-LLM journey (the exploding-backends pattern
from tests/test_agent_protocol.py and tests/test_agent_scoring.py): a
stranded pending_detail job is enriched into the score queue, scored,
tailored, and covered — every LLM/Claude seam patched to raise-and-
record, and the recorded call count must be 0 end to end.
"""

import json

import pytest
from typer.testing import CliRunner

from kestrel import config, database, onboarding
from kestrel.cli import app

runner = CliRunner()

# Distinctive strings the default payloads must never leak: one in the
# resume document, one in the profile's resume_facts section.
RESUME_MARKER = "QUINTESSENTIAL_RESUME_MARKER_9F3A"
FACTS_MARKER = "GHOST_PROJECT_MARKER_7C2B"

PROFILE = {
    "personal": {"full_name": "Test Person"},
    "skills_boundary": {
        "Languages": ["Python", "SQL"],
        "Tools": ["Airflow", "Docker"],
    },
    "experience": {"education_level": "B.S. Computer Science"},
    "resume_facts": {
        "preserved_companies": ["Acme Corp"],
        "preserved_school": "State University",
        "real_metrics": ["40%"],
        "preserved_projects": [f"Built {FACTS_MARKER}"],
    },
}

# Validator-passing tailor data (lenient mode: structure, fabrication,
# preserved companies/school, no LLM self-talk).
GOOD_TAILOR_DATA = json.dumps({
    "title": "Senior Data Engineer",
    "summary": "Ships data pipelines with Python and SQL.",
    "skills": {"Languages": "Python, SQL", "Tools": "Airflow"},
    "experience": [
        {"header": "Senior Engineer at Acme Corp", "subtitle": "2020 - 2024",
         "bullets": ["Rebuilt the ingest pipeline and cut load time 40%."]},
    ],
    "projects": [],
    "education": "B.S. Computer Science | State University",
})

# Validator-passing cover letter: starts with Dear, no self-talk, no
# dashes that survive sanitize_text.
GOOD_COVER = (
    "Dear Hiring Manager,\n\n"
    "I rebuilt the ingest pipeline at Acme Corp and cut load time 40%.\n\n"
    "Your data platform role needs that exact work. I would enjoy walking "
    "through the details.\n\n"
    "Test Person"
)

DESCRIPTION = "Responsibilities: build pipelines. Requirements: Python, SQL."


def run(*args):
    """Invoke `kestrel agent ...` and return the CliRunner result."""
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


def _score(conn, url, score=8):
    """Make the stranded job tailorable without the verb loop: capture a
    description (the tailorable predicate requires one) and set a score."""
    conn.execute(
        "UPDATE jobs SET fit_score = ?, "
        "full_description = COALESCE(full_description, ?) WHERE url = ?",
        (score, DESCRIPTION, url))
    conn.commit()


@pytest.fixture()
def seeded_store(app_dir, isolated_db, monkeypatch):
    """A store with a resume, a profile with resume_facts, and one
    stranded (description-less) job — the enrich journey's raw material."""
    (app_dir / "resume.txt").write_text(
        f"Test Person — data engineer résumé. {RESUME_MARKER}", encoding="utf-8")
    config.write_secret_text(config.PROFILE_PATH, json.dumps(PROFILE))
    conn = database.init_db()
    _seed_job(conn, "https://jobs/stranded", title="Data Engineer",
              full_description=None, location="Austin, TX", salary="$140k")
    # The document writers bind TAILORED_DIR/COVER_LETTER_DIR at import
    # time — repoint them at the app_dir fixture, as the other suites do.
    import kestrel.scoring.cover_letter as cover_letter
    import kestrel.scoring.tailor as tailor

    monkeypatch.setattr(tailor, "TAILORED_DIR", config.TAILORED_DIR)
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", config.COVER_LETTER_DIR)
    return conn


@pytest.fixture()
def exploding_llm(monkeypatch):
    """Record-and-raise on every LLM/Claude seam the pipeline paths could
    reach: the llm singleton (and its factory), the scorer, tailor, cover
    letter, and enrichment-detail bindings, and both onboarding backends.
    Any call fails the test loudly instead of passing by accident."""
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

    import kestrel.enrichment.detail as detail
    import kestrel.llm as llm
    import kestrel.scoring.cover_letter as cover_letter
    import kestrel.scoring.scorer as scorer
    import kestrel.scoring.tailor as tailor

    monkeypatch.setattr(llm, "_instance", _Boom())
    monkeypatch.setattr(llm, "get_client", lambda: _Boom())
    monkeypatch.setattr(scorer, "get_client", lambda: _Boom())
    monkeypatch.setattr(tailor, "get_client", lambda: _Boom())
    monkeypatch.setattr(cover_letter, "get_client", lambda: _Boom())
    monkeypatch.setattr(detail, "get_client", lambda: _Boom())
    monkeypatch.setattr(onboarding, "LLMBackend", make_backend("LLMBackend"))
    monkeypatch.setattr(onboarding, "ClaudePrintBackend",
                        make_backend("ClaudePrintBackend"))
    return calls


# ---------------------------------------------------------------------------
# The zero-LLM journey: enrich -> score -> tailor -> cover
# ---------------------------------------------------------------------------

def test_host_journey_enrich_score_tailor_cover_runs_zero_backend_calls(
        seeded_store, exploding_llm):
    """A driving agent takes one stranded job through enrich, score,
    tailor, and cover with every LLM/Claude seam patched to raise-and-
    record: the loop terminates, makes zero backend calls, and the
    default payloads stayed redacted."""
    all_stdout: list[str] = []

    # 1. Enrich: the stranded job enters the score queue.
    result = run("job-next", "--stage", "enrich")
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)
    payload = payload_of(result)
    assert payload["job"]["url"] == "https://jobs/stranded"
    assert payload["job"]["description_chars"] == 0
    result = run("enrich", "--url", "https://jobs/stranded",
                 "--full-description", DESCRIPTION,
                 "--application-url", "https://apply.example/1")
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)
    next_enrich = payload_of(result)
    assert next_enrich["job"] is None  # the enrich queue drained
    assert next_enrich["progress"]["pending_detail"] == 0

    # 2. Score: the enriched job is now scoreable.
    result = run("job-next", "--stage", "score")
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)
    assert payload_of(result)["job"]["url"] == "https://jobs/stranded"
    result = run("score", "--url", "https://jobs/stranded", "--score", "8",
                 "--keywords", "python, sql", "--reasoning", "Close fit.")
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)

    # 3. Tailor: the scored job carries the data_template contract.
    result = run("job-next", "--stage", "tailor")
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)
    payload = payload_of(result)
    assert payload["job"]["url"] == "https://jobs/stranded"
    assert payload["job"]["fit_score"] == 8
    assert payload["data_template"]["title"]
    assert payload["validation"] == {"mode": "lenient", "retryable_errors": True}
    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", GOOD_TAILOR_DATA)
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)
    written = payload_of(result)
    assert written["written"]["path"].endswith(".txt")
    assert written["progress"]["tailored"] == 1

    # 4. Cover: the tailored job is now coverable.
    result = run("job-next", "--stage", "cover")
    assert result.exit_code == 0, result.stdout
    all_stdout.append(result.stdout)
    assert payload_of(result)["job"]["url"] == "https://jobs/stranded"
    result = run("cover", "--url", "https://jobs/stranded", "--text", GOOD_COVER)
    assert result.exit_code == 0, result.stdout
    written = payload_of(result)
    assert written["written"]["path"].endswith("_CL.txt")
    assert written["progress"]["with_cover_letter"] == 1

    # Zero backend calls anywhere in the journey, and every queue drained.
    assert exploding_llm == []
    final = payload_of(run("job-next", "--stage", "score"))
    assert final["job"] is None
    assert final["progress"]["unscored"] == 0
    assert final["progress"]["pending_detail"] == 0

    # The whole journey ran redacted: no resume text or facts in any
    # default payload.
    assert all(RESUME_MARKER not in chunk for chunk in all_stdout)
    assert all("resume_facts" not in chunk for chunk in all_stdout)

    # Persistence parity: the jobs row looks exactly like the LLM path's —
    # description captured with the same stamp, document paths recorded.
    row = _row(seeded_store, "https://jobs/stranded")
    assert row["full_description"] == DESCRIPTION
    assert row["application_url"] == "https://apply.example/1"
    assert row["detail_scraped_at"] and row["detail_error"] is None
    assert row["tailored_resume_path"] and row["tailored_at"]
    assert row["cover_letter_path"] and row["cover_letter_at"]
    assert "SUMMARY" in (config.TAILORED_DIR / row["tailored_resume_path"]
                         .split("/")[-1]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Enrich envelopes: unknown job, empty description, already enriched
# ---------------------------------------------------------------------------

def test_enrich_requires_url_and_description(seeded_store):
    result = run("enrich")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "invalid_usage"

    result = run("enrich", "--url", "https://jobs/stranded",
                 "--full-description", "   ")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_description"
    # A failed verb never writes.
    assert _row(seeded_store, "https://jobs/stranded")["full_description"] is None


def test_enrich_unknown_job(seeded_store):
    result = run("enrich", "--url", "https://jobs/none",
                 "--full-description", DESCRIPTION)
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "unknown_job"
    assert envelope["error"]["details"]["url"] == "https://jobs/none"


def test_enrich_refuses_recapture(seeded_store):
    first = run("enrich", "--url", "https://jobs/stranded",
                "--full-description", DESCRIPTION)
    assert first.exit_code == 0, first.stdout

    again = run("enrich", "--url", "https://jobs/stranded",
                "--full-description", "Different text entirely.")
    assert again.exit_code != 0
    envelope = payload_of(again)
    assert envelope["error"]["code"] == "already_enriched"
    # The refusal never overwrote the first capture.
    assert _row(seeded_store, "https://jobs/stranded")["full_description"] == DESCRIPTION


def test_enrich_moves_job_into_score_queue(seeded_store):
    before = payload_of(run("job-next", "--stage", "score"))
    assert before["job"] is None  # nothing scoreable yet
    assert before["progress"]["pending_detail"] == 1

    run("enrich", "--url", "https://jobs/stranded", "--full-description", DESCRIPTION)

    after = payload_of(run("job-next", "--stage", "score"))
    assert after["job"]["url"] == "https://jobs/stranded"
    assert after["progress"]["pending_detail"] == 0
    assert after["progress"]["unscored"] == 1


# ---------------------------------------------------------------------------
# Tailor: the validator retry envelope and the skip-the-judge contract
# ---------------------------------------------------------------------------

def test_tailor_retry_envelope_carries_avoid_notes(seeded_store):
    _score(seeded_store, "https://jobs/stranded")
    bad = json.dumps({
        "title": "Senior Data Engineer",
        # summary missing entirely
        "skills": {"Languages": "Python, SQL"},
        "experience": [{"header": "Contractor", "subtitle": "2020",
                        "bullets": ["Worked."]}],
        "projects": [],
        "education": "B.S.",
    })
    result = run("tailor", "--url", "https://jobs/stranded", "--data", bad)
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_resume_data"
    assert envelope["error"]["details"]["retryable"] is True
    assert envelope["error"]["details"]["avoid_notes"] == \
        envelope["error"]["details"]["errors"]
    assert any("summary" in e for e in envelope["error"]["details"]["errors"])
    # Nothing persisted on a failed validation.
    row = _row(seeded_store, "https://jobs/stranded")
    assert row["tailored_resume_path"] is None

    # Retry with corrected data succeeds.
    retry = run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    assert retry.exit_code == 0, retry.stdout
    assert _row(seeded_store, "https://jobs/stranded")["tailored_resume_path"]


def test_tailor_rejects_unparseable_data(seeded_store):
    _score(seeded_store, "https://jobs/stranded")
    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", "not json at all")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_resume_data"
    assert envelope["error"]["details"]["retryable"] is True


def test_tailor_fabricated_skill_is_enforced_even_lenient(seeded_store):
    """Lenient mode skips banned filler words, never fabrication: the
    host agent's data must respect the profile's skills boundary."""
    _score(seeded_store, "https://jobs/stranded")
    bad = json.loads(GOOD_TAILOR_DATA)
    bad["skills"] = {"Languages": "Python, SQL, golang"}
    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", json.dumps(bad))
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_resume_data"
    assert any("golang" in e for e in envelope["error"]["details"]["errors"])
    assert _row(seeded_store, "https://jobs/stranded")["tailored_resume_path"] is None


def test_tailor_refuses_unscored_or_untailorable_targets(seeded_store):
    result = run("tailor", "--url", "https://jobs/none", "--data", GOOD_TAILOR_DATA)
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "unknown_job"

    result = run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "not_tailorable"


def test_tailor_refuses_second_resume(seeded_store):
    _score(seeded_store, "https://jobs/stranded")
    first = run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    assert first.exit_code == 0, first.stdout

    again = run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    assert again.exit_code != 0
    assert payload_of(again)["error"]["code"] == "already_tailored"


def test_tailor_requires_a_profile(app_dir, isolated_db):
    """No profile.json yet: a typed no_profile envelope, not a traceback."""
    conn = database.init_db()
    _seed_job(conn, "https://jobs/1", fit_score=8)
    result = run("tailor", "--url", "https://jobs/1", "--data", GOOD_TAILOR_DATA)
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "no_profile"


# ---------------------------------------------------------------------------
# Cover: validation failure and success paths
# ---------------------------------------------------------------------------

def test_cover_validation_failure_then_success(seeded_store):
    _score(seeded_store, "https://jobs/stranded")
    run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)

    # A letter without the Dear opening fails validation — retryably,
    # carrying the validator's errors, and persisting nothing.
    bad = "Hello there, I am applying for the role."
    result = run("cover", "--url", "https://jobs/stranded", "--text", bad)
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_cover_letter"
    assert envelope["error"]["details"]["retryable"] is True
    assert any("Dear" in e for e in envelope["error"]["details"]["errors"])
    assert _row(seeded_store, "https://jobs/stranded")["cover_letter_path"] is None

    # Retry with a validator-passing letter persists via the document paths.
    good = run("cover", "--url", "https://jobs/stranded", "--text", GOOD_COVER)
    assert good.exit_code == 0, good.stdout
    row = _row(seeded_store, "https://jobs/stranded")
    assert row["cover_letter_path"]
    assert row["cover_attempts"] == 1
    assert row["cover_letter_at"]
    with open(row["cover_letter_path"], encoding="utf-8") as f:
        assert f.read().startswith("Dear")


def test_cover_sanitizes_dashes_on_receipt(seeded_store):
    """The LLM path auto-fixes em dashes before validating; the host path
    runs the same sanitize step, so a dashed letter validates and the
    persisted text is clean."""
    _score(seeded_store, "https://jobs/stranded")
    run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)

    dashed = GOOD_COVER.replace("and cut load time 40%",
                                "and cut load time — 40%")
    result = run("cover", "--url", "https://jobs/stranded", "--text", dashed)
    assert result.exit_code == 0, result.stdout
    row = _row(seeded_store, "https://jobs/stranded")
    with open(row["cover_letter_path"], encoding="utf-8") as f:
        assert "—" not in f.read()


def test_cover_requires_tailored_materials(seeded_store):
    _score(seeded_store, "https://jobs/stranded")

    result = run("cover", "--url", "https://jobs/stranded", "--text", GOOD_COVER)
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "not_coverable"


def test_cover_refuses_second_letter(seeded_store):
    _score(seeded_store, "https://jobs/stranded")
    run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    first = run("cover", "--url", "https://jobs/stranded", "--text", GOOD_COVER)
    assert first.exit_code == 0, first.stdout

    again = run("cover", "--url", "https://jobs/stranded", "--text", GOOD_COVER)
    assert again.exit_code != 0
    assert payload_of(again)["error"]["code"] == "already_covered"


# ---------------------------------------------------------------------------
# Redaction shape on all three job-next stages
# ---------------------------------------------------------------------------

def test_job_next_materials_default_redacted(seeded_store):
    for stage in ("enrich", "tailor", "cover"):
        result = run("job-next", "--stage", stage)
        assert result.exit_code == 0, result.stdout
        assert RESUME_MARKER not in result.stdout
        assert "resume_facts" not in result.stdout
        payload = payload_of(result)
        assert payload["resume"] == {
            "provided": True,
            "chars": len(f"Test Person — data engineer résumé. {RESUME_MARKER}"),
        }
        assert "text" not in payload["resume"]
        assert payload["progress"]["pending_detail"] == 1


def test_job_next_tailor_reveal_carries_profile_slice(seeded_store):
    revealed = payload_of(run("job-next", "--stage", "tailor", "--reveal"))
    assert RESUME_MARKER in revealed["resume"]["text"]
    assert revealed["resume_facts"]["preserved_school"] == "State University"
    # The tailor slice: skills boundary, facts, education — never personal.
    assert revealed["profile"]["skills_boundary"] == PROFILE["skills_boundary"]
    assert revealed["profile"]["experience"] == PROFILE["experience"]
    assert "personal" not in revealed["profile"]


def test_job_next_enrich_and_cover_reveal_carry_facts(seeded_store):
    for stage in ("enrich", "cover"):
        revealed = payload_of(run("job-next", "--stage", stage, "--reveal"))
        assert RESUME_MARKER in revealed["resume"]["text"]
        assert revealed["resume_facts"]["preserved_projects"] == \
            [f"Built {FACTS_MARKER}"]
        assert "profile" not in revealed  # the tailor slice is tailor-only


# ---------------------------------------------------------------------------
# F2: `@file` arguments — tailor --data and cover --text read from disk
# ---------------------------------------------------------------------------

def test_tailor_data_at_file_persists(seeded_store, tmp_path):
    """README-documented `--data @template.json` works: the file's
    contents flow through the unchanged validation path and persist."""
    _score(seeded_store, "https://jobs/stranded")
    template = tmp_path / "template.json"
    template.write_text(GOOD_TAILOR_DATA, encoding="utf-8")

    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", f"@{template}")
    assert result.exit_code == 0, result.stdout
    assert _row(seeded_store, "https://jobs/stranded")["tailored_resume_path"]


def test_tailor_missing_file_is_typed_usage_refusal(seeded_store, tmp_path):
    """An unreadable @path is an invalid_usage envelope, not a traceback —
    and nothing is persisted."""
    _score(seeded_store, "https://jobs/stranded")
    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", f"@{tmp_path / 'missing.json'}")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_usage"
    assert _row(seeded_store, "https://jobs/stranded")["tailored_resume_path"] is None


def test_tailor_malformed_file_keeps_invalid_resume_data(seeded_store, tmp_path):
    """A readable file with bad JSON keeps the existing refusal semantics —
    post-read validation is unchanged (F2)."""
    _score(seeded_store, "https://jobs/stranded")
    template = tmp_path / "malformed.json"
    template.write_text("not json at all", encoding="utf-8")

    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", f"@{template}")
    assert result.exit_code != 0
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_resume_data"
    assert envelope["error"]["details"]["retryable"] is True


def test_tailor_inline_data_passes_through_unchanged(seeded_store):
    """Regression: an inline value (no @ prefix) behaves exactly as before."""
    _score(seeded_store, "https://jobs/stranded")
    result = run("tailor", "--url", "https://jobs/stranded",
                 "--data", GOOD_TAILOR_DATA)
    assert result.exit_code == 0, result.stdout
    assert _row(seeded_store, "https://jobs/stranded")["tailored_resume_path"]


def test_cover_text_at_file_persists(seeded_store, tmp_path):
    """The same @file vocabulary on cover --text — a long letter is the
    other payload you do not want to inline."""
    _score(seeded_store, "https://jobs/stranded")
    first = run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    assert first.exit_code == 0, first.stdout

    letter = tmp_path / "letter.txt"
    letter.write_text(GOOD_COVER, encoding="utf-8")
    result = run("cover", "--url", "https://jobs/stranded", "--text", f"@{letter}")
    assert result.exit_code == 0, result.stdout
    assert _row(seeded_store, "https://jobs/stranded")["cover_letter_path"]


def test_cover_missing_file_is_typed_usage_refusal(seeded_store, tmp_path):
    _score(seeded_store, "https://jobs/stranded")
    first = run("tailor", "--url", "https://jobs/stranded", "--data", GOOD_TAILOR_DATA)
    assert first.exit_code == 0, first.stdout

    result = run("cover", "--url", "https://jobs/stranded",
                 "--text", f"@{tmp_path / 'missing.txt'}")
    assert result.exit_code != 0
    assert payload_of(result)["error"]["code"] == "invalid_usage"
