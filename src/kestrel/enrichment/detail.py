# SPDX-License-Identifier: AGPL-3.0-only
"""Detail page enrichment: fetches full descriptions and apply URLs.

For each job URL in the database, fetches the detail page over pure HTTP
(httpx — never a browser, spec art_VGFSMgRh) and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first) — unchanged pure functions
over the parsed soup:
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (1 LLM call)

A page whose DOM is a JS shell (minimal markup, no JSON-LD) stops with a
typed ``browser-needed`` detail_error steering the host agent to enrich
via ``kestrel agent enrich`` — never a relaunch, never a silent failure.
"""

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from kestrel import config
from kestrel.config import DB_PATH
from kestrel.database import (
    SKIP_DETAIL_CLAUSE,
    get_connection,
    init_db,
    ensure_columns,
)
from kestrel.enrichment.remote import infer_remote
from kestrel.enrichment.seniority import classify_seniority
from kestrel.llm import get_client

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Sites that block scraping -- skip detail extraction entirely. Canonical
# in database.SKIP_DETAIL_SITES: the scraper's WHERE, the pending_detail
# stat, the streaming enrich count, and the portal stage filter share one
# clause, so a skip-site row never counts as pending detail work in any
# surface (H3).

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from kestrel.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

def _load_base_urls() -> dict[str, str | None]:
    """Load site base URLs from config/sites.yaml."""
    from kestrel.config import load_base_urls
    return load_base_urls()


def resolve_url(raw_url: str, site: str) -> str | None:
    """Resolve a stored URL to an absolute URL."""
    if not raw_url:
        return None

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if site == "WelcomeToTheJungle":
        return None

    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"

    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None

    base = _load_base_urls().get(site)
    if not base:
        return None

    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]

    return urljoin(base, raw_url)


def resolve_all_urls(conn: sqlite3.Connection) -> dict:
    """Resolve all relative URLs in the database. Returns stats."""
    rows = conn.execute("SELECT url, site FROM jobs").fetchall()
    resolved = 0
    failed = 0
    already_absolute = 0
    collisions = 0

    for row in rows:
        url, site = row[0], row[1]
        if url.startswith("http://") or url.startswith("https://"):
            already_absolute += 1
            continue

        new_url = resolve_url(url, site)
        if new_url and new_url != url:
            try:
                conn.execute("UPDATE jobs SET url = ? WHERE url = ?", (new_url, url))
                resolved += 1
            except sqlite3.IntegrityError:
                # B5: another row already owns the absolute URL. Deleting that
                # row destroyed real data (possibly the richer record). Keep
                # both rows: leave the relative-url row untouched and flag the
                # collision for manual resolution.
                collisions += 1
                log.warning(
                    "URL-resolution collision: '%s' resolves to '%s' which already "
                    "exists in the DB — both rows kept, relative-url row flagged",
                    url, new_url,
                )
                conn.execute(
                    "UPDATE jobs SET detail_error = ? WHERE url = ?",
                    (f"url-collision: resolved url already exists ({new_url})", url),
                )
        else:
            failed += 1

    # Also resolve relative application_urls
    app_resolved = 0
    rows = conn.execute(
        "SELECT url, site, application_url FROM jobs "
        "WHERE application_url IS NOT NULL AND application_url != '' "
        "AND application_url NOT LIKE 'http%'"
    ).fetchall()
    for row in rows:
        url, site, app_url = row[0], row[1], row[2]
        new_app = resolve_url(app_url, site)
        if new_app and new_app != app_url:
            conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?", (new_app, url))
            app_resolved += 1

    conn.commit()
    return {"resolved": resolved, "failed": failed, "already_absolute": already_absolute,
            "app_resolved": app_resolved, "collisions": collisions}


# WTTJ note (spec art_VGFSMgRh): the old Algolia URL repair captured the
# network response off the wire inside a Playwright browser and is gone with
# the browser itself. Relative WelcomeToTheJungle URLs have no in-process
# resolver — resolve_url() returns None for them, so they stay pending and
# reach the host agent's enrich lane (`kestrel agent enrich`) instead.

