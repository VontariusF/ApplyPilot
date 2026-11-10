"""Deliverable E tests: Composio discovery source (web3.career).

One test per acceptance criterion from the spec addendum (blueprint
art_BmQxJjjF, Deliverable E). All HTTP interaction runs over fake transports
(httpx.MockTransport) — never live calls, never real credentials. The
fixtures reproduce the shapes captured in the live spike (spike-composio):
envelope failure, platform errors, the Keyrock/CoinList listings, and the
include_description=false null-field shape.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import httpx
import pytest

import kestrel.discovery.composio as composio
from kestrel.discovery.composio import (
    DEFAULT_USER_ID,
    TOOL_REGISTRY,
    ComposioClient,
    ComposioError,
    map_web3_job,
    store_web3_jobs,
)
from kestrel.database import init_db

API_KEY = "test-key-not-a-real-credential"

# ---------------------------------------------------------------------------
# Fixtures captured from the live spike (spike-composio §3d)
# ---------------------------------------------------------------------------

KEYROCK_JOB = {
    "apply_url": "https://web3.career/r/yYDO1UTM__x9qMN8",
    "id": 155862,
    "title": "Innovation Quant Developer: Intern - DeFi/Corporate Development",
    "company": "Keyrock",
    "city": "brussels",
    "country": "belgium",
    "is_remote": True,
    "location": " Belgium Brussels Belgium",  # leading space, as served
    "date": "Thu, 29 Oct 2026 18:41:18 +0000",
    "date_epoch": 1793299278,
    "description": (
        "<p><strong>Innovation Quant Developer: Intern</strong></p>"
        "<p>You will join Keyrock's pricing team to build quantitative models "
        "for digital asset markets, working alongside senior researchers.</p>"
        "<p>Requirements: Rust, Solidity, and familiarity with DeFi protocols.</p>"
    ),
    "salary_currency": None,
    "salary_unit": None,
    "salary_min_value": None,
    "salary_max_value": None,
    "estimated_avg_salary": 172000,
    "estimated_min_salary": 110000,
    "estimated_max_salary": 247500,
    "tags": ["dev", "intern", "entry-level", "blockchain", "crypto", "defi",
             "rust", "solidity", "remote"],
}

COINLIST_JOB = {
    "apply_url": "https://web3.career/r/coinlist-example",
    "id": 155815,
    "title": "Senior Smart Contract Engineer",
    "company": "CoinList",
    "city": None,
    "country": "united-states",
    "is_remote": False,
    "location": " United States",
    "date": "Wed, 28 Oct 2026 10:00:00 +0000",
    "date_epoch": 1793268000,
    "description": None,
    "salary_currency": "USD",
    "salary_unit": "YEAR",
    "salary_min_value": 170000,
    "salary_max_value": 190000,
    "estimated_avg_salary": None,
    "estimated_min_salary": None,
    "estimated_max_salary": None,
    "tags": ["smart-contract"],
}

# include_description=false shape (spike §3e.5): keys present, values null
NO_DESCRIPTION_JOB = {
    "apply_url": "https://web3.career/r/au-example",
    "id": None,
    "title": "Blockchain Developer",
    "company": None,
    "city": None,
    "country": None,
    "is_remote": None,
    "location": " Australia",
    "date": "Mon, 26 Oct 2026 08:00:00 +0000",
    "date_epoch": None,
    "description": None,
    "salary_currency": None,
    "salary_unit": None,
    "salary_min_value": None,
    "salary_max_value": None,
    "estimated_avg_salary": 120000,
    "estimated_min_salary": None,
    "estimated_max_salary": None,
    "tags": None,
}


def _ok_response(jobs, **extra):
    """A successful execute envelope (spike §3c/§3f) — for HTTP-level tests."""
    data = {
        "result_count": extra.pop("result_count", len(jobs)),
        "attribution": "Use each job's apply_url unchanged for every user-facing application link.",
        "jobs": jobs,
    }
    data.update(extra)
    return {"successful": True, "error": None, "log_id": "log_test", "data": data}


def _ok_data(jobs, **extra):
    """The unwrapped ``data`` payload execute_tool returns — for the fake client,
    which sits at the same contract boundary as the real one."""
    return _ok_response(jobs, **extra)["data"]


@pytest.fixture()
def clean_composio_env(monkeypatch):
    """Remove all Composio env vars so tests are hermetic."""
    for var in ("COMPOSIO_API_KEY", "COMPOSIO_TOOLKITS", "COMPOSIO_USER_ID",
                "COMPOSIO_CONNECTED_ACCOUNT_ID"):
        monkeypatch.delenv(var, raising=False)


class _FakeClient:
    """Records calls and returns canned data — the run-flow test double."""

    def __init__(self, session_tools=None, data=None, error=None):
        self.sessions: list[tuple[str, list[str]]] = []
        self.executes: list[dict] = []
        self.closed = False
        self._session_tools = session_tools
        self._data = data
        self._error = error

    def create_session(self, toolkit_slug, tool_slugs):
        self.sessions.append((toolkit_slug, list(tool_slugs)))
        return {"session_id": "trs_test",
                "tool_router_tools": list(tool_slugs) if self._session_tools is None else self._session_tools}

    def execute_tool(self, tool_slug, arguments, version):
        self.executes.append({"tool": tool_slug, "arguments": arguments, "version": version})
        if self._error is not None:
            raise ComposioError(self._error)
        return self._data

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Client: session/execute request shape over a fake HTTP layer
# ---------------------------------------------------------------------------


def test_session_request_shape_and_header(clean_composio_env):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"session_id": "trs_x",
                                         "tool_router_tools": ["WEB3_CAREER_LIST_JOBS"]})

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    body = client.create_session("web3_career", ["WEB3_CAREER_LIST_JOBS"])

    assert captured["url"].endswith("/api/v3.1/tool_router/session")
    assert captured["key"] == API_KEY
    assert captured["body"] == {
        "toolkit_slug": "web3_career",
        "user_id": DEFAULT_USER_ID,
        "preload": {"tools": ["WEB3_CAREER_LIST_JOBS"]},
    }
    assert body["session_id"] == "trs_x"
    client.close()


def test_execute_request_shape(clean_composio_env, monkeypatch):
    monkeypatch.setenv("COMPOSIO_USER_ID", "custom-user")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["key"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response([]))

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    data = client.execute_tool("WEB3_CAREER_LIST_JOBS", {"limit": 5}, TOOL_REGISTRY["web3_career"]["version"])

    assert captured["path"] == "/api/v3.1/tools/execute/WEB3_CAREER_LIST_JOBS"
    assert captured["key"] == API_KEY
    # Version pinned per spike §6.2 (the REST default pins to "00000000_00").
    assert captured["body"] == {"arguments": {"limit": 5}, "user_id": "custom-user",
                                "version": "20261101_00"}
    assert data["jobs"] == []
    client.close()


def test_execute_omits_account_id_unless_configured(clean_composio_env, monkeypatch):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_response([]))

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    client.execute_tool("WEB3_CAREER_LIST_JOBS", {}, TOOL_REGISTRY["web3_career"]["version"])
    assert "connected_account_id" not in bodies[0]
    client.close()

    # The override is read at construction — a client built after setting it
    # passes the explicit account id (the spike-verified explicit path).
    monkeypatch.setenv("COMPOSIO_CONNECTED_ACCOUNT_ID", "ca_RJZsvHyL5ZFU")
    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    client.execute_tool("WEB3_CAREER_LIST_JOBS", {}, TOOL_REGISTRY["web3_career"]["version"])
    assert bodies[1]["connected_account_id"] == "ca_RJZsvHyL5ZFU"
    client.close()


def test_execute_envelope_validation_failure_is_source_error(clean_composio_env):
    """HTTP 200 with successful:false (upstream 400 embedded in data.message)
    must surface as a readable source error — never a crash, never silence."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "successful": False,
            "error": "Invalid request data provided",
            "data": {"message": "Invalid request data provided\n- Input should be 'ai', ..., "
                                "or 'zero-knowledge' on parameter `tag`",
                     "status_code": 400},
            "log_id": "log_4Kv7EsDQw-9Q",
        })

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(ComposioError) as excinfo:
        client.execute_tool("WEB3_CAREER_LIST_JOBS", {"tag": "not-a-tag"},
                            TOOL_REGISTRY["web3_career"]["version"])
    message = str(excinfo.value)
    assert "Invalid request data" in message
    assert "log_4Kv7EsDQw-9Q" in message
    # Errors never carry the API key (S1/S2 discipline).
    assert API_KEY not in message
    client.close()


