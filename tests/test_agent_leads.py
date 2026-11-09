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
