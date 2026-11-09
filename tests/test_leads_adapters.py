"""Hiring-lead storage + discovery adapter tests (spec art_u0YGUqpk §3-4).

Pins the acceptance criteria: dedupe on profile_url, daily-cap boundary
with UTC-day reset, all five typed refusals, terminal bot-wall retirement,
redaction of the new provider keys, Firecrawl off by default, and the
structural never-request-linkedin.com guardrail. All HTTP runs over
httpx.MockTransport — never live calls, never real credentials.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import pytest

from kestrel import config
from kestrel.database import (
    LEAD_SOURCES,
    LEAD_STATUSES,
    init_db,
    refresh_company_identity,
    upsert_hiring_lead,
)
from kestrel.discovery import hiring_leads as hl
from kestrel.discovery.hiring_leads import (
    COMPANY_UNRESOLVED,
    NO_HIGH_MATCH_COMPANIES,
    NO_PROVIDER_KEY,
    PROVIDER_UNSUPPORTED,
    QUOTA_EXHAUSTED,
    SOURCE_RETIRED,
    FirecrawlClient,
    FirecrawlPageSource,
    LeadsRefusal,
    SerpClient,
    SerpXRaySource,
    TavilyClient,
    TavilyXRaySource,
    discover_hiring_leads,
    map_serp_result,
    xray_query,
    xray_query_plain,
)

SERP_KEY = "test-serp-key-not-a-real-credential"
TAXONOMY = ("Head of Talent", "Recruiting Manager")
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

ACME = ("https://jobs.acme-robotics.com/posting/1", "Acme Robotics",
        "acme-robotics.com", 90)
BOLT = ("https://boltlabs.wd5.myworkdayjobs.com/en-US/site/job/2", "Bolt Labs",
        "boltlabs.wd5.myworkdayjobs.com", 85)


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


def _profile_result(slug, title="Head of Talent"):
    display = slug.replace("-", " ").title()
    return {
        "position": 1,
        "title": f"{display} - {title} - Acme Robotics | LinkedIn",
        "link": f"https://www.linkedin.com/in/{slug}",
        "snippet": f"{display} — hiring at Acme Robotics.",
    }


def _recording_transport(calls, responder):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(request)

    return httpx.MockTransport(handler)


def _serp_source(responder, calls):
    client = SerpClient(
        SERP_KEY,
        base_url="https://serpapi.test",
        transport=_recording_transport(calls, responder),
        sleep=lambda seconds: None,
    )
    return SerpXRaySource(client, results_per_query=10, taxonomy=TAXONOMY)


def _patch_build_source(monkeypatch, source):
    monkeypatch.setattr(
        hl, "build_source",
        lambda name, *, client=None, sleep=time.sleep: source)


def _hosts(calls):
    return {urlparse(str(request.url)).hostname for request in calls}


def _assert_no_linkedin_host(calls):
    for host in _hosts(calls):
        assert not (host == "linkedin.com" or host.endswith(".linkedin.com")), (
            f"adapter issued a request to linkedin.com host {host}")


# ---------------------------------------------------------------------------
# Storage: hiring_leads table, vocabulary, dedupe
# ---------------------------------------------------------------------------


def test_hiring_leads_table_exists_with_expected_columns():
    conn = init_db()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hiring_leads)")}
    assert columns == {"profile_url", "full_name", "title", "company_key",
                       "source", "evidence_url", "last_status",
                       "discovered_at", "decided_at"}


def test_lead_vocabularies_are_shared_constants():
    assert LEAD_STATUSES == ("discovered", "dismissed", "connected")
    assert LEAD_SOURCES == ("serp", "public_page", "manual")


def test_upsert_hiring_lead_dedupes_on_profile_url():
    conn = init_db()
    lead = dict(profile_url="https://www.linkedin.com/in/jane-doe",
                full_name="Jane Doe", title="Head of Talent",
                company_key="acme-robotics.com", source="serp")
    assert upsert_hiring_lead(conn, **lead) is True
    assert upsert_hiring_lead(conn, **lead) is False
    rows = conn.execute("SELECT * FROM hiring_leads").fetchall()
    assert len(rows) == 1
    assert rows[0]["last_status"] == "discovered"
    assert rows[0]["decided_at"] is None


def test_upsert_rejects_status_or_source_outside_vocabulary():
    conn = init_db()
    base = dict(full_name="Jane Doe", title="Head of Talent",
                company_key="acme-robotics.com")
    with pytest.raises(ValueError, match="lead status"):
        upsert_hiring_lead(conn, source="serp", last_status="maybe",
                           profile_url="https://www.linkedin.com/in/jane-doe",
                           **base)
    with pytest.raises(ValueError, match="lead source"):
        upsert_hiring_lead(conn, source="carrier_pigeon",
                           profile_url="https://www.linkedin.com/in/jane-doe",
                           **base)
    with pytest.raises(ValueError, match="requires"):
        upsert_hiring_lead(conn, source="serp", profile_url="  ", **base)


# ---------------------------------------------------------------------------
# Pure mapping and query construction
# ---------------------------------------------------------------------------


def test_xray_query_scopes_profiles_and_ors_taxonomy():
    query = xray_query("Acme Robotics", TAXONOMY)
    assert query.startswith('site:linkedin.com/in "Acme Robotics"')
    assert '"Head of Talent" OR "Recruiting Manager"' in query


def test_map_serp_result_keeps_only_linkedin_profile_metadata():
    lead = map_serp_result(_profile_result("jane-doe-8b21435"),
                           "acme-robotics.com")
    assert lead == {
        "profile_url": "https://www.linkedin.com/in/jane-doe-8b21435",
        "full_name": "Jane Doe",  # from the profile slug, id suffix stripped
        "title": "Jane Doe 8B21435 - Head of Talent - Acme Robotics",  # verbatim
        "company_key": "acme-robotics.com",
        "source": "serp",
        "evidence_url": None,
    }


@pytest.mark.parametrize("raw", [
    # Company page / post / non-LinkedIn host: metadata noise, not people
    {"link": "https://www.linkedin.com/company/acme-robotics/",
     "title": "Acme Robotics: Jobs | LinkedIn"},
    {"link": "https://www.linkedin.com/posts/sam-1234", "title": "A post"},
    {"link": "https://example.com/in/jane-doe", "title": "Impersonator"},
    {"link": "", "title": "no link"},
])
def test_map_serp_result_skips_non_profile_results(raw):
    with pytest.raises(ValueError):
        map_serp_result(raw, "acme-robotics.com")


def test_map_serp_result_strips_trackers_for_stable_dedupe():
    raw = _profile_result("jane-doe")
    raw["link"] += "?trackingId=abc&ref=x"
    lead = map_serp_result(raw, "acme-robotics.com")
    assert lead["profile_url"] == "https://www.linkedin.com/in/jane-doe"


# ---------------------------------------------------------------------------
# Composio-shape transport: retryable set, clamped backoff, injectable sleep
# ---------------------------------------------------------------------------


def test_serp_client_retries_retryable_status_then_fails_honestly():
    calls, sleeps = [], []
    responder = lambda request: httpx.Response(500)  # noqa: E731
    client = SerpClient(SERP_KEY, base_url="https://serpapi.test",
                        transport=_recording_transport(calls, responder),
                        max_retries=1, sleep=sleeps.append)
    with pytest.raises(hl.HiringLeadsError):
        client.organic_results("q", num=10)
    assert len(calls) == 2  # one retry, then an honest failure
    assert sleeps == [2.0]  # exponential backoff, no hidden sleep


def test_serp_client_clamps_bogus_retry_after_header():
    sleeps = []
    responder = lambda request: httpx.Response(  # noqa: E731
        429, headers={"retry-after": "9999999999"})
    client = SerpClient(SERP_KEY, base_url="https://serpapi.test",
                        transport=httpx.MockTransport(responder),
                        max_retries=1, sleep=sleeps.append)
    with pytest.raises(hl.HiringLeadsError):
        client.organic_results("q", num=10)
    from kestrel.llm import MAX_RATE_LIMIT_WAIT_SECONDS
    assert sleeps == [MAX_RATE_LIMIT_WAIT_SECONDS]


# ---------------------------------------------------------------------------
# Discovery entrypoint: refusals, cap, retirement, dedupe, guardrails
# ---------------------------------------------------------------------------


def test_no_provider_key_refusal_fires_before_any_network_call(app_dir,
                                                               monkeypatch):
    # Default provider is tavily, so its key is the one the refusal names.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERP_API_KEY", raising=False)

    def no_http(*args, **kwargs):
        raise AssertionError("HTTP client constructed before the key check")

    monkeypatch.setattr(hl.httpx, "Client", no_http)
    conn = init_db()
    _seed_companies(conn, ACME)  # company gate passes; the key gate fires
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, now=NOW)
    assert refused.value.code == NO_PROVIDER_KEY
    assert refused.value.details["required_env"] == ["TAVILY_API_KEY"]
    assert refused.value.details["provider"] == "tavily"
    # no_http never tripped: the refusal precedes any client construction.
    assert hl.discovery_runs_used(NOW) == 0  # refusals spend nothing


def test_no_high_match_companies_refusal_names_threshold_and_orphans(app_dir,
                                                                     monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)  # source builds; data gate fires
    conn = init_db()
    conn.execute(
        "INSERT INTO jobs (url, title, fit_score, discovered_at) "
        "VALUES (?, ?, ?, ?)",
        ("https://example.com/job/1", "Engineer", 8, NOW.isoformat()),
    )
    conn.commit()
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, now=NOW)
    assert refused.value.code == NO_HIGH_MATCH_COMPANIES
    assert refused.value.details["min_fit"] == 75
    assert refused.value.details["scored_jobs_without_company"] == 1


def test_company_unresolved_refusal_for_unknown_key(app_dir, monkeypatch):
    _seed_companies(init_db(), ACME)
    monkeypatch.setenv("TAVILY_API_KEY", SERP_KEY)
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(company_key="nowhere.example", now=NOW)
    assert refused.value.code == COMPANY_UNRESOLVED
    assert refused.value.details["company_key"] == "nowhere.example"


def test_quota_exhausted_at_boundary_with_utc_day_reset(app_dir, monkeypatch):
    (app_dir / "leads.yaml").write_text("daily_discovery_cap: 2\n",
                                        encoding="utf-8")
    conn = init_db()
    _seed_companies(conn, ACME)
    responder = lambda request: httpx.Response(200, json={  # noqa: E731
        "organic_results": [_profile_result("jane-doe")]})
    _patch_build_source(monkeypatch, _serp_source(responder, []))

    first = discover_hiring_leads(conn, now=NOW)
    second = discover_hiring_leads(conn, now=NOW)
    assert (first["discovered"], first["guardrails"]["discovery_used_today"]) == (1, 1)
    assert (second["discovered"], second["guardrails"]["discovery_used_today"]) == (0, 2)

    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, now=NOW)
    assert refused.value.code == QUOTA_EXHAUSTED
    assert refused.value.details["daily_discovery_cap"] == 2
    assert refused.value.details["discovery_used_today"] == 2

    # UTC-day reset: the next day the cap is whole again.
    next_day = discover_hiring_leads(conn, now=NOW + timedelta(days=1))
    assert next_day["guardrails"]["discovery_used_today"] == 1


def test_discovery_dedupes_across_runs_on_profile_url(app_dir, monkeypatch):
    conn = init_db()
    _seed_companies(conn, ACME, BOLT)
    responder = lambda request: httpx.Response(200, json={  # noqa: E731
        "organic_results": [_profile_result("jane-doe"),
                            _profile_result("sam-smith")]})
    _patch_build_source(monkeypatch, _serp_source(responder, []))

    first = discover_hiring_leads(conn, now=NOW)
    second = discover_hiring_leads(conn, now=NOW)
    # 2 companies x 2 taxonomy terms; the mock returns the same pair for
    # every query, so within-run collisions also count as skipped_existing.
    assert first["discovered"] == 2 and first["skipped_existing"] == 2
    assert second["discovered"] == 0 and second["skipped_existing"] == 4
    assert conn.execute("SELECT COUNT(*) FROM hiring_leads").fetchone()[0] == 2
    lead = conn.execute("SELECT * FROM hiring_leads LIMIT 1").fetchone()
    assert lead["company_key"] in ("acme-robotics.com",
                                   "boltlabs.wd5.myworkdayjobs.com")
    assert lead["source"] == "serp"


def test_discovery_guardrails_block_rides_every_payload(app_dir, monkeypatch):
    conn = init_db()
    _seed_companies(conn, ACME)
    _patch_build_source(monkeypatch, _serp_source(
        lambda request: httpx.Response(200, json={"organic_results": []}), []))
    payload = discover_hiring_leads(conn, now=NOW)
    assert payload["guardrails"] == {
        "sends_nothing": True,
        "credentials_stored": "none",
        "profile_pages_fetched": False,
        "daily_discovery_cap": 10,
        "discovery_used_today": 1,
    }


@pytest.mark.parametrize("wall", ["http_403", "captcha_body"])
def test_bot_wall_retires_source_terminally(app_dir, monkeypatch, wall):
    def responder(request):
        if wall == "http_403":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, json={"captcha": "unusual traffic"})

    calls = []
    conn = init_db()
    _seed_companies(conn, ACME)
    monkeypatch.setenv("SERP_API_KEY", SERP_KEY)
    _patch_build_source(monkeypatch, _serp_source(responder, calls))

    # Mid-run retirement: the run ends honestly with the source retired.
    payload = discover_hiring_leads(conn, now=NOW)
    assert payload["discovered"] == 0
    assert "serp" in payload["retired_sources"]
    assert len(calls) == 1  # never retried through the wall

    record = hl.retired_sources()
    assert "serp" in record and record["serp"]["reason"]

    # Every later run refuses before any network call.
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, now=NOW)
    assert refused.value.code == SOURCE_RETIRED
    assert "serp" in refused.value.details["retired"]
    assert len(calls) == 1


def test_discovery_never_requests_linkedin_hosts(app_dir, monkeypatch):
    calls = []
    conn = init_db()
    _seed_companies(conn, ACME)
    responder = lambda request: httpx.Response(200, json={  # noqa: E731
        "organic_results": [
            _profile_result("jane-doe"),
            {"link": "https://www.linkedin.com/company/acme-robotics/",
             "title": "Acme Robotics | LinkedIn"},
        ]})
    _patch_build_source(monkeypatch, _serp_source(responder, calls))

    payload = discover_hiring_leads(conn, now=NOW)
    assert payload["discovered"] == 1  # company page skipped, not stored
    assert _hosts(calls) == {"serpapi.test"}
    _assert_no_linkedin_host(calls)


# ---------------------------------------------------------------------------
# Firecrawl: off by default, opt-in, never linkedin.com
# ---------------------------------------------------------------------------


def test_firecrawl_is_off_by_default(app_dir):
    assert config.firecrawl_enabled() is False
    assert config.leads_provider() == "tavily"
    conn = init_db()
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, sources=("firecrawl",), now=NOW)
    assert refused.value.code == NO_PROVIDER_KEY
    assert refused.value.details["required_env"] == ["FIRECRAWL_API_KEY"]
    assert refused.value.details["config"] == "leads.yaml firecrawl.enabled"


def test_firecrawl_opt_in_reads_public_careers_page_only(app_dir):
    calls = []

    def responder(request):
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": ("Our team\n\nDana Reyes, Head of Talent\n\n"
                         "Sam Wu — Recruiting Manager\n\n"
                         "Visit https://example.com/careers to apply.\n")}})

    client = FirecrawlClient("test-firecrawl-key", base_url="https://firecrawl.test",
                             transport=_recording_transport(calls, responder),
                             sleep=lambda seconds: None)
    source = FirecrawlPageSource(client, taxonomy=TAXONOMY, enabled=True)
    company = {"company_key": "acme-robotics.com",
               "display_name": "Acme Robotics", "domain": "acme-robotics.com"}

    leads = source.discover_for_company(company)
    assert [(lead["full_name"], lead["title"]) for lead in leads] == [
        ("Dana Reyes", "Head of Talent"), ("Sam Wu", "Recruiting Manager")]
    for lead in leads:
        assert lead["profile_url"].startswith("https://acme-robotics.com/careers#")
        assert lead["source"] == "public_page"
        assert lead["evidence_url"] == "https://acme-robotics.com/careers"
    assert _hosts(calls) == {"firecrawl.test"}  # the careers page host, only


def test_firecrawl_page_lead_dedupes_and_stores_through_the_one_path(app_dir,
                                                                     monkeypatch):
    conn = init_db()
    _seed_companies(conn, ACME)

    def responder(request):
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "Dana Reyes, Head of Talent\n"}})

    source = FirecrawlPageSource(
        FirecrawlClient("test-firecrawl-key", base_url="https://firecrawl.test",
                        transport=_recording_transport([], responder),
                        sleep=lambda seconds: None),
        taxonomy=TAXONOMY, enabled=True)
    _patch_build_source(monkeypatch, source)

    first = discover_hiring_leads(conn, sources=("firecrawl",), now=NOW)
    second = discover_hiring_leads(conn, sources=("firecrawl",), now=NOW)
    assert first["discovered"] == 1 and second["skipped_existing"] == 1
    lead = conn.execute("SELECT * FROM hiring_leads").fetchone()
    assert lead["source"] == "public_page"
    assert lead["full_name"] == "Dana Reyes"


def test_firecrawl_never_targets_linkedin_even_by_caller_bug(app_dir):
    calls = []
    client = FirecrawlClient("test-firecrawl-key", base_url="https://firecrawl.test",
                             transport=_recording_transport(
                                 calls, lambda request: httpx.Response(200, json={})),
                             sleep=lambda seconds: None)
    source = FirecrawlPageSource(client, taxonomy=("COO",), enabled=True)
    with pytest.raises(ValueError, match="linkedin.com"):
        source.discover_for_company({"company_key": "name:acme",
                                     "display_name": "Acme",
                                     "domain": "linkedin.com"})
    assert calls == []  # refused before a request existed


# ---------------------------------------------------------------------------
# Tavily: the default X-ray provider (plain queries + include_domains)
# ---------------------------------------------------------------------------


def _tavily_result(slug, title="Head of Talent"):
    display = slug.replace("-", " ").title()
    return {
        "title": f"{display} - {title} - Acme Robotics | LinkedIn",
        "url": f"https://www.linkedin.com/in/{slug}",
        "content": f"{display} — hiring at Acme Robotics.",
    }


def _tavily_source(responder, calls, max_results=10):
    client = TavilyClient(
        SERP_KEY,
        base_url="https://api.tavily.test",
        transport=_recording_transport(calls, responder),
        sleep=lambda seconds: None,
    )
    return TavilyXRaySource(client, max_results=max_results,
                            taxonomy=TAXONOMY)


def test_xray_query_plain_is_taxonomy_ors_with_no_site_operator():
    query = xray_query_plain("Acme Robotics", TAXONOMY)
    assert '"Acme Robotics"' in query
    assert '"Head of Talent" OR "Recruiting Manager"' in query
    assert "site:" not in query  # Tavily does not guarantee operators


def test_tavily_request_body_carries_include_domains_and_no_site_prefix():
    calls = []
    source = _tavily_source(
        lambda request: httpx.Response(200, json={"results": []}), calls)
    source.discover_for_company({"company_key": "acme-robotics.com",
                                 "display_name": "Acme Robotics"})
    assert len(calls) == 1
    request = calls[0]
    assert urlparse(str(request.url)).hostname == "api.tavily.test"
    body = json.loads(request.content)
    assert body["include_domains"] == ["linkedin.com"]  # restrict mode
    assert body["max_results"] == 10
    assert body["api_key"] == SERP_KEY
    assert "site:" not in body["query"]


def test_tavily_happy_path_dedupes_on_profile_url(app_dir, monkeypatch):
    conn = init_db()
    _seed_companies(conn, ACME)
    responder = lambda request: httpx.Response(200, json={  # noqa: E731
        "results": [_tavily_result("jane-doe-8b21435"),
                    _tavily_result("sam-smith")]})
    _patch_build_source(monkeypatch, _tavily_source(responder, []))

    first = discover_hiring_leads(conn, now=NOW)
    second = discover_hiring_leads(conn, now=NOW)
    assert first["discovered"] == 2 and second["skipped_existing"] == 2
    lead = conn.execute("SELECT * FROM hiring_leads").fetchone()
    assert lead["profile_url"] == "https://www.linkedin.com/in/jane-doe-8b21435"
    assert lead["source"] == "serp"  # same X-ray surface class, by design


def test_tavily_drops_non_profile_urls_not_trusted(app_dir, monkeypatch):
    conn = init_db()
    _seed_companies(conn, ACME)
    responder = lambda request: httpx.Response(200, json={  # noqa: E731
        "results": [
            _tavily_result("jane-doe"),
            # Provider ignored include_domains or returned a company page:
            # dropped by the host+path gate, never stored.
            {"title": "Acme Robotics | LinkedIn",
             "url": "https://www.linkedin.com/company/acme-robotics/"},
            {"title": "Not even LinkedIn",
             "url": "https://example.com/in/jane-doe"},
            {"title": "No url"},
        ]})
    _patch_build_source(monkeypatch, _tavily_source(responder, []))

    payload = discover_hiring_leads(conn, now=NOW)
    assert payload["discovered"] == 1
    assert conn.execute("SELECT COUNT(*) FROM hiring_leads").fetchone()[0] == 1


def test_tavily_no_provider_key_refusal_names_the_provider(app_dir,
                                                           monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    conn = init_db()
    _seed_companies(conn, ACME)
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, now=NOW)
    assert refused.value.code == NO_PROVIDER_KEY
    assert refused.value.details["provider"] == "tavily"
    assert refused.value.details["required_env"] == ["TAVILY_API_KEY"]
    assert "TAVILY_API_KEY" in str(refused.value)


class _NamedFakeSource:
    name = "set-at-construction"
    source_label = "serp"

    def __init__(self, name):
        self.name = name

    def discover_for_company(self, company):
        return []


@pytest.mark.parametrize("configured,expected", [
    (None, "tavily"),  # the default per the provider decision
    ("serp", "serp"),
    ("tavily", "tavily"),
    ("Tavily", "tavily"),  # normalized, not case-sensitive
])
def test_provider_selection_reads_leads_yaml(app_dir, monkeypatch,
                                             configured, expected):
    if configured is not None:
        (app_dir / "leads.yaml").write_text(f"provider: {configured}\n",
                                            encoding="utf-8")
    requested = []

    def fake_build(name, *, client=None, sleep=time.sleep):
        requested.append(name)
        return _NamedFakeSource(name)

    monkeypatch.setattr(hl, "build_source", fake_build)
    _seed_companies(init_db(), ACME)  # past the company gate
    discover_hiring_leads(now=NOW)
    assert requested == [expected]


def test_provider_unsupported_refusal_for_unknown_provider(app_dir,
                                                           monkeypatch):
    (app_dir / "leads.yaml").write_text("provider: bing\n", encoding="utf-8")
    conn = init_db()
    _seed_companies(conn, ACME)
    with pytest.raises(LeadsRefusal) as refused:
        discover_hiring_leads(conn, now=NOW)
    assert refused.value.code == PROVIDER_UNSUPPORTED
    assert refused.value.details["provider"] == "bing"
    assert set(refused.value.details["known_providers"]) == {
        "serp", "tavily", "firecrawl"}
    assert "bing" in str(refused.value)


def test_tavily_client_retries_retryable_status_then_fails_honestly():
    calls, sleeps = [], []
    responder = lambda request: httpx.Response(500)  # noqa: E731
    client = TavilyClient(SERP_KEY, base_url="https://api.tavily.test",
                          transport=_recording_transport(calls, responder),
                          max_retries=1, sleep=sleeps.append)
    with pytest.raises(hl.HiringLeadsError):
        client.search_results("q", include_domains=["linkedin.com"],
                              max_results=10)
    assert len(calls) == 2  # one retry, then an honest failure
    assert sleeps == [2.0]  # exponential backoff, no hidden sleep


def test_tavily_client_clamps_bogus_retry_after_header():
    sleeps = []
    responder = lambda request: httpx.Response(  # noqa: E731
        429, headers={"retry-after": "9999999999"})
    client = TavilyClient(SERP_KEY, base_url="https://api.tavily.test",
                          transport=httpx.MockTransport(responder),
                          max_retries=1, sleep=sleeps.append)
    with pytest.raises(hl.HiringLeadsError):
        client.search_results("q", include_domains=["linkedin.com"],
                              max_results=10)
    from kestrel.llm import MAX_RATE_LIMIT_WAIT_SECONDS
    assert sleeps == [MAX_RATE_LIMIT_WAIT_SECONDS]


# ---------------------------------------------------------------------------
# Config and redaction
# ---------------------------------------------------------------------------


def test_leads_config_defaults_and_loud_override_refusal(app_dir):
    assert config.leads_daily_discovery_cap() == 10
    assert config.leads_serp_results_per_query() == 10
    assert "Head of Talent" in config.leads_role_taxonomy()

    (app_dir / "leads.yaml").write_text("daily_discovery_cap: banana\n",
                                        encoding="utf-8")
    with pytest.raises(ValueError, match="daily_discovery_cap"):
        config.leads_daily_discovery_cap()


def test_provider_keys_join_the_redaction_boundary(monkeypatch):
    assert "SERP_API_KEY" in config.SECRET_ENV_KEYS
    assert "TAVILY_API_KEY" in config.SECRET_ENV_KEYS
    assert "FIRECRAWL_API_KEY" in config.SECRET_ENV_KEYS
    monkeypatch.setenv("SERP_API_KEY", "serp-secret-value")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-secret-value")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-secret-value")
    values = config.collect_secret_values()
    assert "serp-secret-value" in values
    assert "tavily-secret-value" in values
    assert "firecrawl-secret-value" in values