def test_platform_error_401_shape(clean_composio_env):
    """Bad key: non-2xx JSON error dict — code/slug/message surface, key does not."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": {"message": "Invalid API key: INV**ROBE", "code": 801,
                      "slug": "APIKey_InvalidAPIKey",
                      "suggested_fix": "Please check you are using a valid API key."}})

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(ComposioError) as excinfo:
        client.create_session("web3_career", ["WEB3_CAREER_LIST_JOBS"])
    message = str(excinfo.value)
    assert "APIKey_InvalidAPIKey" in message
    assert "Invalid API key" in message
    assert API_KEY not in message
    client.close()


def test_missing_connection_is_degraded_source_error(clean_composio_env):
    """Error 1810 (no ACTIVE connection) — the connection-gate shape the
    working agreement requires to surface as a degraded-source error."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={
            "error": {"message": "No connected account found for user ID "
                                 "kestrel for toolkit web3_career",
                      "code": 1810,
                      "slug": "ActionExecute_ConnectedAccountNotFound",
                      "suggested_fix": "Connect your web3_career account first."}})

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(ComposioError) as excinfo:
        client.execute_tool("WEB3_CAREER_LIST_JOBS", {},
                            TOOL_REGISTRY["web3_career"]["version"])
    assert "1810" in str(excinfo.value)
    client.close()


