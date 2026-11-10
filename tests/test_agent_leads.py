"""Contract tests for the hiring-lead agent verbs (spec art_u0YGUqpk §5,
criteria A4/A5/A7).

`agent leads-discover` and `agent leads-list` are discovery and listing
ONLY: nothing is ever sent, no LinkedIn credential is read, no
linkedin.com page is fetched. The wire contract matches the other agent
verbs — one pure-JSON document on stdout, typed _fail envelopes (here
the five PR #85 refusals) with nonzero exit, personal data redacted
unless --reveal, and the guardrails block riding every payload.
"""

import json
import os
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from kestrel import config
from kestrel.cli import app
from kestrel.database import (
    LEAD_SOURCE_SERP,
    init_db,
    refresh_company_identity,
    upsert_hiring_lead,
)
from kestrel.discovery import hiring_leads as hl

runner = CliRunner()

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
SERP_KEY = "test-serp-key-not-a-real-credential"

ACME = ("https://jobs.acme-robotics.com/posting/1", "Acme Robotics",
        "acme-robotics.com", 90)

BOLT = ("https://jobs.boltlabs.com/posting/2", "Bolt Labs",
        "boltlabs.com", 85)

GUARDRAIL_KEYS = {"sends_nothing", "credentials_stored", "profile_pages_fetched",
                  "daily_discovery_cap", "discovery_used_today",
                  "discovery_remaining_today"}


def run(*args):
    """Invoke `kestrel agent ...` and return the CliRunner result."""
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    """The one JSON document off stdout — the contract under test."""
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _seed_companies(conn, *companies):
    """Scored companies via the merged §1 contract, then materialize §2."""
    for url, company, domain, fit in companies:
        conn.execute(
            "INSERT INTO jobs (url, title, company, company_domain, fit_score, "
            "discovered_at) VALUES (?, ?, ?, ?, ?, ?)",
            (url, f"Senior Engineer at {company}", company, domain, fit,
             NOW.isoformat()),
        )
    conn.commit()
    refresh_company_identity(conn)


def _seed_lead(conn, profile_url, full_name="Jane Doe",
               title="Jane Doe - Head of Talent - Acme Robotics",
               company_key="acme-robotics.com",
               last_status="discovered"):
    assert upsert_hiring_lead(conn, profile_url=profile_url,
                              full_name=full_name, title=title,
                              company_key=company_key, source=LEAD_SOURCE_SERP,
                              discovered_at=NOW.isoformat(),
                              last_status=last_status)


class _FakeSource:
    """Stands in for SerpXRaySource: canned leads, zero network."""

    name = "serp"
    source_label = LEAD_SOURCE_SERP

    def __init__(self, leads, calls, dropped_no_affiliation=0):
        self._leads = leads
        self._calls = calls
        self.dropped_no_affiliation = dropped_no_affiliation

    def discover_for_company(self, company):
        self._calls.append(company["company_key"])
        return [dict(lead, company_key=company["company_key"])
                for lead in self._leads]


def _patch_build_source(monkeypatch, source):
    monkeypatch.setattr(
        hl, "build_source",
        lambda name, *, client=None, sleep=None: source)


_LEADS_REDACTED = "--reveal-gated--"

_LEAD = {"profile_url": "https://www.linkedin.com/in/jane-doe",
         "full_name": "Jane Doe",
         # Headline shape the adapters actually store: the person's
         # name embedded in the title — the masked payload must redact
         # it from here too, not just from full_name.
         "title": "Jane Doe - Head of Talent - Acme Robotics",
         "source": LEAD_SOURCE_SERP, "evidence_url": None}


@pytest.fixture
def conn(app_dir):
    db = init_db()
    _seed_companies(db, ACME)
    yield db


@pytest.fixture
def no_network(monkeypatch):
    """No test may reach a provider: the two fetch seams raise instead.
    Client construction stays real so the refusal paths can build a
    source — the point is that no request is ever issued."""
    def no_fetch(*args, **kwargs):
        raise AssertionError("a lead verb issued a provider request")

    monkeypatch.setattr(hl.SerpClient, "organic_results", no_fetch)
    monkeypatch.setattr(hl.FirecrawlClient, "scrape_markdown", no_fetch)


