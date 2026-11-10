"""Contract tests for `kestrel agent page-capture` and the smart-extract
HTTP lane (spec art_VGFSMgRh, piece 2).

Kestrel never launches a browser: the host agent captures pages in its own
browser and posts them back through the page-capture verb; plain HTTP
(httpx + BeautifulSoup) is the standalone fallback, and a JS-rendered shell
escalates to a typed browser_needed hand-off instead of any relaunch. The
regression test pins that no launch path remains in the module source.
"""

import inspect
import json

import pytest
from typer.testing import CliRunner

from kestrel import database
from kestrel.cli import app
from kestrel.discovery import smartextract

runner = CliRunner()

# A distinctive string submitted inside captured content that must never
# leak into the protocol's stdout payload (redaction contract).
SUBMITTED_MARKER = "WHISPERED_SECRET_CARD_XYZ"

PAGE_HTML = f"""<html><head><title>Jobs — Acme</title>
<script type="application/ld+json">{{"@type": "JobPosting", "title": "Data Engineer"}}</script>
</head><body><main><ul>
<li data-testid="job-card"><a href="/jobs/1"><h2>Data Engineer</h2><span>{SUBMITTED_MARKER} Austin, TX</span></a></li>
<li data-testid="job-card"><a href="/jobs/2"><h2>Platform Engineer</h2><span>Remote</span></a></li>
<li data-testid="job-card"><a href="/jobs/3"><h2>ML Engineer</h2><span>Austin, TX</span></a></li>
</ul></main></body></html>"""

JS_SHELL_HTML = ("<html><head><title>App</title></head><body>"
                 '<div id="root"></div><script src="/app.js"></script></body></html>')


def run(*args):
    return runner.invoke(app, ["agent", *args])


def payload_of(result):
    return json.loads(result.stdout)


class _FakeResponse:
    """Stand-in for httpx.Response at the boundary collect_page_intelligence uses."""

    def __init__(self, text):
        self._text = text

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        return None


@pytest.fixture()
def store(app_dir, isolated_db):
    return database.init_db()


@pytest.fixture()
def site_url():
    return "https://jobs.example.com/listings"


# ---------------------------------------------------------------------------
# The verb: `kestrel agent page-capture`
# ---------------------------------------------------------------------------

class TestPageCaptureVerb:
    def test_valid_html_capture_persists_intel(self, store, site_url):
        result = run("page-capture", "--url", site_url, "--html", PAGE_HTML)

        assert result.exit_code == 0
        payload = payload_of(result)
        assert payload["verb"] == "page-capture"
        assert payload["captured"]["json_ld_entries"] == 1
        assert payload["captured"]["page_title"] == "Jobs — Acme"
        assert payload["captured"]["full_html_chars"] == len(PAGE_HTML)
        assert payload["next_capture"] is None

        row = database.get_page_capture(store, site_url)
        assert row is not None and row["status"] == "captured"
        intel = row["intel"]
        assert intel["json_ld"][0]["@type"] == "JobPosting"
        assert intel["full_html"] == PAGE_HTML

    def test_capture_returns_next_pending_as_next_action(self, store, site_url):
        older = "https://jobs.example.com/older"
        newer = "https://jobs.example.com/newer"
        database.record_capture_needed(store, older)
        database.record_capture_needed(store, newer)

        result = run("page-capture", "--url", site_url, "--html", PAGE_HTML)

        assert result.exit_code == 0
        payload = payload_of(result)
        assert payload["next_capture"] == {"url": older}

    def test_missing_content_flags_typed_error(self, store, site_url):
        result = run("page-capture", "--url", site_url)

        assert result.exit_code == 1
        assert payload_of(result)["error"]["code"] == "invalid_capture"

    def test_missing_url_typed_error(self, store):
        result = run("page-capture", "--html", PAGE_HTML)

        assert result.exit_code == 1
        assert payload_of(result)["error"]["code"] == "invalid_usage"

    def test_empty_html_typed_error(self, store, site_url):
        result = run("page-capture", "--url", site_url, "--html", "   ")

        assert result.exit_code == 1
        assert payload_of(result)["error"]["code"] == "invalid_html"

    def test_invalid_json_ld_typed_error(self, store, site_url):
        result = run("page-capture", "--url", site_url, "--json-ld", "{not json")

        assert result.exit_code == 1
        assert payload_of(result)["error"]["code"] == "invalid_json_ld"

    def test_invalid_api_responses_typed_error(self, store, site_url):
        result = run("page-capture", "--url", site_url,
                     "--api-responses", "[1, 2, 3]")

        assert result.exit_code == 1
        assert payload_of(result)["error"]["code"] == "invalid_api_responses"

    def test_api_responses_summarized_into_intel(self, store, site_url):
        captured = [{"url": "https://api.example.com/list", "status": 200,
                     "size": 42, "data": [{"title": "Data Engineer"}]}]
        result = run("page-capture", "--url", site_url,
                     "--api-responses", json.dumps(captured))

        assert result.exit_code == 0
        intel = database.get_page_capture(store, site_url)["intel"]
        summary = intel["api_responses"][0]
        assert summary["type"] == "array[1]"
        assert summary["first_item_keys"] == ["title"]
        assert summary["_raw_data"] == captured[0]["data"]

    def test_submitted_content_is_redacted_from_payload(self, store, site_url):
        result = run("page-capture", "--url", site_url, "--html", PAGE_HTML)

        assert result.exit_code == 0
        assert SUBMITTED_MARKER not in result.stdout
        assert PAGE_HTML not in result.stdout

    def test_verb_listed_in_agent_help(self, store):
        result = run("--help")

        assert result.exit_code == 0
        assert "page-capture" in result.stdout


