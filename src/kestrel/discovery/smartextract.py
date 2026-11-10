# SPDX-License-Identifier: AGPL-3.0-only
"""AI-powered smart extraction: discovers jobs from arbitrary websites.

Two-phase approach:
  Phase 1: Lightweight intelligence (JSON-LD, API responses, data-testids, DOM stats)
           -> LLM picks the best extraction strategy
  Phase 2: Only for CSS selectors -- the LLM sees cleaned page HTML and
           generates selectors; they run over the stored HTML.

JSON-LD and API strategies execute directly from stored data -- no LLM needed.

Acquisition has two lanes and Kestrel never launches a browser (spec
art_VGFSMgRh):
  Lane 1 -- the host agent captures pages in its own browser and posts them
  back through `kestrel agent page-capture`; submitted intelligence is
  stored (page_captures) and consumed by this pipeline.
  Lane 2 -- pure HTTP (httpx + BeautifulSoup) for static pages; a JS shell
  escalates to a typed browser_needed hand-off pointing at Lane 1.

Sites are loaded from config/sites.yaml, with {query_encoded} and {location_encoded}
placeholders replaced from the user's search configuration.
"""

import json
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import yaml
from bs4 import BeautifulSoup

from kestrel import config
from kestrel.config import CONFIG_DIR
from kestrel.database import (
    get_connection,
    get_page_capture,
    get_stats,
    init_db,
    record_capture_needed,
    store_jobs,
)
from kestrel.discovery.filters import location_filters, location_ok as _location_ok
from kestrel.llm import get_client

log = logging.getLogger(__name__)

# Fix Windows encoding -- prevents charmap errors on emoji/unicode in job titles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# -- Location filtering -------------------------------------------------------

def _load_location_filter(search_cfg: dict | None = None):
    """Load location accept/reject lists from search config (shared impl)."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    return location_filters(search_cfg)


# -- Site configuration from YAML --------------------------------------------

def load_sites() -> list[dict]:
    """Load scraping target sites from config/sites.yaml."""
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        log.warning("sites.yaml not found at %s", path)
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("sites", [])


def _store_jobs_filtered(
    conn: sqlite3.Connection,
    jobs: list[dict],
    site: str,
    strategy: str,
    accept_locs: list[str],
    reject_locs: list[str],
) -> tuple[int, int]:
    """Store jobs with location filtering. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    filtered = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        if not _location_ok(job.get("location"), accept_locs, reject_locs, url=url):
            filtered += 1
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    if filtered:
        log.info("Filtered %d jobs (wrong location)", filtered)
    conn.commit()
    return new, existing


# -- Page intelligence collector (pure functions over fetched HTML) ----------

# JS-shell detection contract: a page whose cleaned HTML carries fewer than
# this many characters is a client-rendered shell — over HTTP it has no job
# content, so it escalates to the host agent's browser (Lane 1) instead of
# any Kestrel-side relaunch. 5000 was the old headful-retry threshold; the
# enrichment side's browser-needed hand-off uses the same vocabulary.
_JS_SHELL_MIN_CLEANED_CHARS = 5000

_CAPTCHA_SIGNALS = ["captcha", "are you a human", "verify you", "unusual requests",
                    "access denied", "please verify", "bot detection"]


def build_intel_from_html(url: str, html: str) -> dict:
    """Build the same intelligence dict the old browser collector produced,
    from any HTML string — HTTP-fetched or host-submitted via
    `kestrel agent page-capture`. Pure function over its inputs: JSON-LD,
    __NEXT_DATA__, data-testid inventory, DOM stats, card candidates, and
    the raw HTML the CSS-selector phase needs.
    """
    intel: dict = {
        "url": url,
        "json_ld": [],
        "api_responses": [],
        "data_testids": [],
        "page_title": "",
        "dom_stats": {},
        "card_candidates": [],
        "full_html": html or "",
    }
    if not html:
        return intel

    soup = BeautifulSoup(html, "html.parser")

    if soup.title and soup.title.string:
        intel["page_title"] = soup.title.string.strip()

    # 1. JSON-LD
    for el in soup.find_all("script", type="application/ld+json"):
        try:
            intel["json_ld"].append(json.loads(el.string or ""))
        except (ValueError, TypeError):
            continue

    # 2. __NEXT_DATA__
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        try:
            intel["next_data"] = json.loads(next_data.string or "")
        except (ValueError, TypeError):
            pass

    # 3. data-testid attributes
    for el in soup.select("[data-testid]")[:50]:
        intel["data_testids"].append({
            "testid": el.get("data-testid", ""),
            "tag": el.name,
            "text": el.get_text(" ", strip=True)[:80],
        })

    # 4. DOM stats
    intel["dom_stats"] = _dom_stats(soup)

    # 5. Repeating card-like elements
    intel["card_candidates"] = _card_candidates(soup)

    return intel