# ---------------------------------------------------------------------------
# leads-discover: the success contract
# ---------------------------------------------------------------------------


def test_leads_discover_prints_one_pure_json_document(conn, monkeypatch,
                                                      no_network):
    calls = []
    _patch_build_source(monkeypatch,
                        _FakeSource([dict(_LEAD)], calls,
                                    dropped_no_affiliation=2))

    result = run("leads-discover")
    assert result.exit_code == 0
    payload = payload_of(result)  # non-JSON stdout would raise here
    assert payload["discovered"] == 1
    assert payload["skipped_existing"] == 0
    assert payload["companies_scanned"] == 1
    # The affiliation gate's drop counter rides the verb document —
    # the person sees how much noise was filtered, not just what kept.
    assert payload["dropped_no_affiliation"] == 2
    assert len(payload["leads"]) == 1
    assert payload["leads"][0]["company_key"] == "acme-robotics.com"
    assert calls == ["acme-robotics.com"]  # one X-ray query per company


def test_leads_discover_dedupes_and_reports_skipped(conn, monkeypatch,
                                                    no_network):
    _patch_build_source(monkeypatch, _FakeSource([dict(_LEAD)], []))
    assert run("leads-discover").exit_code == 0

    second = run("leads-discover")
    assert second.exit_code == 0
    payload = payload_of(second)
    assert payload["discovered"] == 0
    assert payload["skipped_existing"] == 1
    assert payload["leads"] == []  # nothing new — nothing to show


def test_leads_discover_redacts_personal_data_without_reveal(conn, monkeypatch,
                                                             no_network):
    _patch_build_source(monkeypatch, _FakeSource([dict(_LEAD)], []))

    result = run("leads-discover")
    payload = payload_of(result)
    lead = payload["leads"][0]
    assert lead["full_name"] == _LEADS_REDACTED
    assert lead["profile_url"] == _LEADS_REDACTED
    assert "Jane Doe" not in result.stdout
    assert "linkedin.com/in" not in result.stdout


def test_leads_discover_reveals_personal_data_with_reveal(conn, monkeypatch,
                                                          no_network):
    _patch_build_source(monkeypatch, _FakeSource([dict(_LEAD)], []))

    payload = payload_of(run("leads-discover", "--reveal"))
    lead = payload["leads"][0]
    assert lead["full_name"] == "Jane Doe"
    assert lead["profile_url"] == "https://www.linkedin.com/in/jane-doe"


def test_leads_discover_honors_company_and_limit(conn, monkeypatch, no_network):
    conn.execute(
        "INSERT INTO jobs (url, title, company, company_domain, fit_score, "
        "discovered_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("https://boltlabs.wd5.myworkdayjobs.com/en-US/site/job/2",
         "Engineer at Bolt Labs", "Bolt Labs",
         "boltlabs.wd5.myworkdayjobs.com", 85, NOW.isoformat()),
    )
    conn.commit()
    refresh_company_identity(conn)
    calls = []
    _patch_build_source(monkeypatch, _FakeSource([], calls))

    result = run("leads-discover", "--company=acme-robotics.com")
    assert result.exit_code == 0
    assert payload_of(result)["companies_scanned"] == 1
    assert calls == ["acme-robotics.com"]

    calls.clear()
    limited = run("leads-discover", "--limit=1")
    assert limited.exit_code == 0
    assert payload_of(limited)["companies_scanned"] == 1
    assert len(calls) == 1  # highest fit first (Acme, 90)


def test_leads_discover_refuses_limit_zero_without_recording_a_run(
        conn, monkeypatch, no_network):
    _patch_build_source(monkeypatch, _FakeSource([dict(_LEAD)], []))
    result = run("leads-discover", "--limit=0")
    assert result.exit_code == 1
    payload = payload_of(result)
    assert payload["error"]["code"] == "invalid_limit"
    # A run that reaches no provider must not count against the daily
    # cap — a zero company scan is a refusal, not a spend (sweep
    # art_Izev3UNN suggestion 1).
    assert hl.discovery_runs_used() == 0


