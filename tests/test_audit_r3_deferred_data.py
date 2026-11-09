"""Regression tests for the deferred round-3 DATA/pipeline suggestions
(findings docs art_GxFCvfLI + art_CxX8pt9C, task todo_JoUarQAT — wave 2)
plus the wave-1 verification audit's V3 cap unification (art_G3Mz5okZ).

Each section maps to a finding:

- Pipeline S2 (art_CxX8pt9C): the staged plan token carries the REQUEST
  as made — the confirm compares --limit against the staged
  requested_limit, not the derived batch size, so a faithful repeat of
  an over-requesting --limit no longer drift-refuses.
- Data S1 (art_GxFCvfLI): score-provenance symmetry — host and provider
  arms share the fit_score IS NOT NULL gate, and a provider-scored row
  persists the same provenance shape (scored_by + scored_at) as a
  host-scored one.
- Data S2 (art_GxFCvfLI): status accounting — unscored / scored_fresh /
  score_failed are disjoint and reconcile to the store total; the known
  scored∩score_failed double-bucket (stale score on a failed rescore) is
  documented behavior.
- Data S3 (art_GxFCvfLI): sanitizer hygiene — doubled separators
  collapse, non-ASCII look-alikes transliterate to ASCII, and the pass
  is idempotent.
- Data S4 (art_GxFCvfLI): terminal manual/expired rows are surfaced in
  get_stats AND the portal board.
- Data S6 (art_GxFCvfLI): progress_for_run's wall-clock window is
  documented (no behavior change).
- V3 (art_G3Mz5okZ): the attempts-cap literal is pinned to
  config.DEFAULTS across every use — queue predicates, exhausted-bucket
  counts, and the host verbs' stage_exhausted refusals.
"""

import json
import re

import pytest
from typer.testing import CliRunner

from kestrel import config, database
from kestrel.apply import launcher, planning
from kestrel.cli import app
from kestrel.portal import queries
from kestrel.scoring.docs import _safe_component, output_prefix
from kestrel.scoring.scorer import persist_score, run_scoring

runner = CliRunner()


def _seed_job(conn, url, title="Engineer", **extra):
    cols = {"url": url, "title": title, "site": "acme",
            "full_description": "d" * 300}
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# Pipeline S2 — the confirm compares against the staged REQUEST
# ---------------------------------------------------------------------------


@pytest.fixture()
def queue(app_dir):
    database.init_db()
    return database.get_connection()


@pytest.fixture()
def tier_ok(monkeypatch):
    """The confirm path's Tier-3 gate passes."""
    monkeypatch.setattr(config, "check_tier", lambda tier, feature: None)


def _seed_apply_queue(conn, count=2):
    for i in range(1, count + 1):
        _seed_job(conn, f"https://x.com/jobs/{i}", fit_score=10 - i,
                  tailored_resume_path=f"/tmp/fake/{i}.pdf")


def test_confirm_accepts_a_faithful_over_requesting_limit_repeat(
        queue, seeded_profile, tier_ok, monkeypatch):
    """A plan staged with --limit 10 over 2 eligible jobs runs on a
    faithful `--i-confirm <hash> --limit 10` — the token carries the
    request as made, not the derived batch size."""
    _seed_apply_queue(queue, 2)

    payload = json.loads(runner.invoke(
        app, ["apply", "--plan", "--limit", "10"]).stdout)
    assert payload["batch_size"] == 2  # derived: only 2 eligible

    calls: list[dict] = []
    monkeypatch.setattr(launcher, "main", lambda **kwargs: calls.append(kwargs))
    confirmed = runner.invoke(
        app, ["apply", "--i-confirm", payload["plan_hash"], "--limit", "10"])
    assert confirmed.exit_code == 0, confirmed.stdout
    assert len(calls) == 1
    assert calls[0]["limit"] == 2  # the planned batch runs, not 10


def test_confirm_refuses_a_limit_below_the_staged_request(queue,
        seeded_profile, tier_ok):
    """--limit 3 on a plan staged with --limit 10 is a different request —
    drift refusal, same as any other selection-flag disagreement."""
    _seed_apply_queue(queue, 2)

    payload = json.loads(runner.invoke(
        app, ["apply", "--plan", "--limit", "10"]).stdout)
    refused = runner.invoke(
        app, ["apply", "--i-confirm", payload["plan_hash"], "--limit", "3"])
    assert refused.exit_code == 1
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "plan_drift"
    assert error["details"]["staged_limit"] == 10
    assert error["details"]["given_limit"] == 3


