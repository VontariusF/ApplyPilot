"""Regression tests for the wave-2 verification audit's F3 + F4 fixes
(verification doc art_FNsDXnmI, task todo_a3nWuwox).

- F3: the sanitizer's output is ASCII-only — non-decomposable scripts
  (CJK, Cyrillic) become underscores instead of passing through into
  generated document names, and the pass stays idempotent.
- F4: requested_limit is part of the plan hash core — a disk-tampered
  requested_limit fails the token's integrity check instead of making an
  unfaithful --limit repeat look faithful. Legacy tokens hashed without
  requested_limit are refused (re-plan), never accepted.
"""

import json

import pytest
from typer.testing import CliRunner

from applypilot.apply import planning
from applypilot.cli import app
from applypilot.scoring.docs import _safe_component, output_prefix

runner = CliRunner()


def _seed_job(conn, url, title="Engineer", **extra):
    cols = {"url": url, "title": title, "site": "acme",
            "full_description": "d" * 300}
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})",
                 tuple(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# F3 — the sanitizer emits ASCII-only components
# ---------------------------------------------------------------------------

NON_DECOMPOSABLE_INPUTS = [
    "区块链工程师",          # CJK
    "Русский Заголовок",    # Cyrillic
    "Søren upload",         # non-decomposable Latin (ø)
]


@pytest.mark.parametrize("raw", NON_DECOMPOSABLE_INPUTS)
def test_sanitizer_output_is_ascii(raw):
    """No non-decomposable script survives into a document name."""
    assert _safe_component(raw, 50).isascii()


@pytest.mark.parametrize("raw", NON_DECOMPOSABLE_INPUTS + [
    "Café Ünited — Senior Rôle", "Blockchain Engineer — Platform", "résumé",
])
def test_sanitizer_is_idempotent(raw):
    once = _safe_component(raw, 50)
    assert _safe_component(once, 50) == once


def test_sanitizer_pinned_shapes_for_non_latin_titles():
    # The pinned decision (audit F3's shape): surviving non-ASCII becomes
    # an underscore, separator runs collapse, and edges strip.
    assert _safe_component("区块链 Engineer", 50) == "Engineer"
    assert _safe_component("Søren", 50) == "S_ren"
    # A title made of nothing survivable degrades to the placeholder.
    assert _safe_component("区块链工程师", 50) == "none"


def test_output_prefix_is_ascii_for_non_latin_jobs():
    prefix = output_prefix({"title": "区块链 Engineer", "site": "Glassdoor",
                            "url": "https://x.com/jobs/1"})
    assert prefix.isascii()
    assert prefix == "Glassdoor_Engineer_" + prefix.split("_")[-1]


# ---------------------------------------------------------------------------
# F4 — requested_limit is inside the plan hash
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue(app_dir):
    from applypilot import database
    database.init_db()
    return database.get_connection()


@pytest.fixture()
def tier_ok(monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "check_tier", lambda tier, feature: None)


def _seed_apply_queue(conn, count=2):
    for i in range(1, count + 1):
        _seed_job(conn, f"https://x.com/jobs/{i}", fit_score=10 - i,
                  tailored_resume_path=f"/tmp/fake/{i}.pdf")


def test_plan_hash_covers_requested_limit():
    """Only the request size separates these plans — the hash must see it."""
    base = (planning.utc_today(), 7, 40, ["https://x.com/jobs/1"])
    same = (planning.plan_hash(planning._plan_core(*base, 10)),
            planning.plan_hash(planning._plan_core(*base, 10)))
    assert same[0] == same[1]
    assert (planning.plan_hash(planning._plan_core(*base, 10))
            != planning.plan_hash(planning._plan_core(*base, None)))
    assert (planning.plan_hash(planning._plan_core(*base, 10))
            != planning.plan_hash(planning._plan_core(*base, 3)))


def test_confirm_refuses_a_tampered_requested_limit(queue, seeded_profile,
                                                    tier_ok):
    """Editing requested_limit on disk (10 → 3) and confirming --limit 3
    is the F4 exploit: pre-fix it was accepted; now the integrity check
    refuses the token before any flag comparison."""
    _seed_apply_queue(queue, 2)

    payload = json.loads(runner.invoke(
        app, ["apply", "--plan", "--limit", "10"]).stdout)
    token_path = planning.plan_path()
    staged = json.loads(token_path.read_text(encoding="utf-8"))
    assert staged["requested_limit"] == 10
    staged["requested_limit"] = 3
    token_path.write_text(json.dumps(staged, indent=2), encoding="utf-8")

    refused = runner.invoke(
        app, ["apply", "--i-confirm", payload["plan_hash"], "--limit", "3"])
    assert refused.exit_code == 1
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "plan_edited"


def test_confirm_refuses_legacy_tokens_hashed_without_requested_limit(
        queue, seeded_profile, tier_ok):
    """A token staged by a build whose hash core predates requested_limit
    no longer verifies — the deliberate compatibility decision is refuse
    (re-plan), because tokens do not cross UTC days and the old token's
    tamper surface cannot be vouched for."""
    _seed_apply_queue(queue, 2)

    from applypilot import config
    old_core = {"day": planning.utc_today(), "min_score": 7, "cap": 5,
                "urls": ["https://x.com/jobs/1", "https://x.com/jobs/2"]}
    legacy = {**old_core,
              # The old core hashed WITHOUT requested_limit:
              "plan_hash": planning.plan_hash(old_core),
              "staged_at": "2026-11-09T00:00:00+00:00"}
    config.write_secret_text(planning.plan_path(), json.dumps(legacy))

    refused = runner.invoke(
        app, ["apply", "--i-confirm", legacy["plan_hash"]])
    assert refused.exit_code == 1
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "plan_edited"