# ---------------------------------------------------------------------------
# The guardrails block rides every payload (spec criterion A7)
# ---------------------------------------------------------------------------


def test_guardrails_block_rides_every_success_payload(conn, monkeypatch,
                                                      no_network):
    _patch_build_source(monkeypatch, _FakeSource([dict(_LEAD)], []))

    discover = payload_of(run("leads-discover"))
    assert discover["guardrails"]["sends_nothing"] is True
    assert discover["guardrails"]["credentials_stored"] == "none"
    assert discover["guardrails"]["profile_pages_fetched"] is False
    assert GUARDRAIL_KEYS <= set(discover["guardrails"])
    assert discover["guardrails"]["discovery_used_today"] == 1
    assert (discover["guardrails"]["discovery_remaining_today"]
            == discover["guardrails"]["daily_discovery_cap"] - 1)

    listing = payload_of(run("leads-list"))
    assert listing["guardrails"]["sends_nothing"] is True
    assert listing["guardrails"]["credentials_stored"] == "none"
    assert listing["guardrails"]["profile_pages_fetched"] is False
    assert GUARDRAIL_KEYS <= set(listing["guardrails"])


def test_guardrails_block_rides_every_refusal_envelope(conn, monkeypatch,
                                                       no_network):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)  # default provider

    result = run("leads-discover")  # no key — refusal
    assert result.exit_code != 0
    payload = payload_of(result)
    assert payload["error"]["code"] == "no_provider_key"
    assert payload["error"]["details"]["guardrails"]["sends_nothing"] is True
    assert GUARDRAIL_KEYS <= set(payload["error"]["details"]["guardrails"])


# ---------------------------------------------------------------------------
# All five typed refusals surface as _fail envelopes, exit nonzero (A4)
# ---------------------------------------------------------------------------


def test_no_provider_key_refusal(conn, monkeypatch, no_network):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)  # default provider

    result = run("leads-discover")
    assert result.exit_code == 1
    payload = payload_of(result)
    assert payload["error"]["code"] == "no_provider_key"
    assert payload["error"]["details"]["required_env"] == ["TAVILY_API_KEY"]


def test_no_high_match_companies_refusal(app_dir, monkeypatch, no_network):
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)  # source builds; data gate fires
    init_db()  # no scored companies at all

    result = run("leads-discover")
    assert result.exit_code == 1
    payload = payload_of(result)
    assert payload["error"]["code"] == "no_high_match_companies"
    assert payload["error"]["details"]["min_fit"] == (
        config.leads_min_company_fit())
    assert "score more jobs" in payload["error"]["message"]


def test_company_unresolved_refusal(conn, monkeypatch, no_network):
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)

    result = run("leads-discover", "--company=nowhere.example")
    assert result.exit_code == 1
    payload = payload_of(result)
    assert payload["error"]["code"] == "company_unresolved"
    assert payload["error"]["details"]["company_key"] == "nowhere.example"


def test_bad_leads_yaml_is_the_config_invalid_refusal_not_internal_error(
        app_dir, conn, monkeypatch, no_network):
    """A malformed leads.yaml value used to raise a bare ValueError that
    _verb turned into internal_error — breaking even the read-only
    leads-list verb (sweep art_Izev3UNN suggestion 2). The documented
    typed refusal instead."""
    (app_dir / "leads.yaml").write_text("daily_discovery_cap: not-a-number\n")
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)

    listing = run("leads-list")
    assert listing.exit_code == 1
    payload = payload_of(listing)
    assert payload["error"]["code"] == "config_invalid"
    assert "leads.yaml" in payload["error"]["message"]

    discover = run("leads-discover")
    assert discover.exit_code == 1
    assert payload_of(discover)["error"]["code"] == "config_invalid"