def test_staged_token_carries_the_requested_limit(queue):
    _seed_apply_queue(queue, 1)
    planned = runner.invoke(app, ["apply", "--plan", "--limit", "10"])
    assert planned.exit_code == 0, planned.stdout
    staged = json.loads(planning.plan_path().read_text(encoding="utf-8"))
    assert staged["requested_limit"] == 10


# ---------------------------------------------------------------------------
# Data S1 — provenance symmetry (host arm == provider arm)
# ---------------------------------------------------------------------------


def test_provider_scored_row_has_the_same_provenance_shape_as_host(
        app_dir, isolated_db):
    """persist_score is the single shared write: a provider-scored row
    carries scored_by AND scored_at exactly like a host-scored one, and
    both count in the stats under the same live-score gate."""
    conn = database.init_db()
    _seed_job(conn, "https://jobs/host", fit_score=None)
    _seed_job(conn, "https://jobs/llm", fit_score=None)
    when = "2026-11-09T00:00:00+00:00"
    persist_score(conn, "https://jobs/host", 7, "k", "r", "host", when)
    persist_score(conn, "https://jobs/llm", 9, "k", "r", "openai", when)
    conn.commit()

    for url in ("https://jobs/host", "https://jobs/llm"):
        row = conn.execute(
            "SELECT scored_by, scored_at, fit_score FROM jobs WHERE url = ?",
            (url,)).fetchone()
        assert row["fit_score"] is not None
        assert row["scored_by"] is not None, url
        assert row["scored_at"] == when, url

    stats = database.get_stats(conn)
    assert stats["score_provenance"] == {"host": 1, "provider": 1,
                                         "unattributed": 0}


def test_provenance_requires_a_live_score_on_both_arms(app_dir, isolated_db):
    """S1 symmetry: attribution without a live score counts in NO bucket —
    the same fit_score IS NOT NULL predicate gates host and provider."""
    conn = database.init_db()
    _seed_job(conn, "https://jobs/host-unscored", fit_score=None,
              scored_by="host", scored_at="2026-11-09T00:00:00+00:00")
    _seed_job(conn, "https://jobs/provider-unscored", fit_score=None,
              scored_by="gemini", scored_at="2026-11-09T00:00:00+00:00")

    stats = database.get_stats(conn)
    assert stats["score_provenance"] == {"host": 0, "provider": 0,
                                         "unattributed": 0}


# ---------------------------------------------------------------------------
# Data S2 — score accounting reconciles
# ---------------------------------------------------------------------------


def test_score_buckets_reconcile_to_the_store_total(app_dir, isolated_db):
    conn = database.init_db()
    _seed_job(conn, "https://jobs/undescribed", full_description=None)
    _seed_job(conn, "https://jobs/unscored")
    _seed_job(conn, "https://jobs/fresh", fit_score=8)
    # A failed rescore keeps the previous good score (by design) and
    # carries a live score_error: in BOTH `scored` and `score_failed`.
    _seed_job(conn, "https://jobs/stale", fit_score=7,
              scored_by="openai", score_error="rescore failed")
    _seed_job(conn, "https://jobs/errored-unscored", fit_score=None,
              score_error="scoring failed")

    stats = database.get_stats(conn)
    assert stats["undescribed"] == 1
    assert stats["with_description"] == 4
    # Disjoint partition of the described rows.
    assert (stats["unscored"] + stats["scored_fresh"]
            + stats["score_failed"]) == stats["with_description"]
    assert stats["unscored"] == 1          # described, no score, no error
    assert stats["scored_fresh"] == 1      # live score, no error
    assert stats["score_failed"] == 2      # both error rows
    # The documented double-bucket: the stale-score row counts in `scored`.
    assert stats["scored"] == 2            # fresh + stale
    assert stats["scored"] > stats["scored_fresh"]
    # Full reconciliation to the store total.
    assert stats["undescribed"] + stats["with_description"] == stats["total"]


