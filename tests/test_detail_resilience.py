# SPDX-License-Identifier: AGPL-3.0-only
"""Detail-scraper crash isolation (dogfood 2026-11-09).

The description-fetch stage aborted on the first exception its batch met:
one bad page killed every later job in the site, and one failed site
batch killed every later site — the run "finished" with most pending
jobs never attempted. These tests pin the isolation contract:

- one job's crash costs that row (detail_error stamped, batch continues);
- one site's crash costs that site (site_errors counted, run continues).

Both paths run against a fake Playwright page that extracts nothing, so
every job deterministically reaches the cascade's error path — the tests
observe routing and isolation, not extraction quality.
"""

import contextlib
import json
import types

from kestrel.database import init_db
from kestrel.enrichment import detail as detail_mod


def _seed_pending(conn, url: str, site: str, title: str = "Engineer") -> None:
    conn.execute(
        "INSERT INTO jobs (url, title, site, strategy, discovered_at) "
        "VALUES (?, ?, ?, 'test', '2026-11-09T00:00:00Z')",
        (url, title, site),
    )
    conn.commit()


class _FakePage:
    """Minimal Playwright-page double: extracts nothing, crashes on cue."""

    def __init__(self, crash_on_url: str | None = None,
                 crash_on_title: bool = False,
                 json_ld: tuple = ()):
        self._crash_on_url = crash_on_url
        self._crash_on_title = crash_on_title
        self._json_ld = json_ld  # urls that "serve" a JSON-LD JobPosting
        self.url = "https://example.test/job"

    def goto(self, url, timeout=None):
        if url == self._crash_on_url:
            raise RuntimeError("page crashed")
        return types.SimpleNamespace(status=200)

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def title(self):
        if self._crash_on_title:
            raise RuntimeError("execution context destroyed")
        return "Engineer — Example"

    def query_selector_all(self, selector):
        if self._json_ld and selector == 'script[type="application/ld+json"]':
            posting = json.dumps({
                "@type": "JobPosting",
                "description": "<p>Full description body long enough to "
                               "pass the fifty character floor in the "
                               "tier-one extraction cascade.</p>",
                "url": "https://example.test/apply",
            })
            return [types.SimpleNamespace(inner_text=lambda p=posting: p)]
        return []

    def query_selector(self, selector):
        return None

    def evaluate(self, script):
        raise RuntimeError("no main content")


def _fake_playwright(page):
    """A sync_playwright stand-in handing out a browser holding `page`."""
    context = types.SimpleNamespace(new_page=lambda: page)
    browser = types.SimpleNamespace(
        new_context=lambda user_agent=None: context, close=lambda: None)
    pw = types.SimpleNamespace(
        chromium=types.SimpleNamespace(launch=lambda **kwargs: browser))
    return lambda: contextlib.nullcontext(pw)


def test_scrape_site_batch_continues_after_one_job_crashes(isolated_db,
                                                           monkeypatch):
    conn = init_db()
    urls = ("https://example.test/1", "https://example.test/2",
            "https://example.test/3")
    for url in urls:
        _seed_pending(conn, url, "ExampleCorp")

    # Crash the persistence step for job 2: persist_description runs
    # outside scrape_detail_page's internal try, so the exception escapes
    # to the batch's outer guard. Jobs must extract successfully (JSON-LD
    # tier) to reach persistence — a goto failure is handled inside the
    # cascade instead (detail_error path in scrape_detail_page).
    real_persist = detail_mod.persist_description

    def _boom_on_two(conn, url, full_description, application_url, title):
        if url == urls[1]:
            raise RuntimeError("db write failed")
        real_persist(conn, url, full_description, application_url, title)

    monkeypatch.setattr(detail_mod, "persist_description", _boom_on_two)
    page = _FakePage(json_ld=urls)
    monkeypatch.setattr(detail_mod, "sync_playwright", _fake_playwright(page))

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


def test_scrape_site_batch_survives_crash_in_intelligence_collection(
        isolated_db, monkeypatch):
    # collect_detail_intelligence runs OUTSIDE scrape_detail_page's own
    # try-block — a dead page context must not escape the batch either.
    conn = init_db()
    urls = ("https://example.test/a", "https://example.test/b")
    for url in urls:
        _seed_pending(conn, url, "ExampleCorp")

    page = _FakePage(crash_on_title=True)
    monkeypatch.setattr(detail_mod, "sync_playwright", _fake_playwright(page))

    stats = detail_mod.scrape_site_batch(
        conn, "ExampleCorp", [(url, "Engineer") for url in urls], delay=0)

    assert stats["processed"] == 2
    assert stats["error"] == 2


def test_run_detail_scraper_continues_after_site_batch_crashes(
        isolated_db, monkeypatch):
    conn = init_db()
    _seed_pending(conn, "https://aaa.test/1", "AAA")
    _seed_pending(conn, "https://aaa.test/2", "AAA")
    _seed_pending(conn, "https://bbb.test/1", "BBB")

    def _boom(conn, site, jobs, **kwargs):
        if site == "AAA":
            raise RuntimeError("browser died")
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
            raise RuntimeError("browser died")
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

    page = _FakePage()
    monkeypatch.setattr(detail_mod, "sync_playwright", _fake_playwright(page))

    stats = detail_mod.scrape_site_batch(
        None, "ExampleCorp", [("https://example.test/own", "Engineer")],
        delay=0)
    assert stats["processed"] == 1