# -- Detail page intelligence ------------------------------------------------

def collect_detail_intelligence(soup: BeautifulSoup, final_url: str) -> dict:
    """Collect signals from parsed detail-page HTML. Lighter than discovery --
    no API interception."""
    intel: dict = {"json_ld": [], "page_title": "", "final_url": final_url}

    if soup.title:
        intel["page_title"] = soup.title.get_text(strip=True)

    for el in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(el.string or el.get_text())
            intel["json_ld"].append(data)
        except (json.JSONDecodeError, TypeError):
            # Malformed embedded JSON is common on real pages: skip that
            # block, keep the rest.
            pass

    return intel


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def extract_from_json_ld(intel: dict) -> dict | None:
    """Extract description and apply URL from JSON-LD JobPosting.
    Returns {"full_description": str, "application_url": str|None} or None."""

    def find_job_posting(data):
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    result = find_job_posting(item)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = find_job_posting(item)
                if result:
                    return result
        return None

    for ld in intel.get("json_ld", []):
        posting = find_job_posting(ld)
        if not posting:
            continue

        desc = posting.get("description", "")
        if not desc:
            continue

        desc_clean = clean_description(desc)
        if len(desc_clean) < 50:
            continue

        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")

        return {
            "full_description": desc_clean,
            "application_url": apply_url,
        }

    return None


# -- Tier 2: Deterministic pattern matching ----------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a[data-testid*="apply"]',
    'a[class*="apply"]',
    'a[aria-label*="pply"]',
    'button[data-testid*="apply"]',
    'a#apply_button',
    '.postings-btn-wrapper a',
    'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]',
    'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]',
    'a[class*="apply-btn"]',
    'a[class*="apply-button"]',
]

DESCRIPTION_SELECTORS = [
    '#job-description',
    '#job_description',
    '#jobDescriptionText',
    '.job-description',
    '.job_description',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-testid*="description"]',
    '[data-testid="job-description"]',
    '.posting-page .posting-categories + div',
    '#content .posting-page',
    '#app_body .content',
    '#grnhse_app .content',
    '.ashby-job-posting-description',
    '[class*="posting-description"]',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '[class*="job-content"]',
    '[class*="job-body"]',
    '[role="main"] article',
    'main article',
    'article[class*="job"]',
    '.job-posting-content',
]


def extract_apply_url_deterministic(soup: BeautifulSoup, base_url: str) -> str | None:
    """Try known CSS patterns for apply buttons/links."""
    for sel in APPLY_SELECTORS:
        try:
            el = soup.select_one(sel)
        except Exception:  # noqa: BLE001 - an unsupported selector is a skip, not a crash
            continue
        if not el:
            continue

        href = el.get("href")
        if href and href != "#":
            return href
        if el.name == "button":
            parent_anchor = el.parent.find("a") if el.parent else None
            if parent_anchor and parent_anchor.get("href"):
                return parent_anchor["href"]
            # Old behavior: a bare apply button means the detail page itself
            # is the application entry point.
            return base_url

    for link in soup.find_all("a"):
        text = link.get_text(strip=True).lower()
        if "apply" in text and len(text) < 50:
            href = link.get("href")
            if href and href != "#" and "javascript:" not in href:
                return href

    return None


def extract_description_deterministic(soup: BeautifulSoup) -> str | None:
    """Try known CSS patterns for the job description block."""
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = soup.select_one(sel)
        except Exception:  # noqa: BLE001 - an unsupported selector is a skip, not a crash
            continue
        if el:
            text = el.get_text().strip()
            if len(text) >= 100:
                return clean_description(text)

    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = """You are extracting job details from a single job posting page.

PAGE URL: {url}
PAGE TITLE: {title}

Find TWO things in the HTML below:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link

Rules:
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
- If you cannot find one, set it to null

Return ONLY valid JSON:
{{"full_description": "the complete job description text here", "application_url": "https://..." or null}}

No explanation, no markdown. Keep reasoning under 20 words.

HTML:
{content}"""