def test_bad_leads_yaml_depth_refuses_at_source_build(app_dir, conn,
                                                      monkeypatch,
                                                      no_network):
    (app_dir / "leads.yaml").write_text(
        "tavily:\n  search_depth: deepest\n")
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)

    with pytest.raises(hl.LeadsRefusal) as refused:
        hl.discover_hiring_leads(conn)
    assert refused.value.code == "config_invalid"


def test_record_discovery_run_survives_a_failed_write_intact(
        app_dir, monkeypatch):
    """The counter rewrite is atomic: a crash mid-write leaves the
    previous count readable and no temp litter behind — a half-written
    discovery-usage.json was read as a reset-to-zero cap hole (sweep
    art_Izev3UNN suggestion 3)."""
    hl._record_discovery_run(NOW)
    hl._record_discovery_run(NOW)
    assert hl.discovery_runs_used(NOW) == 2

    def failing_replace(src, dst):
        raise OSError("simulated crash mid-write")

    # Patch os.replace by hand and restore it precisely: monkeypatch.undo()
    # would also tear down the app_dir fixture's path redirections.
    real_replace = os.replace
    os.replace = failing_replace
    try:
        with pytest.raises(OSError, match="simulated crash"):
            hl._record_discovery_run(NOW)
    finally:
        os.replace = real_replace
    assert hl.discovery_runs_used(NOW) == 2  # previous value intact
    assert not list(app_dir.glob("*.tmp"))  # no temp litter


def test_concurrent_run_recording_loses_no_counts(app_dir):
    """The CLI and the portal record as separate processes over one
    APP_DIR; the lock serializes the read-modify-write so concurrent
    runs each count (sweep art_Izev3UNN suggestion 3)."""
    import threading

    threads = [threading.Thread(target=hl._record_discovery_run, args=(NOW,))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert hl.discovery_runs_used(NOW) == 8


def test_unknown_source_name_is_the_typed_refusal(conn, no_network):
    """An explicitly requested unknown source must come back as the
    documented typed refusal, not a bare ValueError that _verb turns
    into internal_error (sweep art_Izev3UNN suggestion 8)."""
    with pytest.raises(hl.LeadsRefusal) as refused:
        hl.discover_hiring_leads(conn, sources=("nonsense",))
    assert refused.value.code == "provider_unsupported"
    assert "nonsense" in str(refused.value)
    assert "tavily" in refused.value.details["known_providers"]


def test_quota_exhausted_refusal_with_utc_cap(conn, monkeypatch, no_network,
                                              app_dir):
    (app_dir / "leads.yaml").write_text("daily_discovery_cap: 1\n",
                                        encoding="utf-8")
    _patch_build_source(monkeypatch, _FakeSource([], []))

    first = run("leads-discover")
    assert first.exit_code == 0

    second = run("leads-discover")  # cap is 1 — the second run refuses
    assert second.exit_code == 1
    payload = payload_of(second)
    assert payload["error"]["code"] == "quota_exhausted"
    assert payload["error"]["details"]["daily_discovery_cap"] == 1
    assert payload["error"]["details"]["discovery_used_today"] == 1


def test_source_retired_refusal(conn, monkeypatch, no_network):
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)
    hl.retire_source("tavily", "test wall", now=NOW)  # durable, session-long

    result = run("leads-discover")
    assert result.exit_code == 1
    payload = payload_of(result)
    assert payload["error"]["code"] == "source_retired"
    assert "tavily" in payload["error"]["details"]["retired"]


# ---------------------------------------------------------------------------
# leads-list: the queue document
# ---------------------------------------------------------------------------


def test_leads_list_lists_stored_leads(conn):
    _seed_lead(conn, "https://www.linkedin.com/in/jane-doe")
    _seed_lead(conn, "https://www.linkedin.com/in/john-smith",
               full_name="John Smith", last_status="dismissed")
    conn.commit()

    payload = payload_of(run("leads-list"))
    assert payload["count"] == 2
    assert {lead["status"] for lead in payload["leads"]} == {
        "discovered", "dismissed"}