def _dom_stats(soup: BeautifulSoup) -> dict:
    """Element counts the strategy briefing reports (ported from the old
    in-browser evaluate)."""
    body = soup.body if soup.body else soup
    return {
        "total_elements": len(body.find_all(True)),
        "links": len(body.select("a[href]")),
        "headings": len(body.select("h1,h2,h3,h4")),
        "lists": len(body.select("ul,ol")),
        "tables": len(body.select("table")),
        "articles": len(body.select("article")),
        "has_data_ids": len(body.select("[data-id]")),
    }


def _selector_for(tag, classes: list[str] | None) -> str:
    """tag + #id + up to 3 short classes — the selector shape the old
    in-browser card detector emitted."""
    selector = tag.name
    if tag.get("id"):
        return selector + "#" + tag["id"]
    short = [c for c in (classes or []) if len(c) < 30][:3]
    if short:
        selector += "." + ".".join(short)
    return selector


def _card_candidates(soup: BeautifulSoup, max_candidates: int = 3) -> list[dict]:
    """Find repeating card-like element groups (ported from the old in-browser
    evaluate): parents whose dominant child tag repeats enough to be a list."""
    from collections import Counter

    candidates: list[dict] = []
    for parent in soup.find_all(True):
        children = parent.find_all(recursive=False)
        if len(children) < 3:
            continue

        tag, count = Counter(c.name for c in children).most_common(1)[0]
        if count < 3:
            continue

        repeating = [c for c in children if c.name == tag]
        with_text = [c for c in repeating if len(c.get_text(strip=True)) > 20]
        if len(with_text) < 3:
            continue

        with_links = [c for c in with_text if c.find("a", href=True)]
        score = len(with_links) * 2 + len(with_text)

        parent_classes = parent.get("class") or []
        child_classes = with_text[0].get("class") or []
        examples = []
        for card in with_text[:3]:
            clone = BeautifulSoup(str(card), "html.parser")
            for el in clone.find_all(["script", "style", "svg", "noscript"]):
                el.decompose()
            card_html = str(clone)
            examples.append(card_html if len(card_html) <= 5000
                            else card_html[:5000] + "...")

        candidates.append({
            "parent_selector": _selector_for(parent, parent_classes),
            "child_selector": _selector_for(with_text[0], child_classes),
            "child_tag": tag,
            "total_children": len(repeating),
            "with_text": len(with_text),
            "with_links": len(with_links),
            "score": score,
            "examples": examples,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:max_candidates]


def summarize_api_responses(captured: list[dict]) -> list[dict]:
    """Reduce raw captured responses ({url, status, size, data}) into the
    summary shape the strategy briefing and execute_api_response consume.
    Pure function — the same processing the old in-browser collector ran on
    its intercepted responses, now applied to host-submitted captures too."""
    summaries: list[dict] = []
    for resp in captured:
        data = resp.get("data")
        summary: dict = {
            "url": resp.get("url", "")[:200],
            "status": resp.get("status", "?"),
            "size": resp.get("size", len(json.dumps(data)) if data else 0),
            "_raw_data": data,
        }
        if data:
            if isinstance(data, list) and data:
                summary["type"] = f"array[{len(data)}]"
                if isinstance(data[0], dict):
                    summary["first_item_keys"] = list(data[0].keys())[:20]
                    summary["first_item_sample"] = {k: str(v)[:100] for k, v in list(data[0].items())[:8]}
            elif isinstance(data, dict):
                summary["type"] = "object"
                summary["keys"] = list(data.keys())[:20]

                def _explore_nested(obj, path_prefix, depth=0):
                    if depth > 3 or not isinstance(obj, dict):
                        return
                    for key in list(obj.keys())[:15]:
                        val = obj[key]
                        path = f"{path_prefix}.{key}" if path_prefix else key
                        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                            info = {
                                "count": len(val),
                                "first_item_keys": list(val[0].keys())[:20],
                                "first_item_sample": {k: str(v)[:200] for k, v in list(val[0].items())[:8]},
                            }
                            for subkey in list(val[0].keys())[:10]:
                                subval = val[0][subkey]
                                if isinstance(subval, list) and len(subval) > 0 and isinstance(subval[0], dict):
                                    info[f"first_item.{subkey}"] = {
                                        "count": len(subval),
                                        "first_item_keys": list(subval[0].keys())[:15],
                                        "first_item_sample": {k: str(v)[:100] for k, v in list(subval[0].items())[:8]},
                                    }
                                elif isinstance(subval, dict):
                                    info[f"first_item.{subkey}"] = {
                                        "type": "object",
                                        "keys": list(subval.keys())[:15],
                                        "sample": {k: str(v)[:150] for k, v in list(subval.items())[:8]},
                                    }
                            summary[f"nested_{path}"] = info
                        elif isinstance(val, dict) and depth < 3:
                            _explore_nested(val, path, depth + 1)
                _explore_nested(data, "")
        summaries.append(summary)
    return summaries


def collect_page_intelligence(url: str) -> dict:
    """Fetch a page over plain HTTP (Lane 2) and collect every signal the
    static HTML carries: JSON-LD, __NEXT_DATA__, data-testids, DOM stats,
    card candidates. XHR-backed API data is invisible to this lane — a
    caller that needs it gets it from a host page-capture instead. Kestrel
    launches no browser here or anywhere else: a JS-rendered shell comes
    back with nearly-empty intel and the caller escalates browser_needed.
    """
    response = httpx.get(url, headers={"User-Agent": UA},
                         timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    return build_intel_from_html(url, response.text)



# -- Judge: filter API responses ---------------------------------------------

JUDGE_PROMPT = """You are filtering intercepted API responses from a job listings website.
Decide if this API response contains actual job listing data (titles, companies, locations, etc).

API Response Summary:
  URL: {url}
  Status: {status}
  Size: {size} chars
  Type: {type}
  Keys/Fields: {fields}
  Sample: {sample}

Is this job listing data? Answer in under 10 words. Return ONLY valid JSON:
{{"relevant": true, "reason": "job objects with title/company"}}
or
{{"relevant": false, "reason": "auth endpoint"}}

No explanation, no markdown, no thinking."""


def judge_api_responses(api_responses: list[dict]) -> list[dict]:
    """Use the LLM to filter API responses, keeping only job-relevant ones."""
    if not api_responses:
        return []

    client = get_client()
    relevant: list[dict] = []

    for resp in api_responses:
        fields = ""
        sample = ""
        resp_type = resp.get("type", "unknown")
        if "first_item_keys" in resp:
            fields = str(resp["first_item_keys"])
            sample = json.dumps(resp.get("first_item_sample", {}), indent=2)[:500]
        elif "keys" in resp:
            fields = str(resp["keys"])
            for k, v in resp.items():
                if k.startswith("nested_"):
                    fields += f"\n  .{k.replace('nested_', '')}: {v.get('count', '?')} items, keys={v.get('first_item_keys', '?')}"
                    sample = json.dumps(v.get("first_item_sample", {}), indent=2)[:500]
        else:
            fields = "no structured data"

        prompt = JUDGE_PROMPT.format(
            url=resp.get("url", "?")[:200],
            status=resp.get("status", "?"),
            size=resp.get("size", "?"),
            type=resp_type,
            fields=fields,
            sample=sample or "n/a",
        )

        try:
            raw = client.ask(prompt, temperature=0.0, max_tokens=1024)
            verdict = extract_json(raw)
            is_relevant = verdict.get("relevant", False)
            reason = verdict.get("reason", "?")
            log.info("Judge: %s -> %s (%s)", resp.get("url", "?")[:80],
                     "KEEP" if is_relevant else "DROP", reason)
            if is_relevant:
                relevant.append(resp)
        except Exception as e:
            log.warning("Judge ERROR for %s: %s -- keeping", resp.get("url", "?")[:80], e)
            relevant.append(resp)

    return relevant


# -- Phase 1: strategy selection ---------------------------------------------

def format_strategy_briefing(intel: dict) -> str:
    """Lightweight briefing for strategy selection. No raw DOM."""
    sections: list[str] = []
    sections.append(f"PAGE: {intel['url']}")
    sections.append(f"TITLE: {intel['page_title']}")

    # JSON-LD
    if intel["json_ld"]:
        job_postings = [j for j in intel["json_ld"] if isinstance(j, dict) and j.get("@type") == "JobPosting"]
        other = [j for j in intel["json_ld"] if not (isinstance(j, dict) and j.get("@type") == "JobPosting")]
        if job_postings:
            sections.append(f"\nJSON-LD: {len(job_postings)} JobPosting entries found (usable!)")
            sections.append(f"First JobPosting:\n{json.dumps(job_postings[0], indent=2)[:3000]}")
        else:
            sections.append(f"\nJSON-LD: NO JobPosting entries (json_ld strategy will NOT work)")
        if other:
            types = [j.get("@type", "?") if isinstance(j, dict) else "?" for j in other]
            sections.append(f"Other JSON-LD types (NOT job data): {types}")
    else:
        sections.append("\nJSON-LD: none")

    # API responses
    if intel["api_responses"]:
        sections.append(f"\nAPI RESPONSES INTERCEPTED: {len(intel['api_responses'])} calls")
        for resp in intel["api_responses"]:
            sections.append(f"\n  URL: {resp['url']}")
            sections.append(f"  Status: {resp['status']} | Size: {resp['size']:,} chars | Type: {resp.get('type', '?')}")
            if "first_item_keys" in resp:
                sections.append(f"  Item keys: {resp['first_item_keys']}")
                sections.append(f"  Sample: {json.dumps(resp.get('first_item_sample', {}), indent=2)[:1000]}")
            if "keys" in resp:
                sections.append(f"  Object keys: {resp['keys']}")
            for k, v in resp.items():
                if k.startswith("nested_"):
                    arr_name = k.replace("nested_", "")
                    sections.append(f"  .{arr_name}: array of {v['count']} items")
                    sections.append(f"    Item keys: {v['first_item_keys']}")
                    sections.append(f"    Sample: {json.dumps(v.get('first_item_sample', {}), indent=2)[:1000]}")
                    for sk, sv in v.items():
                        if sk.startswith("first_item.") and isinstance(sv, dict):
                            sub_name = sk.replace("first_item.", "")
                            if "count" in sv:
                                sections.append(f"    .{arr_name}[0].{sub_name}: array of {sv['count']} items")
                                sections.append(f"      Item keys: {sv['first_item_keys']}")
                                sections.append(f"      Sample: {json.dumps(sv.get('first_item_sample', {}), indent=2)[:1500]}")
                            elif "keys" in sv:
                                sections.append(f"    .{arr_name}[0].{sub_name}: object with keys {sv['keys']}")
                                sections.append(f"      Sample: {json.dumps(sv.get('sample', {}), indent=2)[:1500]}")
    else:
        sections.append("\nAPI RESPONSES: none intercepted")

    # data-testid
    if intel["data_testids"]:
        sections.append(f"\nDATA-TESTID ATTRIBUTES: {len(intel['data_testids'])} elements")
        for dt in intel["data_testids"][:15]:
            text_preview = dt['text'].replace('\n', ' ')[:60]
            sections.append(f"  <{dt['tag']} data-testid=\"{dt['testid']}\"> {text_preview}")
    else:
        sections.append("\nDATA-TESTID: none found")

    # DOM stats
    stats = intel.get("dom_stats", {})
    sections.append(f"\nDOM STATS: {stats.get('total_elements', '?')} elements, "
                    f"{stats.get('links', '?')} links, {stats.get('headings', '?')} headings, "
                    f"{stats.get('tables', '?')} tables, {stats.get('articles', '?')} articles, "
                    f"{stats.get('has_data_ids', '?')} data-id elements")

    # Card candidates
    if intel["card_candidates"]:
        sections.append(f"\nREPEATING ELEMENTS DETECTED: {len(intel['card_candidates'])} candidate groups")
        for i, cand in enumerate(intel["card_candidates"]):
            sections.append(f"  [{i}] parent={cand['parent_selector']} child={cand['child_selector']} "
                          f"count={cand['total_children']} with_text={cand['with_text']} with_links={cand['with_links']}")
    else:
        sections.append("\nREPEATING ELEMENTS: none detected")

    return "\n".join(sections)


STRATEGY_PROMPT = """You are analyzing a job listings page to pick the best extraction strategy.

Below is a lightweight intelligence briefing -- JSON-LD data, intercepted API responses, data-testid attributes, and DOM statistics. NO raw DOM HTML is included.

Pick the BEST strategy:

1. "json_ld" -- ONLY if briefing shows JobPosting JSON-LD entries (it will say "usable!")
2. "api_response" -- ONLY if an intercepted API response has job-like fields (name, title, salary, description, location, slug)
3. "css_selectors" -- when neither JSON-LD nor API data has job data

HOW TO THINK:
- If the briefing says "JSON-LD: NO JobPosting entries" or "json_ld strategy will NOT work", do NOT pick json_ld.
- For api_response: "url_pattern" must be a substring that matches one of the INTERCEPTED API URLs listed above (not the page URL!). Copy a unique part of the API URL.
- For api_response: "items_path" must point to the ARRAY of items, not a single item. Use dot notation with [n] ONLY for traversing into a specific index to reach an inner array. Example: if data is {{"results": [{{"hits": [...]}}]}}, items_path is "results[0].hits" to reach the hits array.
- For api_response: field paths (title, salary, etc.) are RELATIVE TO EACH ITEM in the array. If items are nested objects like {{"_source": {{"Title": "..."}}}}, use "_source.Title" for the title field.
- For css_selectors: just return {{"strategy":"css_selectors","reasoning":"...","extraction":{{}}}} -- selectors will be generated in a separate focused step.

Return ONLY valid JSON:

For json_ld:
{{"strategy":"json_ld","reasoning":"...","extraction":{{"title":"title","salary":"baseSalary_path_or_null","description":"description","location":"jobLocation[0].address.addressCountry","url":"url_field"}}}}

For api_response:
{{"strategy":"api_response","reasoning":"...","extraction":{{"url_pattern":"actual.url.substring","items_path":"path.to.the.array","title":"field_in_each_item","salary":"salary_field_or_null","description":"description_field_or_null","location":"location_path","url":"url_field"}}}}

For css_selectors:
{{"strategy":"css_selectors","reasoning":"...","extraction":{{}}}}

Keep reasoning under 20 words. No explanation, no markdown, no code fences.

INTELLIGENCE BRIEFING:
{briefing}"""


# -- Card HTML cleaning (allowlist approach) ----------------------------------

_ALLOWED_ATTRS = {"id", "href", "data-testid", "data-id", "data-type", "data-slug",
                  "role", "aria-label", "aria-labelledby", "type", "name", "for"}
_ALLOWED_PREFIXES = ("data-", "aria-")
_UTILITY_CLASS_RE = re.compile(
    r"^("
    r"[a-z]{1,2}-\d+|"
    r"[a-z]{1,3}-[a-z]{1,3}-\d+|"
    r"col-\d+|"
    r"d-\w+|"
    r"align-\w+|justify-\w+|"
    r"flex-\w+|order-\d+|"
    r"text-\w+|font-\w+|"
    r"bg-\w+|border-\w+|"
    r"rounded-?\w*|shadow-?\w*|"
    r"w-\d+|h-\d+|"
    r"position-\w+|overflow-\w+|"
    r"float-\w+|clearfix|"
    r"visible-\w+|invisible|"
    r"sr-only|"
    r"css-[a-z0-9]+|"
    r"sc-[a-zA-Z]+|"
    r"sc-[a-f0-9]+-\d+"
    r")$"
)


def clean_card_html(html: str) -> str:
    """Strip layout noise from card HTML, keep only what the LLM needs for selectors."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in _ALLOWED_ATTRS or any(attr.startswith(p) for p in _ALLOWED_PREFIXES):
                new_attrs[attr] = val
            elif attr == "class":
                classes = val if isinstance(val, list) else val.split()
                kept = [c for c in classes if not _UTILITY_CLASS_RE.match(c)]
                if kept:
                    new_attrs["class"] = kept
        tag.attrs = new_attrs

    return str(soup)


def clean_page_html(html: str, max_chars: int = 150_000) -> str:
    """Strip full page HTML to essential structure for LLM card detection."""
    soup = BeautifulSoup(html, "html.parser")

    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main and len(str(main)) > 1000:
        soup = BeautifulSoup(str(main), "html.parser")

    for tag in soup.find_all(["script", "style", "svg", "noscript", "iframe",
                              "link", "meta", "head", "footer", "nav"]):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in _ALLOWED_ATTRS or any(attr.startswith(p) for p in _ALLOWED_PREFIXES):
                new_attrs[attr] = val
            elif attr == "class":
                classes = val if isinstance(val, list) else val.split()
                kept = [c for c in classes if not _UTILITY_CLASS_RE.match(c)]
                if kept:
                    new_attrs["class"] = kept
        tag.attrs = new_attrs

    for tag in soup.find_all(True):
        if not tag.get_text(strip=True) and not tag.find("img") and not tag.find("a"):
            tag.decompose()

    result = str(soup)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n<!-- TRUNCATED -->"
    return result


# -- Phase 2: CSS selector generation ----------------------------------------

FULL_PAGE_SELECTOR_PROMPT = """You are a senior web scraping engineer. Below is the cleaned HTML of a job listings page.

Your task:
1. Find the repeating HTML elements that represent individual job listings
2. Generate CSS selectors to extract data from them

Return a JSON object:
- "job_card": CSS selector matching each job card (MUST match ALL cards on the page)
- "title": selector RELATIVE to the card for the job title
- "salary": selector relative to card for salary, or null
- "description": selector relative to card for description snippet, or null
- "location": selector relative to card for location, or null
- "url": selector relative to card for the link (<a> tag) to the job detail page

Selector rules:
- SIMPLEST wins. A single attribute selector like [data-testid="job-card"] is better than a multi-level path like li > div > [data-testid="job-card"]. Do NOT add parent/ancestor selectors unless the target is ambiguous without them.
- For data-testid/data-id with DYNAMIC values (e.g. data-testid="card-123"), use prefix matching: [data-testid^="card-"]
- For data-testid with STATIC values (e.g. data-testid="job-card"), use exact: [data-testid="job-card"]
- Prefer semantic HTML: article, section, h2, h3 over div
- NEVER use hashed/generated classes: sc-*, css-*, random 5-8 char strings like "fJyWhK"
- Max 2 levels deep. One level is best.
- The "url" selector should target an <a> element (we extract its href attribute)
- If the page has NO job listings visible, return {{"error": "no job listings found"}}

Return ONLY valid JSON, no explanation, no markdown.

PAGE HTML:
{page_html}"""


# -- LLM helpers -------------------------------------------------------------

def ask_llm(prompt: str) -> tuple[str, float, dict]:
    """Send prompt to LLM. Returns (response_text, seconds_taken, metadata)."""
    client = get_client()
    t0 = time.time()
    text = client.ask(prompt, temperature=0.0, max_tokens=4096)
    elapsed = time.time() - t0
    meta = {
        "finish_reason": "stop",
        "prompt_chars": len(prompt),
        "response_chars": len(text),
    }
    return text, elapsed, meta


def extract_json(text: str) -> dict:
    """Extract JSON from LLM response, handling think tags and code fences."""
    if "<think>" in text:
        after = text.split("</think>")[-1].strip()
        if after:
            text = after
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = text.strip()
    text = re.sub(r'\\([^"\\\/bfnrtu])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    while text.endswith("}") or text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            text = text[:-1].rstrip()
    raise json.JSONDecodeError("Could not parse JSON", text, 0)


# -- JSON path resolution ---------------------------------------------------

def resolve_json_path_raw(data, path: str):
    """Navigate a JSON path and return whatever is there (including lists/dicts)."""
    if not path or not data:
        return None
    try:
        current = data
        for part in path.replace("[", ".[").split("."):
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                current = current[idx]
            else:
                current = current[part]
        return current
    except (KeyError, IndexError, TypeError):
        return None


def resolve_json_path(data, path: str):
    """Simple JSON path resolver with type coercion for display."""
    if not path or not data:
        return None
    try:
        current = data
        for part in path.replace("[", ".[").split("."):
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                current = current[idx]
            else:
                current = current[part]
        if isinstance(current, (str, int, float)):
            return str(current) if not isinstance(current, str) else current
        elif isinstance(current, dict):
            return current.get("name", current.get("text", str(current)[:100]))
        elif isinstance(current, list):
            if current and isinstance(current[0], dict):
                return ", ".join(str(item.get("name", item.get("text", ""))) for item in current[:3])
            return ", ".join(str(x) for x in current[:3])
        return str(current) if current else None
    except (KeyError, IndexError, TypeError):
        return None


# -- Extraction executors ----------------------------------------------------

def execute_json_ld(intel: dict, plan: dict) -> list[dict]:
    """Extract jobs from JSON-LD JobPosting entries."""
    ext = plan["extraction"]
    jobs: list[dict] = []
    for entry in intel["json_ld"]:
        if not isinstance(entry, dict) or entry.get("@type") != "JobPosting":
            continue
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url"]:
            path = ext.get(field)
            if not path or path == "null":
                job[field] = None
                continue
            job[field] = resolve_json_path(entry, path)
        jobs.append(job)
    return jobs


def execute_api_response(intel: dict, plan: dict) -> list[dict]:
    """Extract jobs from intercepted API response data."""
    ext = plan["extraction"]
    url_pattern = ext.get("url_pattern", "")

    target_data = None
    for resp in intel["api_responses"]:
        if url_pattern in resp.get("url", ""):
            target_data = resp.get("_raw_data")
            break

    if not target_data:
        log.warning("Could not find stored API response matching: %s", url_pattern)
        return []

    items_path = ext.get("items_path", "")
    items = resolve_json_path_raw(target_data, items_path)
    if not isinstance(items, list):
        log.warning("items_path '%s' did not resolve to a list (got %s)", items_path, type(items).__name__)
        return []

    jobs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url"]:
            path = ext.get(field)
            if not path or path == "null":
                job[field] = None
                continue
            job[field] = resolve_json_path(item, path)
        jobs.append(job)
    return jobs


def execute_css_selectors(intel: dict) -> tuple[dict, list[dict]]:
    """Phase 2: Send full cleaned page HTML to LLM for card detection + selector generation.
    Returns (selectors, jobs)."""
    full_html = intel.get("full_html", "")
    if not full_html:
        log.warning("No page HTML captured")
        return {}, []

    cleaned = clean_page_html(full_html)
    log.info("Page HTML: %s -> %s chars", f"{len(full_html):,}", f"{len(cleaned):,}")

    prompt = FULL_PAGE_SELECTOR_PROMPT.format(page_html=cleaned)

    try:
        raw, elapsed, meta = ask_llm(prompt)
    except Exception as e:
        log.error("LLM_ERROR in Phase 2: %s", e)
        return {}, []

    log.info("Phase 2 LLM: %d chars, %.1fs", meta['response_chars'], elapsed)

    try:
        selectors = extract_json(raw)
    except Exception as e:
        log.error("PARSE_ERROR in Phase 2: %s | raw: %s", e, raw[:500])
        return {}, []

    if "error" in selectors:
        log.warning("LLM: %s", selectors["error"])
        return selectors, []

    log.info("Selectors: %s", selectors)

    # Apply selectors to the ORIGINAL full_html
    soup = BeautifulSoup(full_html, "html.parser")
    card_sel = selectors.get("job_card", "NONE")
    try:
        cards = soup.select(card_sel)
    except Exception as e:
        log.error("Invalid card selector '%s': %s", card_sel, e)
        return selectors, []

    log.info("Matched %d cards", len(cards))

    jobs: list[dict] = []
    for card in cards:
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url"]:
            sel = selectors.get(field)
            if not sel or sel == "null":
                job[field] = None
                continue
            try:
                el = card.select_one(sel)
            except Exception:
                job[field] = None
                continue
            if el:
                job[field] = el.get("href") if field == "url" else el.get_text(strip=True)
            else:
                job[field] = None
        jobs.append(job)
    return selectors, jobs


# -- Main per-site extraction ------------------------------------------------

def _is_js_shell(intel: dict) -> bool:
    """True when the fetched HTML is a client-rendered shell: nearly no
    cleaned content and no CAPTCHA interstitial explaining why. Such a page
    escalates to the host agent's browser — never a Kestrel-side relaunch."""
    full_html = intel.get("full_html", "")
    if not full_html:
        return True
    if any(s in full_html.lower() for s in _CAPTCHA_SIGNALS):
        # Blocked, not a shell — no point asking the host agent to render it.
        log.warning("CAPTCHA/rate-limit detected — not escalating to page-capture")
        return False
    cleaned = clean_page_html(full_html)
    return len(cleaned) < _JS_SHELL_MIN_CLEANED_CHARS


def _run_one_site(name: str, url: str) -> dict:
    """Run full smart extraction pipeline on one site URL."""
    log.info("=" * 60)
    log.info("%s: %s", name, url)

    # Step 1: Intelligence — a persisted agent page-capture wins; otherwise
    # fetch over HTTP (Lane 2). A JS shell escalates browser_needed and
    # records the URL in the pending-capture queue for the host agent.
    conn = get_connection()
    capture = get_page_capture(conn, url)
    if capture and capture.get("intel"):
        intel = capture["intel"]
        log.info("[1] Using agent page-capture intelligence (json-ld: %d, api: %d)",
                 len(intel.get("json_ld", [])), len(intel.get("api_responses", [])))
    else:
        log.info("[1] Collecting page intelligence over HTTP...")
        try:
            intel = collect_page_intelligence(url)
        except httpx.HTTPError as e:
            log.error("FETCH_ERROR: %s", e)
            return {"name": name, "status": "FETCH_ERROR", "error": str(e)}
        log.info("Done | JSON-LD: %d | API: %d | testids: %d | cards: %d",
                 len(intel["json_ld"]), len(intel["api_responses"]),
                 len(intel["data_testids"]), len(intel["card_candidates"]))
        if _is_js_shell(intel):
            record_capture_needed(conn, url)
            hint = f"kestrel agent page-capture --url {url}"
            log.warning("JS shell over HTTP — browser_needed; capture the page "
                        "with '%s'", hint)
            return {
                "name": name,
                "status": "BROWSER_NEEDED",
                "error": f"browser-needed: JS-rendered page — capture it with "
                         f"'{hint}' and re-run",
            }

    # Step 1.5: Judge filters API responses
    if intel["api_responses"]:
        log.info("[1.5] Judge filtering API responses...")
        intel["api_responses"] = judge_api_responses(intel["api_responses"])
        log.info("Kept %d relevant responses", len(intel["api_responses"]))

    # Step 2: Strategy selection
    briefing = format_strategy_briefing(intel)
    log.info("[2] Phase 1: Strategy selection (%s chars briefing)", f"{len(briefing):,}")

    prompt = STRATEGY_PROMPT.format(briefing=briefing)
    try:
        raw, elapsed, meta = ask_llm(prompt)
    except Exception as e:
        log.error("LLM_ERROR: %s", e)
        return {"name": name, "status": "LLM_ERROR", "error": str(e)}

    log.info("LLM: %d chars, %.1fs", meta["response_chars"], elapsed)

    try:
        plan = extract_json(raw)
    except Exception as e:
        log.error("PARSE_ERROR: %s | raw: %s", e, raw[:500])
        return {"name": name, "status": "PARSE_ERROR", "error": str(e), "raw": raw}

    strategy = plan.get("strategy", "?")
    reasoning = plan.get("reasoning", "?")
    log.info("Strategy: %s | Reasoning: %s", strategy, reasoning)

    # Step 3: Execute
    log.info("[3] Executing %s...", strategy)
    try:
        if strategy == "json_ld":
            log.info("Extraction plan: %s", json.dumps(plan.get("extraction", {}))[:300])
            jobs = execute_json_ld(intel, plan)
        elif strategy == "api_response":
            log.info("Extraction plan: %s", json.dumps(plan.get("extraction", {}))[:300])
            jobs = execute_api_response(intel, plan)
        elif strategy == "css_selectors":
            log.info("-> Phase 2: Generating selectors from card examples...")
            selectors, jobs = execute_css_selectors(intel)
            plan["extraction"] = selectors
        else:
            log.warning("Unknown strategy: %s", strategy)
            jobs = []
    except Exception as e:
        log.error("EXECUTION_ERROR: %s", e)
        return {"name": name, "status": "EXEC_ERROR", "error": str(e), "plan": plan}

    # Step 4: Report
    titles = sum(1 for j in jobs if j.get("title"))
    total = len(jobs)
    status = "PASS" if total > 0 and titles / max(total, 1) >= 0.8 else "FAIL" if total == 0 else "PARTIAL"

    urls = sum(1 for j in jobs if j.get("url"))
    salaries = sum(1 for j in jobs if j.get("salary"))
    descs = sum(1 for j in jobs if j.get("description"))
    log.info("RESULT: %s -- %d jobs, %d titles, %d urls, %d salaries, %d descriptions",
             status, total, titles, urls, salaries, descs)

    for j in jobs[:3]:
        log.info("  - %s | loc: %s | salary: %s",
                 str(j.get("title") or "?")[:55],
                 str(j.get("location") or "?")[:25],
                 str(j.get("salary") or "-")[:20])

    return {
        "name": name,
        "status": status,
        "strategy": strategy,
        "total": total,
        "titles": titles,
        "plan": plan,
        "jobs": jobs,
        "sample": jobs[:5],
    }


# -- Target building --------------------------------------------------------

def build_scrape_targets(
    sites: list[dict] | None = None,
    search_cfg: dict | None = None,
) -> list[dict]:
    """Build the full list of (name, url) targets from sites + search config queries.

    - "search" sites get expanded: 1 URL per query from search config
    - "static" sites get scraped once as-is

    Placeholders in URLs:
      {query_encoded} -> URL-encoded search query
      {location_encoded} -> URL-encoded location
      {query} -> raw search query (for simple substitution)
    """
    if sites is None:
        sites = load_sites()
    if search_cfg is None:
        search_cfg = config.load_search_config()

    queries_cfg = search_cfg.get("queries", [])
    queries = [q["query"] for q in queries_cfg]
    locs = search_cfg.get("locations", [])
    default_location = locs[0]["location"] if locs else ""

    targets: list[dict] = []

    for site in sites:
        site_url = site.get("url", "")
        site_name = site.get("name", "Unknown")
        site_type = site.get("type", "static")

        if site_type == "search" and queries:
            for query in queries:
                expanded_url = site_url
                expanded_url = expanded_url.replace("{query_encoded}", quote_plus(query))
                expanded_url = expanded_url.replace("{query}", quote_plus(query))
                expanded_url = expanded_url.replace("{location_encoded}", quote_plus(default_location))
                targets.append({
                    "name": site_name,
                    "url": expanded_url,
                    "query": query,
                })
        else:
            expanded_url = site_url
            expanded_url = expanded_url.replace("{location_encoded}", quote_plus(default_location))
            targets.append({
                "name": site_name,
                "url": expanded_url,
                "query": None,
            })

    return targets


# -- Run all sites -----------------------------------------------------------

def _run_all(
    targets: list[dict],
    accept_locs: list[str],
    reject_locs: list[str],
    workers: int = 1,
) -> dict:
    """Run smart extract on all targets.

    Sequential by default. When workers > 1, scrapes multiple sites in parallel
    using ThreadPoolExecutor. DB storage is still serialized after each result.
    """
    conn = init_db()
    pre_stats = get_stats(conn)
    log.info("Database: %d jobs already stored, %d pending detail scrape",
             pre_stats["total"], pre_stats["pending_detail"])

    results: list[dict] = []
    total_new = 0
    total_existing = 0

    def _process_result(r: dict, target: dict) -> None:
        nonlocal total_new, total_existing
        jobs = r.get("jobs", [])
        if jobs:
            new, existing = _store_jobs_filtered(conn, jobs, target["name"],
                                                  r.get("strategy", "?"),
                                                  accept_locs, reject_locs)
            total_new += new
            total_existing += existing
            log.info("DB: +%d new, %d already existed", new, existing)

    if workers > 1 and len(targets) > 1:
        # Parallel mode
        with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
            future_to_target = {
                pool.submit(_run_one_site, target["name"], target["url"]): target
                for target in targets
            }
            for future in as_completed(future_to_target):
                target = future_to_target[future]
                r = future.result()
                results.append(r)
                _process_result(r, target)
    else:
        # Sequential mode (default)
        for i, target in enumerate(targets):
            label = target["name"]
            if target.get("query"):
                label = f"{target['name']} [{target['query']}]"
            log.info("[%d/%d] %s", i + 1, len(targets), label)

            r = _run_one_site(target["name"], target["url"])
            results.append(r)
            _process_result(r, target)

    # Summary
    for r in results:
        strategy = r.get("strategy", "?")
        if r["status"] in ("PASS", "PARTIAL", "FAIL"):
            detail = f"{r['total']} jobs, {r['titles']} titles, strategy={strategy}"
        else:
            detail = r.get("error", "")[:60]
        log.info("%-10s | %-25s | %s", r["status"], r["name"], detail)

    passed = sum(1 for r in results if r["status"] == "PASS")
    log.info("%d/%d PASS", passed, len(results))

    return {"total_new": total_new, "total_existing": total_existing,
            "passed": passed, "total": len(results)}


# -- Public entry point ------------------------------------------------------

def run_smart_extract(
    sites: list[dict] | None = None,
    workers: int = 1,
) -> dict:
    """Main entry point for AI-powered smart extraction.

    Loads sites from config/sites.yaml and search queries from the user's
    search config, then runs the extraction pipeline on all targets.

    Args:
        sites: Override the site list. If None, loads from YAML.
        workers: Number of parallel threads for site scraping. Default 1 (sequential).

    Returns:
        Dict with stats: total_new, total_existing, passed, total.
    """
    search_cfg = config.load_search_config()
    accept_locs, reject_locs = _load_location_filter(search_cfg)

    targets = build_scrape_targets(sites=sites, search_cfg=search_cfg)

    if not targets:
        log.warning("No scrape targets configured. Create config/sites.yaml and searches.yaml.")
        return {"total_new": 0, "total_existing": 0, "passed": 0, "total": 0}

    search_sites = sum(1 for s in (sites or load_sites()) if s.get("type") == "search")
    static_sites = sum(1 for s in (sites or load_sites()) if s.get("type") != "search")
    log.info("Sites: %d searchable, %d static | Total targets: %d (workers=%d)",
             search_sites, static_sites, len(targets), workers)

    return _run_all(targets, accept_locs, reject_locs, workers=workers)