def test_rescoring_an_undescribed_row_keeps_the_identity_intact(
        app_dir, isolated_db):
    """F1 regression — the audit's exact probe state. A targeted rescore
    of a description-less row used to stamp score_error on it, putting a
    score_failed count on a row outside every described bucket and
    breaking the documented identity. The suite never saw it because the
    identity test's errored rows all carried descriptions."""
    conn = database.init_db()
    _seed_job(conn, "https://jobs/pending-detail", full_description=None)

    result = run_scoring(url="https://jobs/pending-detail")
    assert result["errors"] == 1  # still fails visibly (CLI exit code)
    assert result["scored"] == 0

    row = conn.execute(
        "SELECT score_error FROM jobs WHERE url = 'https://jobs/pending-detail'"
    ).fetchone()
    # Persisted nothing: the row stays pending detail, not score-failed.
    assert row["score_error"] is None

    stats = database.get_stats(conn)
    assert stats["undescribed"] == 1
    assert stats["score_failed"] == 0
    assert stats["with_description"] == 0
    assert (stats["unscored"] + stats["scored_fresh"]
            + stats["score_failed"]) == stats["with_description"]
    assert stats["undescribed"] + stats["with_description"] == stats["total"]


def test_pending_score_stat_matches_the_retried_queue(app_dir, isolated_db):
    """F2: the 'Pending scoring' figure reports the queue run_scoring
    actually drains — described rows without a live score, INCLUDING
    previously-errored ones the queue retries. `unscored` stays the
    narrower never-errored partition bucket."""
    conn = database.init_db()
    _seed_job(conn, "https://jobs/fresh-unscored")
    _seed_job(conn, "https://jobs/errored", fit_score=None, score_error="boom")
    _seed_job(conn, "https://jobs/scored", fit_score=8)
    _seed_job(conn, "https://jobs/undescribed", full_description=None)

    stats = database.get_stats(conn)
    assert stats["pending_score"] == 2       # fresh + errored
    assert stats["pending_score"] == len(database.get_jobs_by_stage(
        conn=conn, stage="pending_score", limit=1000))
    assert stats["unscored"] == 1            # never-errored only
    # The queue reconciles to the partition buckets.
    assert stats["pending_score"] == stats["unscored"] + (
        stats["score_failed"] - (
            stats["scored"] - stats["scored_fresh"]))


# ---------------------------------------------------------------------------
# Data S3 — sanitizer hygiene
# ---------------------------------------------------------------------------


def test_sanitizer_collapses_doubled_separators():
    # The em-dash removal left '__' before S3.
    assert _safe_component("Blockchain Engineer — Platform", 50) == \
        "Blockchain_Engineer_Platform"
    assert "__" not in _safe_component("Senior  --  Dev", 50)
    assert "__" not in _safe_component("A _ B", 50)


def test_sanitizer_transliterates_non_ascii_look_alikes():
    assert _safe_component("Café Engineer", 50) == "Cafe_Engineer"
    assert _safe_component("Zürich — Grüne Rolle", 50) == "Zurich_Grune_Rolle"
    assert _safe_component("résumé", 50) == "resume"


def test_sanitizer_is_idempotent():
    for raw in ("Blockchain Engineer — Platform", "Café  Grüne  Rolle",
                "  trailing — ", "___", "already_clean_title"):
        once = _safe_component(raw, 50)
        assert _safe_component(once, 50) == once


def test_output_prefix_uses_the_hygienic_components():
    prefix = output_prefix({"title": "Blockchain Engineer — Platform",
                            "site": "glassdoor", "url": "https://x/1"})
    assert "__" not in prefix
    assert prefix.startswith("glassdoor_Blockchain_Engineer_Platform_")


# ---------------------------------------------------------------------------
# Data S4 — terminal manual/expired rows surface in stats and the board
# ---------------------------------------------------------------------------


def test_manual_and_expired_rows_surface_in_get_stats(app_dir, isolated_db):
    conn = database.init_db()
    _seed_job(conn, "https://jobs/manual", apply_status="manual")
    _seed_job(conn, "https://jobs/expired", apply_status="expired")
    _seed_job(conn, "https://jobs/other")

    stats = database.get_stats(conn)
    assert stats["manual_marked"] == 1
    assert stats["expired"] == 1


def test_manual_and_expired_rows_surface_on_the_portal_board(
        app_dir, isolated_db):
    conn = database.init_db()
    _seed_job(conn, "https://jobs/manual", apply_status="manual")
    _seed_job(conn, "https://jobs/expired", apply_status="expired")
    _seed_job(conn, "https://jobs/other")

    assert "manual_marked" in queries.STAGES
    assert "expired" in queries.STAGES
    assert [row["url"] for row in queries.filtered_jobs(stage="manual_marked")] \
        == ["https://jobs/manual"]
    assert [row["url"] for row in queries.filtered_jobs(stage="expired")] \
        == ["https://jobs/expired"]
    assert queries.count_filtered_jobs(stage="manual_marked") == 1
    assert queries.count_filtered_jobs(stage="expired") == 1