def extract_main_content(soup: BeautifulSoup) -> str:
    """Extract the main content area, stripped of navigation noise."""
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = soup.select_one(sel)
        except Exception:  # noqa: BLE001 - an unsupported selector is a skip, not a crash
            continue
        if el:
            text_len = len(el.get_text(strip=True))
            if text_len > 200:
                html = str(el)
                if len(html) < 50000:
                    return clean_content_html(html)

    body = soup.body
    if body is None:
        return ""
    clone = BeautifulSoup(str(body), "html.parser")
    for tag in clone.select("nav, header, footer, script, style, noscript, svg, iframe"):
        tag.decompose()
    return clean_content_html(str(clone)[:50000])


def clean_content_html(html: str) -> str:
    """Clean detail page HTML for LLM consumption."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs

    return str(soup)


def extract_with_llm(soup: BeautifulSoup, url: str) -> dict:
    """Send focused HTML to LLM for extraction. Fallback tier."""
    content = extract_main_content(soup)
    if not content:
        return {"full_description": None, "application_url": None}

    title = soup.title.get_text(strip=True) if soup.title else ""

    prompt = DETAIL_EXTRACT_PROMPT.format(
        url=url,
        title=title,
        content=content[:30000],
    )

    try:
        client = get_client()
        t0 = time.time()
        raw = client.ask(prompt, temperature=0.0, max_tokens=4096)
        elapsed = time.time() - t0
        log.info("LLM: %d chars in, %.1fs", len(prompt), elapsed)

        from kestrel.discovery.smartextract import extract_json
        result = extract_json(raw)
        desc = result.get("full_description")
        apply_url = result.get("application_url")

        if desc:
            desc = clean_description(desc)

        return {"full_description": desc, "application_url": apply_url}
    except Exception as e:
        log.error("LLM ERROR: %s", e)
        return {"full_description": None, "application_url": None}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

PERMANENT_FAILURES = {404, 410, 451}

# JS-shell detection: a fetched page this sparse carries no server-rendered
# posting — the content needs a browser to render, which Kestrel does not
# have (spec art_VGFSMgRh). The row stops with a typed browser-needed
# hand-off to the host agent instead of an empty extract or a relaunch.
JS_SHELL_MAX_TAGS = 60
JS_SHELL_MAX_TEXT = 300
BROWSER_NEEDED_ERROR = "browser-needed"


def _fetch_html(client: httpx.Client, url: str) -> httpx.Response:
    """One HTTP GET of the detail page — the transport seam tests stub."""
    return client.get(url)


def _looks_like_js_shell(soup: BeautifulSoup, intel: dict) -> bool:
    """True when the fetched DOM is too sparse to be a server-rendered posting."""
    if intel.get("json_ld"):
        return False
    if extract_description_deterministic(soup):
        return False
    tag_count = len(soup.find_all())
    text_len = len(soup.get_text(strip=True))
    return tag_count < JS_SHELL_MAX_TAGS or text_len < JS_SHELL_MAX_TEXT


def scrape_detail_page(client: httpx.Client, url: str) -> dict:
    """Full cascade for one detail page, fetched over pure HTTP."""
    result: dict = {
        "full_description": None,
        "application_url": None,
        "status": "error",
        "tier_used": None,
        "error": None,
    }
    t0 = time.time()

    try:
        resp = _fetch_html(client, url)
    except httpx.TimeoutException:
        result["error"] = "timeout"
        result["elapsed"] = time.time() - t0
        return result
    except httpx.HTTPError as e:
        result["error"] = str(e)[:200]
        result["elapsed"] = time.time() - t0
        return result

    if resp.status_code in PERMANENT_FAILURES:
        result["error"] = f"HTTP {resp.status_code}"
        result["elapsed"] = time.time() - t0
        return result

    soup = BeautifulSoup(resp.text, "html.parser")
    final_url = str(resp.url)
    intel = collect_detail_intelligence(soup, final_url)

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(soup, final_url)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(soup)
    apply = extract_apply_url_deterministic(soup, final_url)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    # JS shells stop here: no JSON-LD, no deterministic hit, minimal DOM.
    # No relaunch — the host agent's browser is the only render path.
    if _looks_like_js_shell(soup, intel):
        result["error"] = (
            f"{BROWSER_NEEDED_ERROR}: enrich via 'kestrel agent enrich --url {url}'"
        )
        result["elapsed"] = time.time() - t0
        return result

    # Tier 3: LLM
    llm_result = extract_with_llm(soup, url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    result["elapsed"] = time.time() - t0
    return result


def _valid_http_url(value: str) -> bool:
    """True only for a real http(s) URL (the B7 validity bar)."""
    return value.startswith(("http://", "https://"))


def _clean_apply_url(value: str | None) -> str | None:
    """Junk-normalize a caller-supplied application URL (B7).

    '' and the sentinel family ('none'/'null'/'n/a', case-insensitive —
    the B1 pollution class) mean "no URL given": NULL, so the write's
    COALESCE preserves whatever good URL the row already carries.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() in ("none", "null", "n/a"):
        return None
    return text


