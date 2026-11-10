# SPDX-License-Identifier: AGPL-3.0-only
"""Detail-scraper crash isolation over the pure-HTTP transport.

The description-fetch stage aborted on the first exception its batch met:
one bad page killed every later job in the site, and one failed site
batch killed every later site — the run "finished" with most pending
jobs never attempted. These tests pin the isolation contract:

- one job's crash costs that row (detail_error stamped, batch continues);
- one site's crash costs that site (site_errors counted, run continues).

The transport is stubbed at detail._fetch_html — no network, no browser.
Static fixtures exercise the unchanged extraction tiers as pure functions
over the parsed soup; a JS-shell fixture pins the browser-needed hand-off.
"""

import json

import httpx

from kestrel.database import init_db
from kestrel.enrichment import detail as detail_mod

JOB_POSTING = {
    "@type": "JobPosting",
    "description": "<p>Full description body long enough to "
                   "pass the fifty character floor in the "
                   "tier-one extraction cascade.</p>",
    "url": "https://example.test/apply",
}

STATIC_PAGE = f"""<html>
<head><title>Engineer — Example</title>
<script type="application/ld+json">{json.dumps(JOB_POSTING)}</script>
</head>
<body><h1>Engineer</h1>
<p>We are hiring an engineer. Build things with us and ship great software.</p>
<a href="https://example.test/apply">Apply now</a>
</body></html>"""

# A SPA root: minimal DOM, no JSON-LD, nothing server-rendered to extract.
JS_SHELL_PAGE = """<html>
<head><title>Example</title></head>
<body><div id="root"></div><script src="/app.js"></script></body>
</html>"""

# Tier-2 fixture: no JSON-LD, but a description block the deterministic
# selectors catch.
TIER2_PAGE = """<html>
<head><title>Engineer</title></head>
<body>
<div class="job-description">You will build distributed systems for our
platform team, working across services and infrastructure. Minimum padding
to cross the one-hundred character bar for tier two extraction in this
fixture page, with room to spare.</div>
<a class="apply-button" href="https://example.test/apply">Apply</a>
</body></html>"""


def _seed_pending(conn, url: str, site: str, title: str = "Engineer") -> None:
    conn.execute(
        "INSERT INTO jobs (url, title, site, strategy, discovered_at) "
        "VALUES (?, ?, ?, 'test', '2026-11-09T00:00:00Z')",
        (url, title, site),
    )
    conn.commit()