# ---------------------------------------------------------------------------
# Data S6 — the wall-clock window is documented (no behavior to pin)
# ---------------------------------------------------------------------------


def test_progress_for_run_documents_the_wall_clock_window():
    from kestrel.portal.automation import progress_for_run

    assert progress_for_run.__doc__ is not None
    assert "WALL-CLOCK WINDOW" in progress_for_run.__doc__


# ---------------------------------------------------------------------------
# V3 — the attempts cap is pinned to config.DEFAULTS everywhere
# ---------------------------------------------------------------------------


def _cap_in(sql: str) -> int:
    match = re.search(r"COALESCE\((?:tailor|cover)_attempts, 0\) < (\d+)", sql)
    assert match, f"no attempts-cap predicate in {sql!r}"
    return int(match.group(1))


def test_stage_queue_caps_come_from_config():
    from kestrel.database import STAGE_CONDITIONS

    assert _cap_in(STAGE_CONDITIONS["pending_tailor"]) == \
        config.DEFAULTS["max_tailor_attempts"]
    assert _cap_in(STAGE_CONDITIONS["pending_cover"]) == \
        config.DEFAULTS["max_cover_attempts"]


def test_host_verb_caps_come_from_config():
    import kestrel.agent_protocol as protocol

    assert protocol._TAILOR_ATTEMPT_CAP == \
        config.DEFAULTS["max_tailor_attempts"]
    assert protocol._COVER_ATTEMPT_CAP == \
        config.DEFAULTS["max_cover_attempts"]
    # No literal-cap constant left behind (the V3 deferral's name).
    assert not hasattr(protocol, "_STAGE_ATTEMPT_CAP")


def test_exhausted_buckets_and_queues_agree_on_the_config_cap(
        app_dir, isolated_db):
    """Behavioral pin: a row at the cap is exhausted exactly when its
    attempts reach the config value — counters and queue predicates both."""
    conn = database.init_db()
    cap = config.DEFAULTS["max_tailor_attempts"]
    # Untailored rows for the exhausted counters and the tailor queue; the
    # cover queue additionally requires a tailored resume, so it gets its
    # own under-cap row.
    _seed_job(conn, "https://jobs/at-cap", fit_score=9,
              tailor_attempts=cap, cover_attempts=cap)
    _seed_job(conn, "https://jobs/under", fit_score=9,
              tailor_attempts=cap - 1)
    _seed_job(conn, "https://jobs/under-cover", fit_score=9,
              tailored_resume_path="/tmp/fake/u.pdf",
              tailor_attempts=cap - 1, cover_attempts=cap - 1)

    stats = database.get_stats(conn)
    assert stats["tailor_exhausted"] == 1
    assert stats["cover_exhausted"] == 1

    # The queue predicates agree: the at-cap row is out of both queues.
    tailorable = {row["url"] for row in database.get_jobs_by_stage(
        conn, stage="pending_tailor", min_score=7)}
    coverable = {row["url"] for row in database.get_jobs_by_stage(
        conn, stage="pending_cover", min_score=7)}
    assert "https://jobs/at-cap" not in tailorable
    assert {"https://jobs/under"} == tailorable
    assert "https://jobs/at-cap" not in coverable
    assert {"https://jobs/under-cover"} == coverable


def test_stage_exhausted_envelope_reports_the_config_cap(
        app_dir, isolated_db):
    """The host tailor verb's stage_exhausted envelope carries the cap it
    enforced — the same number config.DEFAULTS pins. (A valid profile so
    the verb gets past onboarding, and valid JSON so the parse succeeds
    and the verb reaches the cap check.)"""
    config.write_secret_text(config.PROFILE_PATH, json.dumps(
        {"personal": {"full_name": "Test Person"}}))
    conn = database.init_db()
    cap = config.DEFAULTS["max_tailor_attempts"]
    _seed_job(conn, "https://jobs/x", fit_score=9, tailor_attempts=cap)

    result = runner.invoke(app, [
        "agent", "tailor", "--url", "https://jobs/x",
        "--data", json.dumps({"resume_text": "irrelevant — the cap fires first"}),
    ])
    assert result.exit_code == 1, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "stage_exhausted"
    assert envelope["error"]["details"]["cap"] == \
        config.DEFAULTS["max_tailor_attempts"]