def test_leads_list_status_filter(conn):
    _seed_lead(conn, "https://www.linkedin.com/in/jane-doe")
    _seed_lead(conn, "https://www.linkedin.com/in/john-smith",
               full_name="John Smith", last_status="dismissed")
    conn.commit()

    payload = payload_of(run("leads-list", "--status=dismissed"))
    assert payload["count"] == 1
    assert payload["leads"][0]["status"] == "dismissed"

    payload = payload_of(run("leads-list", "--company=nowhere.example"))
    assert payload["count"] == 0
    assert payload["leads"] == []


def test_leads_list_rejects_status_outside_the_vocabulary(conn):
    result = run("leads-list", "--status=replied")
    assert result.exit_code == 1
    payload = payload_of(result)
    assert payload["error"]["code"] == "invalid_status"
    assert payload["error"]["details"]["valid"] == [
        "discovered", "dismissed", "connected"]


def test_leads_list_redacts_without_reveal_and_reveals_with_it(conn):
    _seed_lead(conn, "https://www.linkedin.com/in/jane-doe")
    conn.commit()

    masked = payload_of(run("leads-list"))
    lead = masked["leads"][0]
    assert lead["full_name"] == _LEADS_REDACTED
    assert lead["profile_url"] == _LEADS_REDACTED
    assert "Jane Doe" not in masked
    assert "linkedin.com/in" not in masked

    revealed = payload_of(run("leads-list", "--reveal"))
    lead = revealed["leads"][0]
    assert lead["full_name"] == "Jane Doe"
    assert lead["profile_url"] == "https://www.linkedin.com/in/jane-doe"


def test_leads_list_masks_name_variant_in_title(conn):
    # 2026-11-10 live run: the title carries 'Julia Casey' while the
    # slug-parsed full_name is 'Julia Casey Az' — the exact-string
    # scrubber missed it and the masked output leaked the name.
    _seed_lead(conn, "https://www.linkedin.com/in/julia-casey-az",
               full_name="Julia Casey Az",
               title="Julia Casey - Senior Business Recruiter @ Brex "
                     "| Strategic Talent")
    conn.commit()

    masked = payload_of(run("leads-list"))
    lead = masked["leads"][0]
    assert lead["title"].startswith(_LEADS_REDACTED)
    assert "Julia" not in masked and "Casey" not in masked
    assert "Julia" not in run("leads-list").stdout

    # The store keeps the raw title (sweep finding 4 removed the
    # destructive startup backfill), so --reveal shows the headline as
    # the provider served it — masking lives in the read path alone.
    revealed = payload_of(run("leads-list", "--reveal"))
    assert revealed["leads"][0]["title"] == (
        "Julia Casey - Senior Business Recruiter @ Brex "
        "| Strategic Talent")


# ---------------------------------------------------------------------------
# Sweep art_Izev3UNN regressions: findings 1 and 5 (name variants in
# scrub) and finding 2 (punctuation-tolerant attribution gate)
# ---------------------------------------------------------------------------


def test_leads_list_masks_reversed_name_order_in_title(conn):
    # Sweep finding 1 (High): a provider headline that REVERSES the
    # name ('Green Anne' for slug-parsed 'Anne Green') matched no
    # variant — the First-Last pair only ever matches 'First Last'
    # order — so the real name rode the masked JSON. The Last-First
    # pair closes the leak.
    _seed_lead(conn, "https://www.linkedin.com/in/anne-green",
               full_name="Anne Green",
               title="Green Anne | Head of Talent | LinkedIn")
    conn.commit()

    masked = payload_of(run("leads-list"))
    lead = masked["leads"][0]
    assert lead["title"].startswith(_LEADS_REDACTED)
    assert "Anne" not in masked and "Green" not in masked