def _response(url: str, page: str, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(status, text=page, request=request)


class _FetchStub:
    """detail._fetch_html double: serves canned pages, raises on cue."""

    def __init__(self, pages: dict[str, str] | None = None,
                 raise_on: set[str] | None = None,
                 status: int = 200):
        self.pages = pages or {}
        self.raise_on = raise_on or set()
        self.status = status

    def __call__(self, client, url: str) -> httpx.Response:
        if url in self.raise_on:
            raise httpx.ConnectError(
                "connection refused", request=httpx.Request("GET", url))
        return _response(url, self.pages.get(url, STATIC_PAGE), self.status)


def test_setup_failure_records_error_on_every_job(isolated_db, monkeypatch):
    """A batch-level setup crash (client construction, proxy wiring) must
    not leave the site's jobs silent: every job in the batch gets a
    detail_error and the function returns instead of raising."""
    conn = init_db()
    urls = ("https://example.test/1", "https://example.test/2",
            "https://example.test/3")
    for url in urls:
        _seed_pending(conn, url, "ExampleCorp")

    def _boom(**kwargs):
        raise RuntimeError("proxy config invalid")

    monkeypatch.setattr(detail_mod.httpx, "Client", _boom)

    jobs = [(url, "Engineer") for url in urls]
    stats = detail_mod.scrape_site_batch(conn, "ExampleCorp", jobs, delay=0)

    rows = conn.execute(
        "SELECT url, detail_error, detail_scraped_at FROM jobs ORDER BY url"
    ).fetchall()
    assert [row[1] for row in rows] == [
        "enrich-setup-failed: proxy config invalid"] * len(urls)
    assert all(row[2] for row in rows)  # every row also gets a timestamp
    assert stats["error"] == len(urls)
    assert stats["processed"] == 0


def test_scrape_site_batch_continues_after_one_job_crashes(isolated_db,
                                                           monkeypatch):
    conn = init_db()
    urls = ("https://example.test/1", "https://example.test/2",
            "https://example.test/3")
    for url in urls:
        _seed_pending(conn, url, "ExampleCorp")

    # Crash the persistence step for job 2: persist_description runs
    # outside scrape_detail_page's internal try, so the exception escapes
    # to the batch's outer guard. The fetch itself succeeds for all three
    # jobs so they extract through the JSON-LD tier and reach persistence.
    real_persist = detail_mod.persist_description

    def _boom_on_two(conn, url, full_description, application_url, title):
        if url == urls[1]:
            raise RuntimeError("db write failed")
        real_persist(conn, url, full_description, application_url, title)

    monkeypatch.setattr(detail_mod, "persist_description", _boom_on_two)
    monkeypatch.setattr(detail_mod, "_fetch_html", _FetchStub())

    jobs = [(url, "Engineer") for url in urls]
    stats = detail_mod.scrape_site_batch(conn, "ExampleCorp", jobs, delay=0)

    # The batch survived: all three jobs processed, the crash costing
    # exactly its own row. Jobs 1 and 3 extract fine (JSON-LD tier) and
    # persist; job 2's persistence crash is contained to its row.
    assert stats["processed"] == 3
    assert stats["error"] == 1
    rows = conn.execute(
        "SELECT url, detail_error, detail_scraped_at FROM jobs ORDER BY url"
    ).fetchall()
    assert all(row["detail_scraped_at"] for row in rows)
    persisted = conn.execute(
        "SELECT url, full_description FROM jobs WHERE url != ?",
        (urls[1],),
    ).fetchall()
    assert all(row["full_description"] for row in persisted)
    crashed = [row for row in rows if row["url"] == urls[1]]
    assert crashed[0]["detail_error"].startswith("enrich-crash:")
    assert "db write failed" in crashed[0]["detail_error"]


def test_fetch_failure_is_contained_to_its_row(isolated_db, monkeypatch):
    # A transport error on one URL (refused connection, timeout) is
    # handled inside scrape_detail_page: its row gets a detail_error and
    # the other jobs still enrich.
    conn = init_db()
    urls = ("https://example.test/a", "https://example.test/b")
    for url in urls:
        _seed_pending(conn, url, "ExampleCorp")

    monkeypatch.setattr(
        detail_mod, "_fetch_html",
        _FetchStub(raise_on={urls[0]}))

    stats = detail_mod.scrape_site_batch(
        conn, "ExampleCorp", [(url, "Engineer") for url in urls], delay=0)

    assert stats["processed"] == 2
    assert stats["error"] == 1
    assert stats["ok"] == 1
    failed = conn.execute(
        "SELECT detail_error FROM jobs WHERE url = ?", (urls[0],)
    ).fetchone()
    assert failed[0]  # stamped, not silent
    enriched = conn.execute(
        "SELECT full_description FROM jobs WHERE url = ?", (urls[1],)
    ).fetchone()
    assert enriched[0]


def test_static_page_enriches_via_json_ld_tier(isolated_db, monkeypatch):
    # Same detail fields the old Chromium path produced: full_description,
    # application_url, tier 1, no error — from fixture HTML, no network.
    conn = init_db()
    _seed_pending(conn, "https://example.test/1", "ExampleCorp")

    monkeypatch.setattr(detail_mod, "_fetch_html", _FetchStub())

    stats = detail_mod.scrape_site_batch(
        conn, "ExampleCorp", [("https://example.test/1", "Engineer")], delay=0)

    assert stats["ok"] == 1
    assert stats["tiers"][1] == 1
    row = conn.execute(
        "SELECT full_description, application_url, detail_error "
        "FROM jobs WHERE url = 'https://example.test/1'"
    ).fetchone()
    assert "Full description body long enough" in row[0]
    assert row[1] == "https://example.test/apply"
    assert row[2] is None


def test_tier2_deterministic_extraction_over_soup(isolated_db, monkeypatch):
    conn = init_db()
    _seed_pending(conn, "https://css.test/1", "CssCorp")

    monkeypatch.setattr(
        detail_mod, "_fetch_html",
        _FetchStub(pages={"https://css.test/1": TIER2_PAGE}))

    stats = detail_mod.scrape_site_batch(
        conn, "CssCorp", [("https://css.test/1", "Engineer")], delay=0)

    assert stats["ok"] == 1
    assert stats["tiers"][2] == 1
    row = conn.execute(
        "SELECT full_description, application_url FROM jobs "
        "WHERE url = 'https://css.test/1'"
    ).fetchone()
    assert "distributed systems" in row[0]
    assert row[1] == "https://example.test/apply"


def test_js_shell_records_browser_needed_and_points_at_agent_verb(
        isolated_db, monkeypatch):
    # A JS-shell page (minimal DOM, no JSON-LD) must stop with the typed
    # browser-needed hand-off — never a relaunch, never a silent failure.
    conn = init_db()
    _seed_pending(conn, "https://spa.test/1", "SpaCorp")

    monkeypatch.setattr(
        detail_mod, "_fetch_html",
        _FetchStub(pages={"https://spa.test/1": JS_SHELL_PAGE}))

    stats = detail_mod.scrape_site_batch(
        conn, "SpaCorp", [("https://spa.test/1", "Engineer")], delay=0)

    assert stats["error"] == 1
    assert stats["tiers"] == {1: 0, 2: 0, 3: 0}
    row = conn.execute(
        "SELECT detail_error, detail_scraped_at FROM jobs "
        "WHERE url = 'https://spa.test/1'"
    ).fetchone()
    assert row[0].startswith(detail_mod.BROWSER_NEEDED_ERROR)
    assert "kestrel agent enrich --url https://spa.test/1" in row[0]
    assert row[1]  # stamped: the row left the HTTP queue for the agent lane


def test_run_detail_scraper_continues_after_site_batch_crashes(
        isolated_db, monkeypatch):
    conn = init_db()
    _seed_pending(conn, "https://aaa.test/1", "AAA")
    _seed_pending(conn, "https://aaa.test/2", "AAA")
    _seed_pending(conn, "https://bbb.test/1", "BBB")

    def _boom(conn, site, jobs, **kwargs):
        if site == "AAA":
            raise RuntimeError("batch died")
        return {"processed": len(jobs), "ok": 1, "partial": 0, "error": 0,
                "tiers": {1: 1, 2: 0, 3: 0}}

    monkeypatch.setattr(detail_mod, "scrape_site_batch", _boom)

    stats = detail_mod._run_detail_scraper(conn)

    assert stats["site_errors"] == 1
    assert stats["ok"] == 1  # BBB still ran


def test_run_detail_scraper_parallel_mode_also_isolates_sites(
        isolated_db, monkeypatch):
    conn = init_db()
    _seed_pending(conn, "https://aaa.test/1", "AAA")
    _seed_pending(conn, "https://bbb.test/1", "BBB")

    def _boom(conn, site, jobs, **kwargs):
        if site == "AAA":
            raise RuntimeError("batch died")
        return {"processed": len(jobs), "ok": 1, "partial": 0, "error": 0,
                "tiers": {1: 1, 2: 0, 3: 0}}

    monkeypatch.setattr(detail_mod, "scrape_site_batch", _boom)

    stats = detail_mod._run_detail_scraper(conn, workers=2)

    assert stats["site_errors"] == 1
    assert stats["ok"] == 1


def test_scrape_site_batch_creates_own_connection(isolated_db, monkeypatch):
    # conn=None path: the batch owns its connection and the finally-block
    # closes it without leaking into the caller's handle.
    conn = init_db()
    _seed_pending(conn, "https://example.test/own", "ExampleCorp")

    monkeypatch.setattr(detail_mod, "_fetch_html", _FetchStub())

    stats = detail_mod.scrape_site_batch(
        None, "ExampleCorp", [("https://example.test/own", "Engineer")],
        delay=0)
    assert stats["processed"] == 1


def test_proxy_config_threads_through_httpx_client(isolated_db, monkeypatch):
    # The shared proxy config must reach the httpx client the same way it
    # used to reach the old browser transport's launch opts.
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _response(url, STATIC_PAGE)

    monkeypatch.setattr(detail_mod.httpx, "Client", FakeClient)
    monkeypatch.setattr(detail_mod, "_PROXY_CONFIG", {
        "host": "proxy.test", "port": "8080", "user": "u", "pass": "p",
    })

    stats = detail_mod.scrape_site_batch(
        None, "ExampleCorp", [("https://example.test/1", "Engineer")],
        delay=0)

    assert stats["ok"] == 1
    assert captured["proxy"] == "http://u:p@proxy.test:8080"
    assert captured["headers"]["User-Agent"] == detail_mod.UA
    assert captured["follow_redirects"] is True