def persist_description(conn: sqlite3.Connection, url: str,
                        full_description: str | None,
                        application_url: str | None, title: str | None) -> None:
    """The single description-capture write: one UPDATE that stamps the
    captured description, the apply URL, and the is_remote inference,
    and clears any prior detail error.

    The scrape loop (below) and the host enrich verb (spec art_IMViYTMU)
    both land here — the host path writes through the same UPDATE the
    fallback tier's result would, so no parallel schema for host data.

    Field semantics (B7/M1):
      - application_url is preserve-on-absent: a call without the URL
        never clobbers an existing stored one (the host verb may omit it,
        a partial scrape never has one), junk sentinels normalize to
        NULL, and any other non-http(s) value is refused rather than
        written.
      - a NULL full_description stamps detail_error='no description
        extracted' instead of a clean settle — the row stays visible in
        detail_errors instead of vanishing from every queue and counter.
    """
    normalized_url = _clean_apply_url(application_url)
    if normalized_url is not None and not _valid_http_url(normalized_url):
        raise ValueError(
            f"persist_description: refusing non-http(s) application_url "
            f"{application_url!r} for {url} — pass the apply URL or None"
        )

    conn.execute(
        "UPDATE jobs SET full_description = ?, "
        "application_url = COALESCE(?, application_url), "
        "detail_scraped_at = ?, "
        "detail_error = CASE WHEN ? IS NULL "
        "THEN COALESCE(detail_error, 'no description extracted') ELSE NULL END, "
        "is_remote = ?, "
        "seniority = COALESCE(seniority, ?) "
        "WHERE url = ?",
        (full_description, normalized_url,
         datetime.now(timezone.utc).isoformat(),
         full_description,
         1 if infer_remote(title or "", full_description) else 0,
         classify_seniority(title, full_description),
         url),
    )
    conn.commit()