# ---------------------------------------------------------------------------
# The pure builders: the same intel dict the Playwright collector produced
# ---------------------------------------------------------------------------

class TestBuildIntelFromHtml:
    def test_full_inventory(self, store, site_url):
        intel = smartextract.build_intel_from_html(site_url, PAGE_HTML)

        assert intel["url"] == site_url
        assert intel["page_title"] == "Jobs — Acme"
        assert intel["json_ld"][0]["@type"] == "JobPosting"
        assert intel["full_html"] == PAGE_HTML
        assert [d["testid"] for d in intel["data_testids"]] == ["job-card"] * 3

    def test_dom_stats(self, store, site_url):
        intel = smartextract.build_intel_from_html(site_url, PAGE_HTML)

        stats = intel["dom_stats"]
        assert stats["links"] == 3
        assert stats["headings"] == 3
        assert stats["lists"] == 1

    def test_card_candidates_find_repeating_cards(self, store, site_url):
        intel = smartextract.build_intel_from_html(site_url, PAGE_HTML)

        candidates = intel["card_candidates"]
        assert candidates, "the three repeating <li> cards should be detected"
        top = candidates[0]
        assert top["child_tag"] == "li"
        assert top["with_text"] == 3
        assert top["with_links"] == 3

    def test_empty_html_yields_empty_intel(self, site_url):
        intel = smartextract.build_intel_from_html(site_url, "")

        assert intel["json_ld"] == []
        assert intel["data_testids"] == []
        assert intel["full_html"] == ""


class TestExecutorsOverCapturedIntel:
    def test_execute_json_ld_over_http_intel(self, store, site_url, monkeypatch):
        monkeypatch.setattr(smartextract.httpx, "get",
                            lambda url, **kw: _FakeResponse(PAGE_HTML))
        intel = smartextract.collect_page_intelligence(site_url)

        plan = {"extraction": {"title": "title", "salary": "null",
                               "description": "null", "location": "null",
                               "url": "url"}}
        jobs = smartextract.execute_json_ld(intel, plan)

        assert jobs == [{"title": "Data Engineer", "salary": None,
                         "description": None, "location": None, "url": None}]

    def test_execute_api_response_over_submitted_intel(self, store, site_url):
        captured = [{"url": "https://api.example.com/listings", "status": 200,
                     "size": 99,
                     "data": {"hits": [{"title": "Data Engineer",
                                        "loc": "Austin, TX"}]}}]
        intel = smartextract.build_intel_from_html(site_url, "")
        intel["api_responses"] = smartextract.summarize_api_responses(captured)

        plan = {"extraction": {"url_pattern": "api.example.com/listings",
                               "items_path": "hits", "title": "title",
                               "salary": "null", "description": "null",
                               "location": "loc", "url": "null"}}
        jobs = smartextract.execute_api_response(intel, plan)

        assert jobs == [{"title": "Data Engineer", "salary": None,
                         "description": None, "location": "Austin, TX",
                         "url": None}]

    def test_execute_css_selectors_over_submitted_intel(self, store, site_url, monkeypatch):
        selectors = {"job_card": "li[data-testid='job-card']", "title": "h2",
                     "salary": "null", "description": "span", "location": "null",
                     "url": "a"}
        meta = {"response_chars": len(json.dumps(selectors))}
        monkeypatch.setattr(smartextract, "ask_llm",
                            lambda prompt: (json.dumps(selectors), 0.0, meta))
        intel = smartextract.build_intel_from_html(site_url, PAGE_HTML)

        found, jobs = smartextract.execute_css_selectors(intel)

        assert found == selectors
        assert [j["title"] for j in jobs] == ["Data Engineer",
                                              "Platform Engineer",
                                              "ML Engineer"]
        assert jobs[0]["url"] == "/jobs/1"