def test_leads_list_masks_slug_separated_name_tokens_in_title(conn):
    # Sweep finding 5 (Medium): display_name_from_slug reads [-_+] as
    # token separators, but variant matching did not — a hyphenated
    # headline kept the surname in the clear
    # ('--reveal-gated---Smith - Talent').
    _seed_lead(conn, "https://www.linkedin.com/in/jane-doe-smith",
               full_name="Jane Doe Smith",
               title="Jane-Doe-Smith - Talent Partner @ Brex")
    conn.commit()

    masked = payload_of(run("leads-list"))
    lead = masked["leads"][0]
    assert lead["title"].startswith(_LEADS_REDACTED)
    assert "Doe" not in masked and "Smith" not in masked

    # The docstring contract survives the separator widening: only a
    # MULTI-token match identifies the person. A bare single token is
    # never a variant (a coincidental 'Doe' passes untouched), and a
    # token fused into a longer word is not whole-word.
    assert hl.scrub_lead_title("Doe - Talent Partner @ Brex",
                               "Jane Doe Smith") == \
        "Doe - Talent Partner @ Brex"
    assert hl.scrub_lead_title("JaneDoeSmith - Recruiter",
                               "Jane Doe Smith") == \
        "JaneDoeSmith - Recruiter"
    # Pure and idempotent: scrubbing an already-scrubbed title is a
    # no-op.
    once = hl.scrub_lead_title("Jane-Doe-Smith - Talent Partner @ Brex",
                               "Jane Doe Smith")
    assert hl.scrub_lead_title(once, "Jane Doe Smith") == once


def test_affiliation_gate_tolerates_trailing_punctuation_in_company_name():
    # Sweep finding 2 (High): the needle built from 'Acme, Inc.' ended
    # in a literal '.', and \b after a non-word char can never fire —
    # every title was dropped as no-affiliation and the company
    # yielded zero leads.
    role = hl.title_affiliation_role(
        title="Jane Doe - Head of Talent at Acme, Inc. | LinkedIn",
        display_name="Acme, Inc.", domain=None,
        taxonomy=("Head of Talent", "Recruiter"))
    assert role == "Head of Talent"

    # Whole-word semantics survive the normalization: 'inc' cannot
    # complete inside a longer word.
    role = hl.title_affiliation_role(
        title="Jane Doe - Head of Talent at Acme, Incoming | LinkedIn",
        display_name="Acme, Inc.", domain=None,
        taxonomy=("Head of Talent", "Recruiter"))
    assert role is None


# ---------------------------------------------------------------------------
# Sweep art_Izev3UNN regressions: findings 3 (daily-cap accounting on
# provider errors) and 4 (non-destructive title backfill)
# ---------------------------------------------------------------------------


class _ErrorSource:
    """A source whose provider scan fails on the first company."""

    name = "serp"
    source_label = LEAD_SOURCE_SERP

    def __init__(self, calls):
        self._calls = calls

    def discover_for_company(self, company):
        self._calls.append(company["company_key"])
        if len(self._calls) == 1:
            return [dict(_LEAD, company_key=company["company_key"])]
        raise hl.HiringLeadsError("provider 500 — upstream quota exhausted")


def test_provider_error_still_records_the_daily_run(conn, monkeypatch,
                                                    no_network, app_dir):
    # Sweep finding 3 (Medium): a non-SourceRetired provider error
    # aborted the scan before _record_discovery_run, so a persistently
    # failing source burned provider spend the daily cap never counted
    # and every retry rescanned all companies from the top. The failed
    # run reached the provider-scan stage — it counts.
    (app_dir / "leads.yaml").write_text("daily_discovery_cap: 1\n",
                                        encoding="utf-8")
    _seed_companies(conn, BOLT)  # two companies — the scan errors on the 2nd
    calls = []
    _patch_build_source(monkeypatch, _ErrorSource(calls))

    first = run("leads-discover")
    assert first.exit_code == 1
    assert payload_of(first)["error"]["code"] == "source_error"
    assert hl.discovery_runs_used() == 1

    second = run("leads-discover")
    assert second.exit_code == 1
    payload = payload_of(second)
    # Cap 1 is spent — the retry refuses instead of rescanning.
    assert payload["error"]["code"] == "quota_exhausted"
    assert payload["error"]["details"]["discovery_used_today"] == 1