def test_retry_on_429_honors_retry_after(clean_composio_env):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_ok_response([]))

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler), backoff_base=0)
    data = client.execute_tool("WEB3_CAREER_LIST_JOBS", {}, TOOL_REGISTRY["web3_career"]["version"])
    assert len(calls) == 3
    assert data["jobs"] == []
    client.close()


def test_retry_exhaustion_raises(clean_composio_env):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": {"message": "boom", "code": 5,
                                                   "slug": "ServerError"}})

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler),
                            max_retries=2, backoff_base=0)
    with pytest.raises(ComposioError):
        client.execute_tool("WEB3_CAREER_LIST_JOBS", {}, TOOL_REGISTRY["web3_career"]["version"])
    assert len(calls) == 3  # max_retries + 1
    client.close()


def test_transport_error_raises_credential_free(clean_composio_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = ComposioClient(API_KEY, transport=httpx.MockTransport(handler), backoff_base=0)
    with pytest.raises(ComposioError) as excinfo:
        client.create_session("web3_career", ["WEB3_CAREER_LIST_JOBS"])
    assert API_KEY not in str(excinfo.value)
    client.close()


# ---------------------------------------------------------------------------
# Mapping: result-to-jobs contract from the captured spike fixture
# ---------------------------------------------------------------------------


def test_map_keyrock_fixture_job():
    job = map_web3_job(KEYROCK_JOB)
    # apply_url verbatim (provider ToS) and used as both url and application_url
    assert job["url"] == "https://web3.career/r/yYDO1UTM__x9qMN8"
    assert job["application_url"] == job["url"]
    assert job["title"] == "Innovation Quant Developer: Intern - DeFi/Corporate Development"
    # explicit salary group null + estimated group populated (mutually exclusive)
    assert job["salary"] == "est. 110,000-247,500"
    # leading space trimmed, remote marker appended
    assert job["location"] == "Belgium Brussels Belgium (Remote)"
    # raw HTML stripped; company/tags folded into the description text
    assert "<p>" not in job["description"]
    assert "Company: Keyrock" in job["description"]
    assert "Tags: dev, intern" in job["description"]


def test_map_coinlist_explicit_salary():
    job = map_web3_job(COINLIST_JOB)
    assert job["salary"] == "USD 170,000-190,000/YEAR"
    assert job["location"] == "United States"
    assert job["description"] == "Company: CoinList\n\nTags: smart-contract"


def test_map_include_description_false_nulls():
    """include_description=false nulls id/company/tags and more — the mapper
    tolerates every null and dedupe stays on apply_url."""
    job = map_web3_job(NO_DESCRIPTION_JOB)
    assert job["url"] == "https://web3.career/r/au-example"
    assert job["title"] == "Blockchain Developer"
    assert job["location"] == "Australia"  # trimmed, is_remote is null
    assert job["salary"] == "est. 120,000"
    assert job["description"] is None


def test_map_avg_only_estimated_salary():
    job = map_web3_job({**NO_DESCRIPTION_JOB, "estimated_avg_salary": 172000,
                        "estimated_min_salary": None, "estimated_max_salary": None})
    assert job["salary"] == "est. 172,000"


def test_map_salary_group_all_null():
    job = map_web3_job({**COINLIST_JOB, "salary_currency": None, "salary_unit": None,
                        "salary_min_value": None, "salary_max_value": None})
    assert job["salary"] is None


def test_map_missing_apply_url_raises():
    with pytest.raises(ValueError):
        map_web3_job({**KEYROCK_JOB, "apply_url": None})


def test_map_non_numeric_salary_is_none_not_crash():
    job = map_web3_job({**COINLIST_JOB, "salary_min_value": "lots", "salary_max_value": "more"})
    assert job["salary"] is None


# ---------------------------------------------------------------------------
# Storage: per-row guards (B10) and apply_url dedupe
# ---------------------------------------------------------------------------


def test_store_batch_skips_malformed_and_dedupes(isolated_db):
    conn = init_db()
    batch = [KEYROCK_JOB, {"title": "listing with no apply_url"}, COINLIST_JOB]

    new, existing, skipped, filtered = store_web3_jobs(conn, batch)
    assert (new, existing, skipped, filtered) == (2, 0, 1, 0)

    # Re-run the same batch: everything dedupes on apply_url.
    new, existing, skipped, filtered = store_web3_jobs(conn, batch)
    assert (new, existing, skipped, filtered) == (0, 2, 1, 0)

    row = conn.execute(
        "SELECT url, application_url, site, strategy, detail_scraped_at FROM jobs WHERE url = ?",
        (KEYROCK_JOB["apply_url"],),
    ).fetchone()
    assert row[0] == KEYROCK_JOB["apply_url"]  # stored verbatim
    assert row[1] == KEYROCK_JOB["apply_url"]
    assert row[2] == "web3_career"
    assert row[3] == "composio"
    assert row[4] is not None  # long description satisfies enrichment up front


def test_store_long_description_promotes_full_description(isolated_db):
    conn = init_db()
    new, _, _, _ = store_web3_jobs(conn, [KEYROCK_JOB, COINLIST_JOB])
    assert new == 2
    full, empty = conn.execute(
        "SELECT full_description, detail_scraped_at FROM jobs WHERE url = ?",
        (KEYROCK_JOB["apply_url"],),
    ).fetchone()
    assert full and full.startswith("Company: Keyrock")
    assert empty is not None
    short_full, short_detail = conn.execute(
        "SELECT full_description, detail_scraped_at FROM jobs WHERE url = ?",
        (COINLIST_JOB["apply_url"],),
    ).fetchone()
    assert short_full is None  # short description stays in the enrichment queue
    assert short_detail is None


def test_store_applies_location_filter(isolated_db):
    conn = init_db()
    # Empty accept list accepts all non-remote; CoinList is non-remote US.
    new, _, _, filtered = store_web3_jobs(conn, [COINLIST_JOB], accept_locs=[], reject_locs=[])
    assert (new, filtered) == (1, 0)
    # A non-empty accept list without a matching pattern filters it out.
    new, _, _, filtered = store_web3_jobs(conn, [COINLIST_JOB], accept_locs=["canada"], reject_locs=[])
    assert (new, filtered) == (0, 1)
    # Pure-remote jobs always pass. A remote-tagged job with a geographic
    # remainder is filtered by that remainder (2026-11-10: "France, Remote"
    # leaked past a US accept list via the old remote-marker early return).
    new, _, _, filtered = store_web3_jobs(conn, [KEYROCK_JOB], accept_locs=["canada"], reject_locs=[])
    assert (new, filtered) == (0, 1)  # "Belgium Brussels Belgium (Remote)": geo remainder fails
    remote_only = {**KEYROCK_JOB, "location": "Remote"}
    new, _, _, filtered = store_web3_jobs(conn, [remote_only], accept_locs=["canada"], reject_locs=[])
    assert (new, filtered) == (1, 0)  # nothing geographic left -> always passes


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_toolkits_default(clean_composio_env):
    assert composio._resolve_toolkits({}) == ["web3_career"]


def test_toolkits_env_overrides(clean_composio_env, monkeypatch):
    monkeypatch.setenv("COMPOSIO_TOOLKITS", "web3_career, dice_mcp")
    assert composio._resolve_toolkits({}) == ["web3_career", "dice_mcp"]


def test_toolkits_from_yaml_block(clean_composio_env):
    assert composio._resolve_toolkits({"toolkits": ["web3_career"]}) == ["web3_career"]


def test_tool_arguments_passthrough(clean_composio_env):
    block = {"tag": "solidity", "limit": 20, "country": "united-states",
             "remote_only": True, "include_description": False}
    assert composio._tool_arguments(block) == block
    assert composio._tool_arguments({}) == {}


# ---------------------------------------------------------------------------
# Run flow (fake client injection — no network)
# ---------------------------------------------------------------------------


def test_run_discovery_happy_path(clean_composio_env, monkeypatch, isolated_db):
    monkeypatch.setenv("COMPOSIO_API_KEY", API_KEY)
    fake = _FakeClient(data=_ok_data([KEYROCK_JOB, {"title": "no url"}, COINLIST_JOB]))

    result = composio.run_composio_discovery(
        cfg={"composio": {"tag": "solidity", "limit": 20}}, client=fake)

    # Session created scoped to the toolkit, with the registry tool preloaded.
    assert fake.sessions == [("web3_career", ["WEB3_CAREER_LIST_JOBS"])]
    assert fake.executes == [{"tool": "WEB3_CAREER_LIST_JOBS",
                              "arguments": {"tag": "solidity", "limit": 20},
                              "version": "20261101_00"}]
    assert result["new"] == 2
    assert result["existing"] == 0
    assert result["skipped_listings"] == 1
    assert result["toolkits"] == ["web3_career"]


def test_run_discovery_default_arguments(clean_composio_env, monkeypatch, isolated_db):
    monkeypatch.setenv("COMPOSIO_API_KEY", API_KEY)
    fake = _FakeClient(data=_ok_data([]))
    composio.run_composio_discovery(cfg={}, client=fake)
    assert fake.executes[0]["arguments"] == {}


def test_bogus_toolkit_detected_via_session(clean_composio_env, monkeypatch, isolated_db):
    """A bogus toolkit slug 201s silently with no tools (spike §4 row 3) —
    the session verification must catch it before any execution."""
    monkeypatch.setenv("COMPOSIO_API_KEY", API_KEY)
    fake = _FakeClient(session_tools=[])

    with pytest.raises(ComposioError):
        composio.run_composio_discovery(cfg={}, client=fake)
    assert fake.executes == []  # never executed on an unverified session


def test_unmapped_toolkit_skipped_with_warning(clean_composio_env, monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", API_KEY)
    monkeypatch.setenv("COMPOSIO_TOOLKITS", "dice_mcp")
    fake = _FakeClient()

    result = composio.run_composio_discovery(cfg={}, client=fake)
    assert fake.sessions == []  # no verified mapping → no calls
    assert result == composio._zero_stats()


def test_all_toolkits_failing_raises_source_error(clean_composio_env, monkeypatch, isolated_db):
    monkeypatch.setenv("COMPOSIO_API_KEY", API_KEY)
    fake = _FakeClient(error="Composio error 1810 (ActionExecute_ConnectedAccountNotFound): "
                             "No connected account found")

    with pytest.raises(ComposioError) as excinfo:
        composio.run_composio_discovery(cfg={}, client=fake)
    assert "1810" in str(excinfo.value)


def test_run_discovery_not_configured_noops(clean_composio_env, monkeypatch):
    """Neither key nor block: quiet no-op, no client construction."""
    monkeypatch.setattr(
        composio, "ComposioClient",
        lambda *a, **k: pytest.fail("client must not be constructed when unconfigured"))
    assert composio.run_composio_discovery(cfg={}) == composio._zero_stats()


def test_run_discovery_block_without_key_noops(clean_composio_env, monkeypatch):
    """Block present but key missing: warn-and-skip, like an empty Workday registry."""
    monkeypatch.setattr(
        composio, "ComposioClient",
        lambda *a, **k: pytest.fail("client must not be constructed without an API key"))
    assert composio.run_composio_discovery(cfg={"composio": {}}) == composio._zero_stats()


def test_empty_result_is_clean(clean_composio_env, monkeypatch, isolated_db):
    """Empty result set (spike §3f) is a successful no-op, not an error."""
    monkeypatch.setenv("COMPOSIO_API_KEY", API_KEY)
    fake = _FakeClient(data=_ok_data([], result_count=0))
    result = composio.run_composio_discovery(cfg={}, client=fake)
    assert result["new"] == 0
    assert result["toolkits"] == ["web3_career"]


# ---------------------------------------------------------------------------
# Pipeline wiring: discovery-summary degradation (B7 semantics)
# ---------------------------------------------------------------------------


def test_discover_stage_degrades_on_composio_error(monkeypatch, clean_composio_env):
    """A Composio source error lands in the discover-stage summary exactly
    like the other sources — logged, counted, never a crashed stage."""
    monkeypatch.setattr("kestrel.discovery.jobspy.run_discovery", lambda: None)
    monkeypatch.setattr("kestrel.discovery.workday.run_workday_discovery",
                        lambda employers=None, workers=1: None)
    monkeypatch.setattr("kestrel.discovery.smartextract.run_smart_extract",
                        lambda workers=1: None)

    import kestrel.discovery.composio as composio_module

    def boom():
        raise ComposioError("no ACTIVE connection for toolkit web3_career")

    monkeypatch.setattr(composio_module, "run_composio_discovery", boom)

    import kestrel.pipeline as pipeline
    stats = pipeline._run_discover()
    assert stats["composio"].startswith("error:")
    assert "no ACTIVE connection" in stats["composio"]


# ---------------------------------------------------------------------------
# Doctor: masked key (S2 discipline)
# ---------------------------------------------------------------------------


def test_doctor_row_masks_key(clean_composio_env, monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "supersecretkey-987654")
    status, note = composio.doctor_row()
    assert status == "[green]OK[/green]"
    assert "7654" in note  # last four only
    assert "supersecretkey" not in note


def test_doctor_row_unconfigured(clean_composio_env):
    status, note = composio.doctor_row()
    assert "optional" in status
    assert "COMPOSIO_API_KEY" in note


def test_doctor_row_block_without_key_warns(clean_composio_env, monkeypatch):
    monkeypatch.setattr(composio.config, "load_search_config", lambda: {"composio": {}})
    status, note = composio.doctor_row()
    assert status == "[yellow]WARN[/yellow]"
    assert "COMPOSIO_API_KEY" in note


# ---------------------------------------------------------------------------
# Wizard: optional Composio step
# ---------------------------------------------------------------------------


def test_wizard_composio_step_writes_env_and_yaml(app_dir, monkeypatch):
    import kestrel.wizard.init as wizard

    answers = iter(["ck_wizard_secret_4321", "web3_career", DEFAULT_USER_ID])
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))

    wizard._setup_composio()

    env_text = (app_dir / ".env").read_text(encoding="utf-8")
    assert "COMPOSIO_API_KEY='ck_wizard_secret_4321'" in env_text
    assert "COMPOSIO_TOOLKITS='web3_career'" in env_text
    assert f"COMPOSIO_USER_ID='{DEFAULT_USER_ID}'" in env_text
    assert stat.S_IMODE((app_dir / ".env").stat().st_mode) == 0o600

    yaml_text = (app_dir / "searches.yaml").read_text(encoding="utf-8")
    assert "\ncomposio:" in yaml_text
    assert "web3_career" in yaml_text

    # Re-run: the key is replaced, the yaml block is not duplicated.
    answers = iter(["ck_new_secret_0000", "web3_career", DEFAULT_USER_ID])
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    wizard._setup_composio()

    env_text = (app_dir / ".env").read_text(encoding="utf-8")
    assert "COMPOSIO_API_KEY='ck_new_secret_0000'" in env_text
    assert "ck_wizard_secret_4321" not in env_text
    yaml_text = (app_dir / "searches.yaml").read_text(encoding="utf-8")
    assert yaml_text.count("composio:") == 1


def test_wizard_composio_step_skippable(app_dir, monkeypatch):
    import kestrel.wizard.init as wizard

    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: False))
    wizard._setup_composio()
    assert not (app_dir / ".env").exists() or "COMPOSIO_API_KEY" not in (
        app_dir / ".env").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lazy import: the base install works without the composio-core extra
# ---------------------------------------------------------------------------


def test_base_install_imports_without_composio_sdk():
    """The discovery module and pipeline import with the composio-core SDK
    blocked — the base install carries no composio-core dependency."""
    repo_root = Path(__file__).resolve().parents[1]
    code = textwrap.dedent("""
        import sys

        class _BlockExtra:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("composio", "composio_core"):
                    raise ImportError("composio-core extra not installed")
                return None

        sys.meta_path.insert(0, _BlockExtra())
        import kestrel.discovery.composio  # noqa: F401
        import kestrel.pipeline  # noqa: F401
        print("ok")
    """)
    env = dict(os.environ, KESTREL_DIR=tempfile.mkdtemp(prefix="kestrel-lazy-"))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env=env, cwd=repo_root, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