# ---------------------------------------------------------------------------
# The smart-extract flow: browser_needed escalation, never a relaunch
# ---------------------------------------------------------------------------

class TestBrowserNeededEscalation:
    def _fake_http(self, monkeypatch, html):
        monkeypatch.setattr(smartextract.httpx, "get",
                            lambda url, **kw: _FakeResponse(html))

    def test_js_shell_escalates_browser_needed_and_records_queue(
            self, store, site_url, monkeypatch):
        self._fake_http(monkeypatch, JS_SHELL_HTML)

        result = smartextract._run_one_site("acme", site_url)

        assert result["status"] == "BROWSER_NEEDED"
        assert "browser-needed" in result["error"]
        assert "kestrel agent page-capture" in result["error"]
        pending = database.get_pending_captures(store)
        assert [row["url"] for row in pending] == [site_url]

    def test_captured_intel_is_consumed_without_http(self, store, site_url,
                                                     monkeypatch):
        intel = smartextract.build_intel_from_html(site_url, PAGE_HTML)
        database.record_page_capture(store, site_url, intel)
        plan = {"strategy": "json_ld", "reasoning": "JobPosting present",
                "extraction": {"title": "title", "salary": "null",
                               "description": "null", "location": "null",
                               "url": "null"}}
        meta = {"response_chars": len(json.dumps(plan))}
        monkeypatch.setattr(smartextract, "ask_llm",
                            lambda prompt: (json.dumps(plan), 0.0, meta))

        def _no_http(*args, **kw):
            raise AssertionError("HTTP fetch must not run when a capture exists")

        monkeypatch.setattr(smartextract.httpx, "get", _no_http)

        result = smartextract._run_one_site("acme", site_url)

        assert result["strategy"] == "json_ld"
        assert result["jobs"] == [{"title": "Data Engineer", "salary": None,
                                   "description": None, "location": None,
                                   "url": None}]

    def test_escalation_without_playwright_installed(self, store, site_url,
                                                     monkeypatch):
        # No Playwright anywhere on the path: the flow escalates through the
        # typed hand-off instead of crashing (the import gate below is the
        # assertion the no-extras environment exercises).
        import builtins

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError(f"blocked for test: {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        self._fake_http(monkeypatch, JS_SHELL_HTML)

        result = smartextract._run_one_site("acme", site_url)

        assert result["status"] == "BROWSER_NEEDED"


class TestNoBrowserPathRegression:
    """Pin the removal: no launch site, no guard import, no headed retry."""

    def test_no_browser_code_remains_in_smartextract(self):
        source = inspect.getsource(smartextract).lower()
        assert "playwright" not in source
        assert "chromium" not in source
        # The headed retry re-opened visible windows via a headless=False
        # relaunch — the flag itself must be gone with it.
        assert "headless" not in source

    def test_js_shell_detection_contract(self, store, site_url, monkeypatch):
        # A captcha interstitial is a block, not a shell — no escalation.
        captcha_html = PAGE_HTML + "<div>please verify you are human</div>"
        assert not smartextract._is_js_shell(
            smartextract.build_intel_from_html(site_url, captcha_html))
        # A real shell escalates.
        assert smartextract._is_js_shell(
            smartextract.build_intel_from_html(site_url, JS_SHELL_HTML))