def test_source_error_envelope_carries_partial_counts(conn, monkeypatch,
                                                      no_network):
    # Sweep finding 3: leads upserted from the companies scanned before
    # the error went unreported — the envelope carried no counts at
    # all, so the host could not see what the partial run had stored.
    _seed_companies(conn, BOLT)  # two companies — the scan errors on the 2nd
    calls = []
    _patch_build_source(monkeypatch, _ErrorSource(calls))

    result = run("leads-discover")
    assert result.exit_code == 1
    error = payload_of(result)["error"]
    assert error["code"] == "source_error"
    # One company scanned (and its lead upserted) before the failure.
    assert error["details"]["discovered"] == 1
    assert error["details"]["skipped_existing"] == 0
    assert error["details"]["sources_used"] == ["serp"]
    stored = conn.execute("SELECT COUNT(*) FROM hiring_leads").fetchone()[0]
    assert stored == 1


def test_init_db_preserves_stored_titles_while_masked_output_scrubs(conn):
    # Sweep finding 4 (Medium): the startup backfill rewrote stored
    # titles to mask tokens while full_name stayed raw in the same row
    # — the deliberately unmasked portal board rendered mask tokens
    # beside clear names, and the store lost its headline. The store
    # keeps the raw title; masking is the read path's job.
    _seed_lead(conn, "https://www.linkedin.com/in/julia-casey-az",
               full_name="Julia Casey Az",
               title="Julia Casey - Senior Business Recruiter @ Brex")
    conn.commit()

    fresh = init_db()  # the startup hook must not rewrite stored titles
    row = fresh.execute(
        "SELECT title FROM hiring_leads WHERE profile_url = ?",
        ("https://www.linkedin.com/in/julia-casey-az",),
    ).fetchone()
    assert row["title"] == \
        "Julia Casey - Senior Business Recruiter @ Brex"

    masked = payload_of(run("leads-list"))
    lead = masked["leads"][0]
    assert lead["title"].startswith(_LEADS_REDACTED)
    assert "Julia" not in masked and "Casey" not in masked

    # --reveal shows the raw stored headline, not a backfill token.
    revealed = payload_of(run("leads-list", "--reveal"))
    assert revealed["leads"][0]["title"] == \
        "Julia Casey - Senior Business Recruiter @ Brex"


# ---------------------------------------------------------------------------
# Flag validation — typed envelopes, never usage prose
# ---------------------------------------------------------------------------


def test_invalid_min_fit_and_limit_refusals(conn):
    result = run("leads-discover", "--min-fit=high")
    assert result.exit_code == 1
    assert payload_of(result)["error"]["code"] == "invalid_min_fit"

    result = run("leads-discover", "--limit=-3")
    assert result.exit_code == 1
    assert payload_of(result)["error"]["code"] == "invalid_limit"


# ---------------------------------------------------------------------------
# Hint contract — the 'next' pointers name the installed CLI
# ---------------------------------------------------------------------------


def test_verb_next_hints_name_the_installed_cli(conn, monkeypatch, no_network):
    """Regression: the 'next' hints must name the full mounted command
    path, `kestrel agent <verb>` — the agent verbs are a Typer sub-app
    (cli.py add_typer(agent_app, name='agent')), so a bare `kestrel
    leads-list` is not a runnable invocation (follow-up to 7b5295a)."""
    _patch_build_source(monkeypatch, _FakeSource([dict(_LEAD)], []))

    discover = payload_of(run("leads-discover"))
    listing = payload_of(run("leads-list"))

    assert "`kestrel agent leads-list`" in discover["next"]
    assert "`kestrel agent leads-list --reveal`" in listing["next"]
    # And never a bare or package-name invocation of an agent verb.
    assert "kestrel-agent" not in discover["next"]
    assert "kestrel-agent" not in listing["next"]
    assert "`kestrel leads-" not in discover["next"]
    assert "`kestrel leads-" not in listing["next"]