def _httpx_proxy_url(proxy_config: dict) -> str:
    """Render the shared proxy config in httpx form (http://user:pass@host:port).

    Same components the Playwright proxy setting consumed, in the form the
    pure-HTTP transport speaks."""
    host, port = proxy_config["host"], proxy_config["port"]
    user, passwd = proxy_config.get("user"), proxy_config.get("pass")
    if user and passwd:
        return f"http://{quote(str(user))}:{quote(str(passwd))}@{host}:{port}"
    return f"http://{host}:{port}"


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 2.0,
    max_jobs: int | None = None,
) -> dict:
    """Process all jobs for one site over pure HTTP (no browser, spec
    art_VGFSMgRh). One httpx client per site batch; JS-shell pages stop with
    a browser-needed detail_error for the host agent.

    If conn is None, creates its own DB connection.
    """
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    if max_jobs:
        jobs = jobs[:max_jobs]

    if not jobs:
        return stats

    own_conn = conn is None
    if own_conn:
        conn = init_db()

    now = datetime.now(timezone.utc).isoformat()

    client_kwargs: dict = {
        "headers": {"User-Agent": UA},
        "timeout": 45.0,
        "follow_redirects": True,
    }
    if _PROXY_CONFIG:
        client_kwargs["proxy"] = _httpx_proxy_url(_PROXY_CONFIG)

    opened = False
    try:
        with httpx.Client(**client_kwargs) as client:
            opened = True
            for i, (url, title) in enumerate(jobs):
                # Dogfood 2026-11-09: one bad page (a crashed tab, a DB
                # hiccup) aborted the whole batch — every later job in
                # the site stayed unscraped. One job's failure must
                # cost one row, not the site.
                try:
                    stats["processed"] += 1  # one attempt = one count, wherever it fails
                    result = scrape_detail_page(client, url)

                    tier = result.get("tier_used")
                    status = result["status"]
                    elapsed = result.get("elapsed", 0)

                    if tier:
                        stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                    tier_str = f"T{tier}" if tier else "--"
                    desc_len = len(result.get("full_description") or "")
                    apply_str = "yes" if result.get("application_url") else "no"
                    err_str = f" | err={result.get('error')}" if result.get("error") else ""

                    log.info("  %s | %s | desc=%s chars | apply=%s | %.1fs%s",
                             status, tier_str, f"{desc_len:,}", apply_str, elapsed, err_str)

                    if status in ("ok", "partial"):
                        stats[status] += 1
                        persist_description(conn, url,
                                            result.get("full_description"),
                                            result.get("application_url"), title)
                    else:
                        stats["error"] += 1
                        conn.execute(
                            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
                            (result.get("error", "unknown"), now, url),
                        )

                    conn.commit()
                except Exception as e:  # noqa: BLE001 - one row must not sink the batch
                    stats["error"] += 1
                    log.error("%s: job %d/%d crashed, batch continues: %s",
                              site, i + 1, len(jobs), e)
                    try:
                        conn.execute(
                            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? "
                            "WHERE url = ?",
                            (f"enrich-crash: {str(e)[:180]}", now, url),
                        )
                        conn.commit()
                    except Exception:
                        conn.rollback()

                if i < len(jobs) - 1:
                    time.sleep(delay)
    except Exception as e:  # noqa: BLE001 - a client-setup crash must record, not vanish
        if not opened:
            # Dogfood 2026-11-09: the setup (client construction, proxy
            # wiring) sat outside the per-job loop, so a setup crash left
            # every job in the batch with NO detail_error at all. Stamp the
            # whole batch and return — per-request failures are contained
            # per-job inside scrape_detail_page instead.
            reason = f"enrich-setup-failed: {str(e)[:180]}"
            log.error("%s: batch setup failed, %d job(s) recorded as failed: %s",
                      site, len(jobs), e)
            for url, _title in jobs:
                conn.execute(
                    "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? "
                    "WHERE url = ?",
                    (reason, now, url),
                )
            conn.commit()
            stats["error"] += len(jobs)
        else:
            raise  # past setup: the per-job guard already contained the failure
    finally:
        if own_conn:
            conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    """Groups pending jobs by site and processes each batch.

    Sequential by default. When workers > 1, processes multiple site batches
    in parallel using ThreadPoolExecutor (each thread gets its own HTTP
    client and DB connection).

    Returns aggregate stats dict.
    """
    skip_filter = SKIP_DETAIL_CLAUSE
    where = f"WHERE detail_scraped_at IS NULL AND {skip_filter}"
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    log.info("Pending: %d jobs across %d sites (workers=%d)", len(rows), len(site_jobs), workers)
    for site, jobs in site_jobs.items():
        log.info("  %s: %d jobs", site, len(jobs))

    known_order = [
        "RemoteOK", "Job Bank Canada", "BuiltIn Remote",
        "WelcomeToTheJungle", "CareerJet Canada", "Hacker News Jobs",
    ]
    order = [s for s in known_order if s in site_jobs]
    order += [s for s in sorted(site_jobs.keys()) if s not in order]

    total_stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    def _merge_stats(stats: dict) -> None:
        for k in ("processed", "ok", "partial", "error"):
            total_stats[k] += stats[k]
        for t, count in stats["tiers"].items():
            total_stats["tiers"][t] = total_stats["tiers"].get(t, 0) + count

    if workers > 1 and len(order) > 1:
        # Parallel mode: each site batch runs in its own thread with its own
        # DB connection (conn=None tells scrape_site_batch to create one)
        def _scrape_site(site: str) -> dict:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)
            stats = scrape_site_batch(None, site, jobs, delay=delay, max_jobs=max_per_site)
            log.info("%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     site, stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))
            return stats

        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            futures = {pool.submit(_scrape_site, site): site for site in order}
            for future in as_completed(futures):
                try:
                    _merge_stats(future.result())
                except Exception as e:  # noqa: BLE001 - one site must not sink the run
                    site = futures[future]
                    total_stats["site_errors"] = total_stats.get("site_errors", 0) + 1
                    log.error("%s: batch CRASHED (remaining sites continue): %s", site, e)
    else:
        # Sequential mode (default)
        for site in order:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)

            try:
                stats = scrape_site_batch(conn, site, jobs, delay=delay, max_jobs=max_per_site)
            except Exception as e:  # noqa: BLE001 - one site must not sink the run
                # Dogfood 2026-11-09: the parallel path caught per-site
                # crashes but this path did not — a single failed batch
                # aborted enrichment with every later site's jobs stranded.
                total_stats["site_errors"] = total_stats.get("site_errors", 0) + 1
                log.error("%s: batch CRASHED (remaining sites continue): %s", site, e)
                continue
            _merge_stats(stats)

            log.info("Site summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))

    log.info("TOTAL: %d processed | %d ok | %d partial | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["partial"], total_stats["error"])
    log.info("Tier distribution: T1=%d T2=%d T3=%d",
             total_stats["tiers"].get(1, 0), total_stats["tiers"].get(2, 0), total_stats["tiers"].get(3, 0))

    llm_calls = total_stats["tiers"].get(3, 0)
    total = total_stats["processed"]
    if total > 0:
        savings = ((total - llm_calls) / total) * 100
        log.info("LLM calls: %d/%d (%.0f%% saved)", llm_calls, total, savings)

    return total_stats


# -- Streaming detail scraper (for sequential pipeline) ----------------------

def stream_detail(
    upstream_done,
    my_done,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs, scrapes sites sequentially.

    Args:
        upstream_done: Event set when discover+extract done. None = run once.
        my_done: Event to set when this stage completes.
        proxy_str: Proxy in host:port:user:pass format.
        poll_interval: Seconds to sleep when no pending jobs found.
    """
    if proxy_str:
        set_proxy(proxy_str)

    conn = init_db()

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute",
             url_stats['resolved'], url_stats['already_absolute'])

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            skip_filter = SKIP_DETAIL_CLAUSE
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                f"WHERE detail_scraped_at IS NULL AND {skip_filter} "
                "ORDER BY site LIMIT 200"
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                for site, jobs in site_jobs.items():
                    delay = SITE_DELAYS.get(site, 2.0)
                    log.info("%s: %d jobs (delay=%.1fs)", site, len(jobs), delay)

                    try:
                        stats = scrape_site_batch(conn, site, jobs, delay=delay)
                        total_ok += stats["ok"] + stats["partial"]
                        total_err += stats["error"]
                        log.info("%s: %d ok, %d partial, %d error",
                                 site, stats['ok'], stats['partial'], stats['error'])
                    except Exception as e:
                        log.error("%s: CRASHED: %s", site, e)

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        if total_ok or total_err:
            log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        conn.close()
        my_done.set()


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 1) -> dict:
    """Main entry point for detail page enrichment.

    Fetches pending jobs from the database (those without full_description),
    resolves relative URLs, then runs the three-tier extraction cascade on
    each detail page.

    Args:
        limit: Maximum number of jobs per site to process.
        workers: Number of parallel threads for site batch processing. Default 1 (sequential).

    Returns:
        Dict with stats: processed, ok, partial, error, tiers.
    """
    conn = init_db()

    # URL resolution first
    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    # Run the detail scraper
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)

    return stats
