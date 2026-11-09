"""Regression tests for the round-3 protocol-audit remediation (findings
doc art_YLnPMn0i, task todo_pmuZ7omd).

Each section maps to a finding:

- F1: the interview state store — atomic persistence (temp file +
  os.replace + fsync), a cross-process turn lock (flock on a sibling
  file, since every `kestrel agent` verb is its own process), and
  load_state distinguishing "no file" from "unparseable" (the torn-read
  probe P2f once reset a live interview with exit 0).
- F2: claim discipline for tailor/cover — the version row and pointer
  reserved inside one BEGIN IMMEDIATE transaction BEFORE file writes;
  the loser of a parallel race gets a typed conflict envelope, never
  internal_error, and the winner's bytes on disk match its payload.
- F3/F6: explicit field-type validation (the P2a probe once passed a
  list-shaped `skills` with watchlist entries and an empty skills
  section) and typed refusals for non-dict --data.
- F5: --min-score refuses "-5"/"--5" (in test_agent_apply_verbs).
- F7: lenient cover validation enforces the hard word cap (2x the
  250-word target) and surfaces word_count.
- F9: Typer usage errors are JSON envelopes on stdout, no Rich prose.
- F10: resume upload sniffs magic bytes and caps file size.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from kestrel import config, database, onboarding
from kestrel.cli import app

from tests.test_agent_materials import DESCRIPTION, GOOD_TAILOR_DATA, PROFILE

runner = CliRunner()


def run(*args):
    """Invoke `kestrel agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# F1 — interview state: atomic writes, cross-process lock, corrupt state
# ---------------------------------------------------------------------------

TORN_STATE = '{"phase": "REVIEW", "answers": {"full_name"'  # truncated JSON


@pytest.fixture()
def torn_state_file(app_dir):
    config.ONBOARDING_STATE_PATH.write_text(TORN_STATE, encoding="utf-8")
    return app_dir


def test_torn_state_file_is_never_a_fresh_store(torn_state_file):
    """The P2f probe: a torn state file once made `agent next` answer
    phase 'gate' with exit 0, and a following `agent start` silently
    rewrote the file as a fresh INTAKE — destroying the interview."""
    result = run("next")
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "state_corrupt"
    assert "corrupt" in envelope["error"]["message"]

    # The destroy path is gone too: `start` must refuse, not rewrite.
    result = run("start")
    assert result.exit_code == 1, result.stdout
    assert payload_of(result)["error"]["code"] == "state_corrupt"
    assert config.ONBOARDING_STATE_PATH.read_text(encoding="utf-8") == TORN_STATE


def test_load_state_distinguishes_missing_from_corrupt(app_dir):
    assert onboarding.load_state() is None  # no file: a fresh store
    config.ONBOARDING_STATE_PATH.write_text(TORN_STATE, encoding="utf-8")
    with pytest.raises(onboarding.OnboardingError, match="corrupt"):
        onboarding.load_state()
    config.ONBOARDING_STATE_PATH.write_text("[]", encoding="utf-8")
    with pytest.raises(onboarding.OnboardingError, match="corrupt"):
        onboarding.load_state()


def test_redo_clears_corrupt_state_and_recovers(torn_state_file, monkeypatch):
    """Redo is the documented recovery from corrupt state: it must get
    past load_state (the deliberate catch), clear the dead file, and —
    without a configured backend — fail with the honest readiness error,
    leaving `next` reading a fresh store instead of a corruption loop."""
    monkeypatch.setattr(onboarding, "STATE_TURN_TIMEOUT", 0.05)
    result = run("redo")
    assert result.exit_code == 1, result.stdout
    assert payload_of(result)["error"]["code"] == "provider_setup"
    assert not config.ONBOARDING_STATE_PATH.exists()
    result = run("next")
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["phase"] == "gate"


def test_save_state_is_atomic_and_leaves_no_temp_files(app_dir):
    state = onboarding.InterviewState(seq=0, phase="intake", backend="host")
    onboarding.save_state(state)
    assert onboarding.load_state().seq == 1
    leftovers = [p.name for p in app_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


_HOLDER = textwrap.dedent("""
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    sys.stderr.write("HELD")
    sys.stderr.flush()
    time.sleep(60)
""")


def test_second_process_gets_busy_while_a_turn_is_held(app_dir, monkeypatch):
    """The flock makes the spec's single-session promise hold across
    processes: a second `kestrel agent` process waits out the timeout
    and gets the structured busy envelope, not a lost update."""
    monkeypatch.setattr(onboarding, "STATE_TURN_TIMEOUT", 0.3)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(onboarding._state_lock_path())],
        stderr=subprocess.PIPE)
    try:
        assert holder.stderr.read(4) == b"HELD"
        result = run("send", "--text", "hi")
        assert result.exit_code == 1, result.stdout
        envelope = payload_of(result)
        assert envelope["error"]["code"] == "busy"
        assert "another surface" in envelope["error"]["message"]
    finally:
        holder.kill()
        holder.wait()
    # Lock released: the same verb no longer reports contention.
    result = run("send", "--text", "hi")
    assert result.exit_code == 1, result.stdout
    assert payload_of(result)["error"]["code"] != "busy"


def test_state_turn_serializes_threads_in_one_process(
        app_dir, monkeypatch, tmp_path):
    """The in-process lock keeps the coexistence guarantee underneath the
    new flock layer: a held turn from a thread still yields the busy
    error (and it is the in-process lock that fires first)."""
    monkeypatch.setattr(onboarding, "STATE_TURN_TIMEOUT", 0.05)
    with onboarding._state_turn():
        with pytest.raises(onboarding.OnboardingError, match="another surface"):
            with onboarding._state_turn():
                pass  # pragma: no cover — the acquire must refuse first


# ---------------------------------------------------------------------------
# F2 — document claims: reserve-then-write, typed conflict for the loser
# ---------------------------------------------------------------------------

@pytest.fixture()
def claim_store(app_dir, isolated_db):
    conn = database.init_db()
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description) "
        "VALUES (?, ?, ?, ?)",
        ("https://x/j1", "Engineer", "acme", "d" * 300))
    conn.commit()
    return conn


def test_claim_reserves_version_row_and_pointer_before_any_write(claim_store, app_dir):
    conn = claim_store
    claim = database.claim_document_version(
        conn, "https://x/j1", database.KIND_RESUME,
        path_for=lambda v: str(app_dir / f"tailored_resumes/j1_v{v}.txt"),
        legacy_path=None,
        validation_status="approved",
        pointer_column="tailored_resume_path",
        stamp_column="tailored_at",
        attempts_column="tailor_attempts",
        history_on=True,
    )
    assert claim.version == 1 and claim.version_id is not None
    job = conn.execute("SELECT * FROM jobs WHERE url = ?", ("https://x/j1",)).fetchone()
    assert job["tailored_resume_path"] == claim.file_path
    versions = database.get_document_versions(conn, "https://x/j1")
    assert len(versions) == 1 and versions[0]["version"] == 1


def test_second_claim_conflicts_and_inserts_nothing(claim_store):
    conn = claim_store
    database.claim_document_version(
        conn, "https://x/j1", database.KIND_RESUME,
        path_for=lambda v: f"/tmp/x_v{v}.txt", legacy_path=None,
        validation_status="approved",
        pointer_column="tailored_resume_path", stamp_column="tailored_at",
        attempts_column="tailor_attempts", history_on=True)
    with pytest.raises(database.DocumentClaimConflict):
        database.claim_document_version(
            conn, "https://x/j1", database.KIND_RESUME,
            path_for=lambda v: f"/tmp/x_v{v}.txt", legacy_path=None,
            validation_status="approved",
            pointer_column="tailored_resume_path", stamp_column="tailored_at",
            attempts_column="tailor_attempts", history_on=True)
    assert len(database.get_document_versions(conn, "https://x/j1")) == 1


def test_claim_history_off_sets_pointer_without_version_rows(claim_store):
    conn = claim_store
    claim = database.claim_document_version(
        conn, "https://x/j1", database.KIND_RESUME,
        path_for=lambda _v: str(claim_store and "/tmp/plain.txt"),
        legacy_path=None,
        validation_status="approved",
        pointer_column="tailored_resume_path",
        stamp_column="tailored_at",
        attempts_column="tailor_attempts",
        history_on=False,
    )
    assert claim.version is None and claim.version_id is None
    job = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?",
                       ("https://x/j1",)).fetchone()
    assert job["tailored_resume_path"] == "/tmp/plain.txt"
    assert database.get_document_versions(conn, "https://x/j1") == []


def test_claim_backfills_legacy_row_inside_the_transaction(claim_store, app_dir):
    legacy = app_dir / "tailored_resumes"
    legacy.mkdir()
    (legacy / "j1.txt").write_text("legacy bytes", encoding="utf-8")
    conn = claim_store
    claim = database.claim_document_version(
        conn, "https://x/j1", database.KIND_RESUME,
        path_for=lambda v: str(legacy / f"j1_v{v}.txt"),
        legacy_path=legacy / "j1.txt",
        validation_status="approved",
        pointer_column="tailored_resume_path",
        stamp_column="tailored_at",
        attempts_column="tailor_attempts",
        history_on=True,
    )
    assert claim.version == 2  # legacy backfill takes version 1 (spec D2)
    versions = database.get_document_versions(conn, "https://x/j1")
    assert [(v["version"], v["validation_status"]) for v in versions] == [
        (1, "legacy"), (2, "approved")]
    assert (legacy / "j1.txt").read_text(encoding="utf-8") == "legacy bytes"


def test_conflicting_claim_writes_no_files(claim_store, monkeypatch):
    """The F2 corruption mechanism: the loser of the race used to write
    its files before failing the version INSERT, clobbering the winner's
    bytes. The claim must refuse before anything touches disk — every
    file-writing primitive is booby-trapped here, so a claim that wrote
    anything (its own file or a caller's) would fail the trap, not the
    assertion."""
    conn = claim_store
    database.claim_document_version(
        conn, "https://x/j1", database.KIND_RESUME,
        path_for=lambda v: f"/tmp/x_v{v}.txt", legacy_path=None,
        validation_status="approved",
        pointer_column="tailored_resume_path", stamp_column="tailored_at",
        attempts_column="tailor_attempts", history_on=True)

    def _boom(*a, **k):
        raise AssertionError("no file writes may happen on a claim path")

    for target in ("pathlib.Path.write_text", "pathlib.Path.write_bytes",
                   "builtins.open", "os.replace"):
        monkeypatch.setattr(target, _boom)
    with pytest.raises(database.DocumentClaimConflict):
        database.claim_document_version(
            conn, "https://x/j1", database.KIND_RESUME,
            path_for=lambda v: f"/tmp/x_v{v}.txt", legacy_path=None,
            validation_status="approved",
            pointer_column="tailored_resume_path", stamp_column="tailored_at",
            attempts_column="tailor_attempts", history_on=True)
    assert len(database.get_document_versions(conn, "https://x/j1")) == 1


def _subprocess_env(app_dir):
    return {**os.environ, "KESTREL_DIR": str(app_dir)}


def _spawn_cli(app_dir, *args):
    return subprocess.Popen(
        [sys.executable, "-m", "kestrel.cli", "agent", *args],
        env=_subprocess_env(app_dir), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)


def _seed_race_target(app_dir, kind):
    """Profile + one scored job both parallel writers want; cover needs a
    tailored resume pointer set (the stage's precondition)."""
    config.write_secret_text(config.PROFILE_PATH, json.dumps(PROFILE))
    conn = database.init_db()
    if kind == "cover":
        conn.execute(
            "INSERT INTO jobs (url, title, site, full_description, fit_score, "
            "tailored_resume_path) VALUES (?, ?, ?, ?, ?, ?)",
            ("https://x/race", "Engineer", "acme", DESCRIPTION, 8, "/tmp/t.txt"))
    else:
        conn.execute(
            "INSERT INTO jobs (url, title, site, full_description, fit_score) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://x/race", "Engineer", "acme", DESCRIPTION, 8))
    conn.commit()


def _race_payloads(kind):
    """Two valid, distinguishable payloads — the marker lands in the
    persisted document's body."""
    marker_a = f"MARKER_A_{kind.upper()}"
    marker_b = f"MARKER_B_{kind.upper()}"
    if kind == "tailor":
        data = json.loads(GOOD_TAILOR_DATA)
        a = json.dumps({**data, "summary": marker_a})
        b = json.dumps({**data, "summary": marker_b})
    else:
        a = f"Dear Hiring Manager,\n\n{marker_a}\n\nTest Person"
        b = f"Dear Hiring Manager,\n\n{marker_b}\n\nTest Person"
    return a, b, marker_a, marker_b


@pytest.mark.parametrize("kind", ["tailor", "cover"])
def test_parallel_writers_winner_bytes_loser_typed_conflict(app_dir, kind):
    """The P4 probe, as a gate: two parallel writer processes on one job
    must leave a persisted file whose bytes match the exit-0 writer's
    payload, while the other exits 1 with a typed, branchable envelope —
    never internal_error, and its bytes never land on disk.

    (app_dir only — the subprocesses read $KESTREL_DIR/kestrel.db,
    so the seed and the writers must share that file. Which payload wins
    the claim race is timing-dependent by design; the test derives the
    winner from the outcomes.)"""
    _seed_race_target(app_dir, kind)
    payload_a, payload_b, marker_a, marker_b = _race_payloads(kind)
    verb = "tailor" if kind == "tailor" else "cover"
    flag = "--data" if kind == "tailor" else "--text"

    procs = [
        _spawn_cli(app_dir, verb, "--url", "https://x/race", flag, payload)
        for payload in (payload_a, payload_b)]
    outcomes = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        outcomes.append((p.returncode, out, err))

    ok = [(rc, out) for rc, out, _ in outcomes if rc == 0]
    lost = [(rc, out, err) for rc, out, err in outcomes if rc != 0]
    assert len(ok) == 1 and len(lost) == 1, outcomes
    envelopes = [json.loads(out or err) for _, out, err in lost]
    loser_codes = {e["error"]["code"] for e in envelopes}
    assert loser_codes <= {"conflict", "already_tailored", "already_covered"}, envelopes

    winner_path = json.loads(ok[0][1])["written"]["path"]
    bodies = [p for p in (app_dir / (
        "tailored_resumes" if kind == "tailor" else "cover_letters")).glob("*.txt")
        if not p.name.endswith(("_JOB.txt", "_REPORT.json"))]
    assert len(bodies) == 1
    assert os.path.realpath(bodies[0]) == os.path.realpath(winner_path)
    body = bodies[0].read_text(encoding="utf-8")
    # The persisted bytes belong to the exit-0 writer — exactly one of the
    # two distinguishable markers, never a blend or the crashed writer's.
    winner_marker = next(m for m in (marker_a, marker_b) if m in body)
    other_marker = marker_b if winner_marker == marker_a else marker_a
    assert other_marker not in body


# ---------------------------------------------------------------------------
# F3/F6 — field types are validated, non-dict data is a typed refusal
# ---------------------------------------------------------------------------

def _seed_tailor_job(app_dir):
    config.write_secret_text(config.PROFILE_PATH, json.dumps(PROFILE))
    conn = database.init_db()
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score) "
        "VALUES (?, ?, ?, ?, ?)",
        ("https://x/t1", "Engineer", "acme", DESCRIPTION, 8))
    conn.commit()


def _tailor(app_dir, data):
    """Run `agent tailor` in a subprocess (a clean import state — the
    in-process runner keeps the thread connection cached across cases)."""
    proc = subprocess.run(
        [sys.executable, "-m", "kestrel.cli", "agent", "tailor",
         "--url", "https://x/t1", "--data", data],
        env=_subprocess_env(app_dir), capture_output=True, text=True,
        timeout=120)
    return proc.returncode, proc.stdout


@pytest.mark.parametrize("mutation,field", [
    ({"skills": ["Rust", "C++", "Django", "Ruby on Rails"]}, "skills"),
    ({"summary": 12345}, "summary"),
    ({"education": ["B.S."]}, "education"),
    ({"experience": "Acme Corp"}, "experience"),
    ({"projects": {"Built": "things"}}, "projects"),
    ({"experience": [{"header": "Acme Corp", "bullets": "not-a-list"}]}, "bullets"),
    ({"experience": [{"subtitle": "2020", "bullets": ["ok"]}]}, "header"),
])
def test_wrong_typed_fields_are_refused_naming_the_field(app_dir, mutation, field):
    """The P2a probe: a list-shaped `skills` carrying four fabrication-
    watchlist entries once passed lenient validation with an EMPTY skills
    section — the type gate makes that refusal unconditional."""
    _seed_tailor_job(app_dir)
    data = json.loads(GOOD_TAILOR_DATA)
    data.update(mutation)
    code, out = _tailor(app_dir, json.dumps(data))
    assert code == 1
    envelope = json.loads(out)
    assert envelope["error"]["code"] == "invalid_resume_data", envelope
    assert field in envelope["error"]["message"] + str(envelope["error"]["details"])


@pytest.mark.parametrize("raw", ["null", "[1, 2]", '"a string"', "42"])
def test_non_dict_data_is_a_typed_refusal_never_internal_error(app_dir, raw):
    """F6: 'null'/scalars/arrays parse fine but detonated as TypeError
    internal_errors deep in validation; the verb refuses them by shape."""
    _seed_tailor_job(app_dir)
    code, out = _tailor(app_dir, raw)
    assert code == 1
    envelope = json.loads(out)
    assert envelope["error"]["code"] == "invalid_resume_data", envelope
    assert "internal_error" not in envelope["error"]["code"]


def test_list_skills_persist_nothing(app_dir):
    """The refusal persists nothing: no pointer, no version row, no files."""
    _seed_tailor_job(app_dir)
    data = json.loads(GOOD_TAILOR_DATA)
    data["skills"] = ["Rust", "C++", "Django", "Ruby on Rails"]
    code, out = _tailor(app_dir, json.dumps(data))
    assert code == 1
    conn = database.init_db()
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url=?",
                       ("https://x/t1",)).fetchone()
    assert row["tailored_resume_path"] is None
    assert database.get_document_versions(conn, "https://x/t1") == []
    assert not (app_dir / "tailored_resumes").exists() or \
        not list((app_dir / "tailored_resumes").glob("*.txt"))


def test_assembler_refuses_unvalidated_shape():
    """Defense in depth: the assembler can never disagree with validation
    about what a field is (an unvalidated wrong-typed skills once rendered
    an empty TECHNICAL SKILLS section)."""
    from kestrel.scoring.tailor import assemble_resume_text
    with pytest.raises(ValueError, match="skills"):
        assemble_resume_text({**json.loads(GOOD_TAILOR_DATA),
                              "skills": ["Rust", "C++"]}, {})
    with pytest.raises(ValueError, match="JSON object"):
        assemble_resume_text(None, {})


def test_valid_data_still_persists_end_to_end(app_dir):
    """The happy path survives the gates: good data persists, version row
    + pointer set, approved."""
    _seed_tailor_job(app_dir)
    code, out = _tailor(app_dir, GOOD_TAILOR_DATA)
    assert code == 0, out
    conn = database.init_db()
    row = conn.execute("SELECT tailored_resume_path FROM jobs WHERE url=?",
                       ("https://x/t1",)).fetchone()
    assert row["tailored_resume_path"] is not None
    versions = database.get_document_versions(conn, "https://x/t1")
    assert len(versions) == 1 and versions[0]["validation_status"] == "approved"
    assert versions[0]["file_path"] == row["tailored_resume_path"]
    with open(row["tailored_resume_path"], encoding="utf-8") as f:
        assert "SUMMARY" in f.read()


# ---------------------------------------------------------------------------
# F7 — the hard word cap holds in lenient mode; word_count is surfaced
# ---------------------------------------------------------------------------

def _letter(n_words):
    body = " ".join(["word"] * (n_words - 5))  # greeting + signature = 5
    return f"Dear Hiring Manager,\n\n{body}\n\nTest Person"


def test_lenient_cover_refuses_over_the_hard_cap(app_dir):
    """The P3a probe: a 363-word letter once persisted in lenient mode
    against the documented 250-word limit. 500 (2x the target) is now a
    hard error in every mode, with word_count in the details."""
    _seed_tailor_job(app_dir)
    conn = database.init_db()
    conn.execute("UPDATE jobs SET tailored_resume_path=? WHERE url=?",
                 ("/tmp/t.txt", "https://x/t1"))
    conn.commit()
    result = run("cover", "--url", "https://x/t1", "--text", _letter(600))
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_cover_letter"
    assert envelope["error"]["details"]["word_count"] == 600
    conn = database.init_db()
    row = conn.execute("SELECT cover_letter_path FROM jobs WHERE url=?",
                       ("https://x/t1",)).fetchone()
    assert row["cover_letter_path"] is None


def test_cover_under_the_cap_still_passes(app_dir):
    _seed_tailor_job(app_dir)
    conn = database.init_db()
    conn.execute("UPDATE jobs SET tailored_resume_path=? WHERE url=?",
                 ("/tmp/t.txt", "https://x/t1"))
    conn.commit()
    result = run("cover", "--url", "https://x/t1", "--text", _letter(240))
    assert result.exit_code == 0, result.stdout


def test_cover_cap_boundaries_in_lenient_mode():
    """Unit pass: 500 accepted, 501 refused, in lenient — the cap is 2x
    the 250-word target, enforced in every mode (audit F7)."""
    from kestrel.scoring.validator import validate_cover_letter
    ok = validate_cover_letter(_letter(500), mode="lenient")
    assert ok["passed"] is True and ok["word_count"] == 500
    over = validate_cover_letter(_letter(501), mode="lenient")
    assert over["passed"] is False
    assert any("Hard cap 500" in e for e in over["errors"])
    assert over["word_count"] == 501


def test_cover_payload_requirements_disclose_the_cap(app_dir):
    result = run("job-next", "--stage", "cover")
    assert result.exit_code == 0, result.stdout
    requirements = payload_of(result)["requirements"]
    assert "hard cap" in requirements and "500" in requirements


# ---------------------------------------------------------------------------
# F9 — Typer usage errors stay inside the JSON wire contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ("job-next", "--stagger"),      # unknown option
    ("job-next", "--stage"),        # missing option value
    ("fly",),                       # unknown command
])
def test_usage_errors_are_json_envelopes(app_dir, args):
    """The parser raises before any verb body — the errors used to be
    Rich-boxed prose with NOTHING on stdout (audit F9). Every one of them
    is now an invalid_usage envelope on stdout, exit 1."""
    result = run(*args)
    assert result.exit_code == 1
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_usage"
    assert envelope["error"]["message"]
    assert envelope["error"]["details"] is None
    assert "╭" not in result.stdout  # no Rich prose anywhere on stdout


def test_bare_invocation_keeps_the_help_affordance(app_dir):
    """Documented decision: `kestrel agent` with no verb still prints
    the help page (exit 2) — a human affordance, not a host path; the
    protocol's host always passes a verb."""
    result = run()
    assert result.exit_code == 2
    assert "Usage" in result.output


# ---------------------------------------------------------------------------
# F10 — resume upload sniffs magic bytes and caps the size
# ---------------------------------------------------------------------------

def _intake_store():
    """A started host interview (resume accepted in intake or interview)."""
    result = run("start")
    assert result.exit_code == 0, result.stdout


def test_zip_magic_docx_refused_as_invalid_resume(app_dir):
    """The P5d probe: a file of ZIP bytes named fake.docx used to become
    THE stored resume, exit 0. It is now a typed refusal and stores
    nothing."""
    _intake_store()
    fake = app_dir / "fake.docx"
    fake.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    result = run("resume", "--file", str(fake))
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_resume", envelope


def test_nul_byte_file_refused(app_dir):
    _intake_store()
    binary = app_dir / "resume.dat"
    binary.write_bytes(b"mostly text\x00with a nul")
    result = run("resume", "--file", str(binary))
    assert result.exit_code == 1, result.stdout
    assert payload_of(result)["error"]["code"] == "invalid_resume"


def test_oversized_resume_refused_before_reading(app_dir):
    _intake_store()
    big = app_dir / "big.txt"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    result = run("resume", "--file", str(big))
    assert result.exit_code == 1, result.stdout
    envelope = payload_of(result)
    assert envelope["error"]["code"] == "invalid_resume"
    assert "cap" in envelope["error"]["message"]


def test_text_resume_still_accepted(app_dir):
    _intake_store()
    good = app_dir / "resume.txt"
    good.write_text("Test Person — data engineer résumé.", encoding="utf-8")
    result = run("resume", "--file", str(good))
    assert result.exit_code == 0, result.stdout
    assert payload_of(result)["needs"]["resume"] is False
