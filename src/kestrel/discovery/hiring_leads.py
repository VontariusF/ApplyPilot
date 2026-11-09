# SPDX-License-Identifier: AGPL-3.0-only
"""Hiring-lead discovery sources (hiring-leads spec art_u0YGUqpk §4).

Source adapters follow the Composio adapter shape (env-keyed client,
retryable-status set with exponential backoff clamped to
MAX_RATE_LIMIT_WAIT_SECONDS, injectable sleep for tests, B10 per-row
guards): the X-ray sources — SERP and Tavily, selectable via
leads.yaml ``provider`` (default tavily) — and an optional Firecrawl
public-page source (off by default).

Guardrails are structural, not conventional (spec locked decisions):

- Discovery reads search-result METADATA and public non-LinkedIn pages
  only. No code path here fetches a linkedin.com page: the SERP client
  talks to the SERP provider host, the Tavily client to the Tavily API,
  the Firecrawl client to the Firecrawl API, and
  ``_refuse_linkedin_target`` runs before any request is built. The
  person's browser is the only thing that ever touches LinkedIn.
- Nothing is ever sent: this module holds no outreach code, and no
  LinkedIn credential is ever read or stored.
- A bot-wall or CAPTCHA challenge retires a source terminally (recorded
  under APP_DIR, no un-retire path) — never retried through, matching
  the store's terminal-captcha policy.

Typed refusals (LeadsRefusal) surface through the agent verbs as _fail
envelopes: no_provider_key, provider_unsupported, quota_exhausted,
no_high_match_companies, source_retired, company_unresolved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from kestrel import config
from kestrel.company_identity import normalize_name_key
from kestrel.database import (
    LEAD_SOURCE_PUBLIC_PAGE,
    LEAD_SOURCE_SERP,
    LEAD_SOURCE_TAVILY,
    get_connection,
    high_match_companies,
    upsert_hiring_lead,
)
from kestrel.llm import MAX_RATE_LIMIT_WAIT_SECONDS

log = logging.getLogger(__name__)

# The default source set comes from config (leads.yaml ``provider``,
# default tavily) — read by the discovery entrypoint, which refuses an
# unknown name with the typed provider_unsupported refusal.

_SERP_BASE_URL = "https://serpapi.com"
_TAVILY_BASE_URL = "https://api.tavily.com"
_FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_TIMEOUT_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Typed refusals (spec §5 failure states) — the verbs surface these as
# _fail envelopes; none is ever a stack trace.
# ---------------------------------------------------------------------------

NO_PROVIDER_KEY = "no_provider_key"
PROVIDER_UNSUPPORTED = "provider_unsupported"
QUOTA_EXHAUSTED = "quota_exhausted"
NO_HIGH_MATCH_COMPANIES = "no_high_match_companies"
SOURCE_RETIRED = "source_retired"
COMPANY_UNRESOLVED = "company_unresolved"

ALL_REFUSALS = (
    NO_PROVIDER_KEY,
    PROVIDER_UNSUPPORTED,
    QUOTA_EXHAUSTED,
    NO_HIGH_MATCH_COMPANIES,
    SOURCE_RETIRED,
    COMPANY_UNRESOLVED,
)


class LeadsRefusal(Exception):
    """A typed, expected refusal with a stable machine-readable code.

    The agent verbs translate one of these into a _fail envelope with
    ``code`` and ``details``. Messages name the exact fix (which env key
    to set, which stage to run first) and never carry credentials.
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class SourceRetired(Exception):
    """A bot-wall/CAPTCHA block on one source — terminal, never retried.

    Raised by a client when its provider serves a block; the discovery
    entrypoint records the retirement durably (``retire_source``) and
    stops using that source for the rest of the session — and every
    later session, since there is no un-retire path.
    """


class HiringLeadsError(Exception):
    """A source-level failure that is not a typed refusal.

    Messages carry only provider-provided diagnostics and configuration
    hints — never an API key or any other credential value. The SERP
    request URL in particular embeds the key per the SerpAPI wire
    format, so it is never included in any message.
    """


# ---------------------------------------------------------------------------
# LinkedIn boundaries — the structural half of the no-scraping guardrail
# ---------------------------------------------------------------------------

_LINKEDIN_PROFILE_RE = re.compile(r"^/in/(?P<slug>[A-Za-z0-9_%-]{2,})/?$",
                                  re.IGNORECASE)

# A LinkedIn /in/ profile URL is the only shape allowed to become a
# lead's stable person key; every other result shape is metadata noise.
_LINKEDIN_HOST_SUFFIX = "linkedin.com"


def _host_of(url: str | None) -> str | None:
    try:
        host = urlparse(url).hostname if url else None
    except ValueError:
        return None
    return host.lower() if host else None


def is_linkedin_host(url: str | None) -> bool:
    """True for linkedin.com and any subdomain (www, country sites)."""
    host = _host_of(url)
    return bool(host) and (host == _LINKEDIN_HOST_SUFFIX
                           or host.endswith("." + _LINKEDIN_HOST_SUFFIX))


def is_linkedin_profile_url(url: str | None) -> bool:
    """True only for a linkedin.com/in/<slug> URL."""
    if not is_linkedin_host(url):
        return False
    return bool(_LINKEDIN_PROFILE_RE.match(urlparse(url).path))


def _refuse_linkedin_target(url: str, what: str) -> None:
    """Guardrail: no adapter ever builds a request at linkedin.com.

    Runs before the request exists, so the ban cannot be bypassed by a
    caller passing an odd URL. Host-only in the message — profile URLs
    are personal data and never belong in logs.
    """
    if is_linkedin_host(url):
        raise ValueError(
            f"{what} would target {_host_of(url)}, which Kestrel never "
            "fetches (spec locked decision: the person's browser is the "
            "only thing that touches LinkedIn)")


# ---------------------------------------------------------------------------
# Pure mapping (B10): one malformed result never aborts the batch
# ---------------------------------------------------------------------------

# Trailing id-like chunks on /in/ slugs ("jane-doe-8b21435").
_SLUG_ID_TAIL_RE = re.compile(r"-[0-9a-f]{4,}$|-\d+$", re.IGNORECASE)
_LINKEDIN_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*LinkedIn\s*$",
                                       re.IGNORECASE)


def display_name_from_slug(profile_url: str) -> str | None:
    """Display approximation from the /in/ slug — not a verified name.

    "jane-doe-8b21435" -> "Jane Doe": the trailing id-like chunk is
    dropped and segments are title-cased. The profile URL stays the
    authoritative identity; a slug that leaves nothing readable (single
    token, empty) maps to None and the result is skipped (B10) rather
    than guessed.
    """
    match = _LINKEDIN_PROFILE_RE.match(urlparse(profile_url).path)
    if not match:
        return None
    slug = _SLUG_ID_TAIL_RE.sub("", match.group("slug"))
    parts = [p for p in re.split(r"[-_]+", slug) if p]
    if not 2 <= len(parts) <= 4:
        return None
    return " ".join(p.capitalize() for p in parts)


def clean_profile_title(raw_title: object) -> str | None:
    """Clean a SERP result title into a usable headline, or None.

    Strips the trailing "| LinkedIn" / "- LinkedIn" site suffix and
    collapses whitespace — the metadata the search provider already
    served, never enriched from the profile itself.
    """
    title = str(raw_title or "").replace("\u00a0", " ")
    title = _LINKEDIN_TITLE_SUFFIX_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or None


def map_search_result(*, link: str, title: object,
                      company_key: str,
                      source: str = LEAD_SOURCE_SERP) -> dict:
    """Map one search-provider result to a hiring_leads row (pure).

    Shared by the SERP and Tavily X-ray sources: both return result
    METADATA (title, link) — never page fetches — so one mapping holds
    the LinkedIn-only and tracker-stripping rules. Raises ValueError
    when the result is not a usable LinkedIn-profile metadata hit —
    callers skip the row and keep the batch alive (B10). Non-profile
    links (company pages, posts) are filter, not error: they are simply
    not people. ``source`` records the provider that served the
    metadata (serp | tavily) — provenance, so a Tavily row reads as
    Tavily-sourced (dogfood finding 2026-11-09).
    """
    link = str(link or "").strip()
    if not is_linkedin_profile_url(link):
        raise ValueError("result is not a linkedin.com/in profile link")
    parsed = urlparse(link)
    # Strip query/fragment (trackers) so the person key dedupes stably.
    profile_url = f"https://{parsed.hostname.lower()}{parsed.path.rstrip('/')}"
    cleaned = clean_profile_title(title)
    full_name = display_name_from_slug(profile_url)
    if not cleaned or not full_name:
        raise ValueError("profile metadata lacks a usable title or name slug")
    return {
        "profile_url": profile_url,
        "full_name": full_name,
        "title": cleaned,
        "company_key": company_key,
        # The search surface (X-ray metadata) is the same for both
        # providers; the source column records WHICH provider served it.
        "source": source,
        # The search result is its own discovery surface; there is no
        # separate evidence page (and fetching one is out of bounds).
        "evidence_url": None,
    }


def map_serp_result(raw: dict, company_key: str) -> dict:
    """Map one SERP organic result to a hiring_leads row (pure)."""
    return map_search_result(link=raw.get("link"), title=raw.get("title"),
                             company_key=company_key,
                             source=LEAD_SOURCE_SERP)


def map_tavily_result(raw: dict, company_key: str) -> dict:
    """Map one Tavily search result to a hiring_leads row (pure)."""
    return map_search_result(link=raw.get("url"), title=raw.get("title"),
                             company_key=company_key,
                             source=LEAD_SOURCE_TAVILY)


def title_affiliation_role(*, title: object, display_name: str,
                           domain: str | None = None,
                           taxonomy: tuple[str, ...] | list[str] = (),
                           ) -> str | None:
    """The hiring role a result TITLE pairs with the company, if any.

    Guarantee-grade attribution (dogfood round two, 2026-11-09): the
    title alone must establish BOTH facts — the company display name
    or its domain appears as a whole-word match, AND a role-taxonomy
    term appears. Snippet text is deliberately not read: a profile
    that merely mentions the company in body text (a project or tool
    list) is an assertion of affiliation, not evidence of it — the
    live store attributed a student to supabase.com on exactly that.
    Returns the matched taxonomy term (as configured, not lowercased),
    or None. Pure — the title is metadata the adapter already holds.
    """
    text = str(title or "").lower()
    if not text:
        return None
    needles = [n for n in (str(display_name or "").lower(),
                           str(domain or "").lower()) if n]
    if not any(re.search(rf"\b{re.escape(n)}\b", text) for n in needles):
        return None
    for term in taxonomy:
        if re.search(rf"\b{re.escape(str(term).lower())}\b", text):
            return str(term)
    return None


def affiliated_xray_rows(raw_results: list[dict], company: dict, *,
                         mapper: Callable[[dict, str], dict],
                         taxonomy: tuple[str, ...] | list[str] = (),
                         ) -> tuple[list[dict], int]:
    """Map raw X-ray results to lead rows, keeping affiliated ones (pure).

    Two independent gates per result, both B10 — a bad result is
    dropped, never fatal to the batch:

    1. The LinkedIn host+path gate inside ``mapper`` (not a person, or
       not a profile link — metadata noise).
    2. The attribution gate: the result TITLE must pair the company
       (name or domain) with a hiring role from ``taxonomy``
       (``title_affiliation_role``); anything weaker — a snippet
       mention, a company name without a hiring role, a role without
       the company — is an assertion, not a guarantee, and is dropped
       and counted, never stored.

    Returns the mapped rows and the count dropped for no affiliation —
    the run payload's ``dropped_no_affiliation`` counter.
    """
    rows: list[dict] = []
    dropped = 0
    for raw in raw_results:
        try:
            row = mapper(raw, company["company_key"])
        except ValueError as exc:
            # B10: one unusable result never aborts the batch. The
            # message is generic — result titles carry personal data.
            log.debug("Skipping unusable X-ray result: %s", exc)
            continue
        role = title_affiliation_role(
            title=raw.get("title"),
            display_name=company.get("display_name", ""),
            domain=company.get("domain"),
            taxonomy=taxonomy)
        if role is None:
            dropped += 1
            log.debug("Dropping result: title pairs no company and hiring role")
            continue
        log.debug("Result attributed via title pairing with role %r", role)
        rows.append(row)
    return rows, dropped


def xray_query(display_name: str,
               taxonomy: tuple[str, ...] | list[str]) -> str:
    """One SERP X-ray query per company: role taxonomy OR-ed (spec §4).

    site:-scoped so hits are LinkedIn profile metadata; the roles ride
    as quoted OR terms so one query covers the whole taxonomy instead
    of multiplying provider calls against the daily cap.
    """
    roles = " OR ".join(f'"{term}"' for term in taxonomy)
    return f'site:linkedin.com/in "{display_name}" ({roles})'


def xray_query_plain(display_name: str,
                     taxonomy: tuple[str, ...] | list[str]) -> str:
    """The Tavily X-ray query: the same taxonomy ORs, PLAIN text.

    No ``site:`` prefix — Tavily does not guarantee search-operator
    support, so the LinkedIn restriction rides in the request body as
    ``include_domains=["linkedin.com"]`` (restrict mode) instead. The
    adapter's host+path /in/ validation stays the final gate on every
    result URL regardless of what the provider returned.
    """
    roles = " OR ".join(f'"{term}"' for term in taxonomy)
    return f'"{display_name}" ({roles})'


# ---------------------------------------------------------------------------
# Clients (Composio adapter shape: retryable set, clamped backoff,
# injectable sleep, credential-free errors)
# ---------------------------------------------------------------------------


class SerpClient:
    """Thin HTTP client for a SerpAPI-compatible search provider.

    The wire format is the SerpAPI one (GET /search?q=...&api_key=...,
    JSON with ``organic_results``). Any compatible endpoint swaps in via
    ``base_url`` — the provider seam the spec requires. Reads ONLY
    organic-result metadata.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _SERP_BASE_URL,
        timeout: float = _TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("SERP API key is required (SERP_API_KEY)")
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "SerpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def organic_results(self, query: str, *, num: int) -> list[dict]:
        """Organic-result metadata for one query — never page fetches."""
        body = self._request("GET", "/search", {
            "engine": "google",
            "q": query,
            "num": str(num),
            "api_key": self._api_key,
        })
        results = body.get("organic_results")
        if not isinstance(results, list):
            return []
        return [r for r in results if isinstance(r, dict)]

    # -- Transport ---------------------------------------------------------

    def _request(self, method: str, path: str, params: dict) -> dict:
        """One request with bounded retry on 429/5xx/transport errors."""
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(method, path, params=params)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    # Type only: transport text may embed request state,
                    # and errors must stay credential-free (S3).
                    raise HiringLeadsError(
                        f"SERP request failed: {type(exc).__name__}") from exc
                self._sleep(self._backoff_seconds(attempt))
                continue
            if response.status_code == 403:
                raise SourceRetired(
                    "SERP provider served a bot-wall/CAPTCHA block (HTTP 403)")
            if response.status_code == 401:
                raise LeadsRefusal(
                    NO_PROVIDER_KEY,
                    "SERP provider rejected SERP_API_KEY — set a valid key",
                    {"required_env": ["SERP_API_KEY"], "provider": "serp"})
            if (response.status_code in _RETRYABLE_STATUSES
                    and attempt < self._max_retries):
                self._sleep(self._retry_wait_seconds(response, attempt))
                continue
            return self._parse_response(response)
        raise HiringLeadsError("SERP request retry loop exited unexpectedly")

    def _parse_response(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError as exc:
            raise HiringLeadsError(
                f"SERP returned non-JSON response (HTTP {response.status_code})"
            ) from exc
        if not isinstance(body, dict):
            raise HiringLeadsError(
                f"SERP returned an unexpected response shape "
                f"(HTTP {response.status_code})")
        if not response.is_success:
            raise HiringLeadsError(f"SERP returned HTTP {response.status_code}")
        blob = json.dumps(body).lower()
        if body.get("captcha") or "unusual traffic" in blob:
            raise SourceRetired("SERP response contains a CAPTCHA challenge")
        error = body.get("error")
        if isinstance(error, str) and error.strip():
            raise HiringLeadsError(f"SERP error: {error}")
        return body

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** attempt),
                   MAX_RATE_LIMIT_WAIT_SECONDS)

    def _retry_wait_seconds(self, response: httpx.Response,
                            attempt: int) -> float:
        """Rate-limit wait: Retry-After hint, clamped to the shared ceiling
        so a bogus epoch header cannot sleep for years."""
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(max(0.0, float(value)), MAX_RATE_LIMIT_WAIT_SECONDS)
            except ValueError:
                log.debug("SERP Retry-After header not numeric: %r", value)
        return self._backoff_seconds(attempt)


class TavilyClient:
    """Thin HTTP client for the Tavily search API — result metadata only.

    Wire format (docs.tavily.com search endpoint): POST /search with a
    JSON body of ``{query, include_domains, max_results, api_key}``;
    the response's ``results[]`` carry ``title`` / ``url`` / ``content``.
    Only the metadata is read — the profile pages themselves are never
    fetched (spec locked decision).
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _TAVILY_BASE_URL,
        timeout: float = _TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("Tavily API key is required (TAVILY_API_KEY)")
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "TavilyClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def search_results(self, query: str, *,
                       include_domains: list[str],
                       max_results: int) -> list[dict]:
        """Search-result metadata for one query — never page fetches."""
        body = self._request("POST", "/search", {
            "query": query,
            "include_domains": list(include_domains),
            "max_results": max_results,
            "api_key": self._api_key,
        })
        results = body.get("results")
        if not isinstance(results, list):
            return []
        return [r for r in results if isinstance(r, dict)]

    # -- Transport (same shape as SerpClient) -------------------------------

    def _request(self, method: str, path: str, payload: dict) -> dict:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(method, path, json=payload)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise HiringLeadsError(
                        f"Tavily request failed: {type(exc).__name__}") from exc
                self._sleep(self._backoff_seconds(attempt))
                continue
            if response.status_code == 403:
                raise SourceRetired("Tavily served a block (HTTP 403)")
            if response.status_code == 401:
                raise LeadsRefusal(
                    NO_PROVIDER_KEY,
                    "Tavily rejected TAVILY_API_KEY — set a valid key",
                    {"required_env": ["TAVILY_API_KEY"],
                     "provider": "tavily"})
            if (response.status_code in _RETRYABLE_STATUSES
                    and attempt < self._max_retries):
                self._sleep(self._retry_wait_seconds(response, attempt))
                continue
            return self._parse_response(response)
        raise HiringLeadsError("Tavily request retry loop exited unexpectedly")

    def _parse_response(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError as exc:
            raise HiringLeadsError(
                f"Tavily returned non-JSON response "
                f"(HTTP {response.status_code})") from exc
        if not isinstance(body, dict):
            raise HiringLeadsError(
                f"Tavily returned an unexpected response shape "
                f"(HTTP {response.status_code})")
        if not response.is_success:
            raise HiringLeadsError(
                f"Tavily returned HTTP {response.status_code}")
        error = body.get("error")
        if isinstance(error, str) and error.strip():
            raise HiringLeadsError(f"Tavily error: {error}")
        return body

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** attempt),
                   MAX_RATE_LIMIT_WAIT_SECONDS)

    def _retry_wait_seconds(self, response: httpx.Response,
                            attempt: int) -> float:
        """Rate-limit wait: Retry-After hint, clamped to the shared ceiling
        so a bogus epoch header cannot sleep for years."""
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(max(0.0, float(value)), MAX_RATE_LIMIT_WAIT_SECONDS)
            except ValueError:
                log.debug("Tavily Retry-After header not numeric: %r", value)
        return self._backoff_seconds(attempt)


class FirecrawlClient:
    """Thin HTTP client for the Firecrawl v1 scrape API — public pages only.

    Every scrape passes ``_refuse_linkedin_target`` before a request is
    built, so the client cannot be pointed at LinkedIn even by a caller
    bug.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _FIRECRAWL_BASE_URL,
        timeout: float = _TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("Firecrawl API key is required (FIRECRAWL_API_KEY)")
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "FirecrawlClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def scrape_markdown(self, url: str) -> str:
        """Scrape one public page; returns its markdown body (may be empty)."""
        _refuse_linkedin_target(url, "Firecrawl scrape")
        body = self._request("POST", "/v1/scrape", {"url": url,
                                                    "formats": ["markdown"]})
        data = body.get("data")
        markdown = data.get("markdown") if isinstance(data, dict) else None
        return markdown if isinstance(markdown, str) else ""

    # -- Transport (same shape as SerpClient) -------------------------------

    def _request(self, method: str, path: str, payload: dict) -> dict:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(method, path, json=payload)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise HiringLeadsError(
                        f"Firecrawl request failed: {type(exc).__name__}"
                    ) from exc
                self._sleep(self._backoff_seconds(attempt))
                continue
            if response.status_code == 403:
                raise SourceRetired(
                    "Firecrawl served a block (HTTP 403)")
            if response.status_code == 401:
                raise LeadsRefusal(
                    NO_PROVIDER_KEY,
                    "Firecrawl rejected FIRECRAWL_API_KEY — set a valid key",
                    {"required_env": ["FIRECRAWL_API_KEY"],
                     "provider": "firecrawl"})
            if (response.status_code in _RETRYABLE_STATUSES
                    and attempt < self._max_retries):
                self._sleep(self._backoff_seconds(attempt))
                continue
            return self._parse_response(response)
        raise HiringLeadsError("Firecrawl request retry loop exited unexpectedly")

    def _parse_response(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError as exc:
            raise HiringLeadsError(
                f"Firecrawl returned non-JSON response "
                f"(HTTP {response.status_code})") from exc
        if not isinstance(body, dict):
            raise HiringLeadsError(
                f"Firecrawl returned an unexpected response shape "
                f"(HTTP {response.status_code})")
        if not response.is_success:
            raise HiringLeadsError(
                f"Firecrawl returned HTTP {response.status_code}")
        return body

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** attempt),
                   MAX_RATE_LIMIT_WAIT_SECONDS)


# ---------------------------------------------------------------------------
# Sources (the provider seam: pluggable by name)
# ---------------------------------------------------------------------------


class SerpXRaySource:
    """X-ray source: search-result metadata for people at one company.

    One query per company (spec §4 shape), reading only organic-result
    metadata — titles, links, snippets. The provider is any
    SerpAPI-compatible endpoint behind SerpClient.
    """

    name = "serp"
    source_label = LEAD_SOURCE_SERP

    def __init__(self,
                 client: SerpClient | None = None,
                 *,
                 results_per_query: int | None = None,
                 taxonomy: tuple[str, ...] | list[str] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if client is None:
            key = os.getenv("SERP_API_KEY", "").strip()
            if not key:
                # Before any network call: the refusal names the fix.
                raise LeadsRefusal(
                    NO_PROVIDER_KEY,
                    "SERP_API_KEY is not set — export it to enable "
                    "hiring-lead discovery",
                    {"required_env": ["SERP_API_KEY"]})
            client = SerpClient(key, sleep=sleep)
        self._client = client
        self._results_per_query = (results_per_query
                                   or config.leads_serp_results_per_query())
        self._taxonomy = (tuple(taxonomy) if taxonomy is not None
                          else config.leads_role_taxonomy())
        self.dropped_no_affiliation = 0

    def discover_for_company(self, company: dict) -> list[dict]:
        query = xray_query(company["display_name"], self._taxonomy)
        raw_results = self._client.organic_results(
            query, num=self._results_per_query)
        leads, dropped = affiliated_xray_rows(
            raw_results, company, mapper=map_serp_result,
            taxonomy=self._taxonomy)
        self.dropped_no_affiliation += dropped
        return leads


class TavilyXRaySource:
    """X-ray source over the Tavily search API — result metadata only.

    Builds the same role-taxonomy X-ray queries as the SERP source but
    as PLAIN query text (no ``site:`` prefix — Tavily does not
    guarantee operator support); the LinkedIn restriction rides in the
    request body as ``include_domains=["linkedin.com"]``. The
    host+path /in/ validation inside ``map_tavily_result`` remains the
    final gate: a result that fails it is dropped, not trusted.
    """

    name = "tavily"
    # The provider that served the metadata IS the provenance: Tavily
    # rows read as Tavily-sourced (dogfood finding 2026-11-09).
    source_label = LEAD_SOURCE_TAVILY

    def __init__(self,
                 client: TavilyClient | None = None,
                 *,
                 max_results: int | None = None,
                 taxonomy: tuple[str, ...] | list[str] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if client is None:
            key = os.getenv("TAVILY_API_KEY", "").strip()
            if not key:
                # Before any network call: the refusal names the provider
                # it wanted and the exact fix.
                raise LeadsRefusal(
                    NO_PROVIDER_KEY,
                    "TAVILY_API_KEY is not set — export it to enable "
                    "hiring-lead discovery via the Tavily provider",
                    {"required_env": ["TAVILY_API_KEY"],
                     "provider": "tavily"})
            client = TavilyClient(key, sleep=sleep)
        self._client = client
        self._max_results = (max_results
                             if max_results is not None
                             else config.leads_tavily_max_results())
        self._taxonomy = (tuple(taxonomy) if taxonomy is not None
                          else config.leads_role_taxonomy())
        self.dropped_no_affiliation = 0

    def discover_for_company(self, company: dict) -> list[dict]:
        query = xray_query_plain(company["display_name"], self._taxonomy)
        raw_results = self._client.search_results(
            query, include_domains=[_LINKEDIN_HOST_SUFFIX],
            max_results=self._max_results)
        leads, dropped = affiliated_xray_rows(
            raw_results, company, mapper=map_tavily_result,
            taxonomy=self._taxonomy)
        self.dropped_no_affiliation += dropped
        return leads


class FirecrawlPageSource:
    """Public careers/team pages via Firecrawl — off by default (spec §4).

    Scrapes the company's own careers page (the domain the identity
    layer resolved) and extracts person-at-title mentions whose line
    names a role-taxonomy term. Page leads have no profile URL, so the
    stable person key is the page URL plus the normalized name — an
    honest dedupe key, not a fabricated LinkedIn link.
    """

    name = "firecrawl"
    source_label = LEAD_SOURCE_PUBLIC_PAGE

    def __init__(self,
                 client: FirecrawlClient | None = None,
                 *,
                 taxonomy: tuple[str, ...] | list[str] | None = None,
                 enabled: bool | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if enabled is None:
            enabled = config.firecrawl_enabled()
        if not enabled:
            raise LeadsRefusal(
                NO_PROVIDER_KEY,
                "Firecrawl is disabled — set firecrawl.enabled: true in "
                "leads.yaml and export FIRECRAWL_API_KEY to opt in",
                {"required_env": ["FIRECRAWL_API_KEY"],
                 "config": "leads.yaml firecrawl.enabled"})
        if client is None:
            key = os.getenv("FIRECRAWL_API_KEY", "").strip()
            if not key:
                raise LeadsRefusal(
                    NO_PROVIDER_KEY,
                    "FIRECRAWL_API_KEY is not set — export it to enable "
                    "public-page discovery",
                    {"required_env": ["FIRECRAWL_API_KEY"],
                     "provider": "firecrawl"})
            client = FirecrawlClient(key, sleep=sleep)
        self._client = client
        self._taxonomy = (tuple(taxonomy) if taxonomy is not None
                          else config.leads_role_taxonomy())

    def discover_for_company(self, company: dict) -> list[dict]:
        domain = company.get("domain")
        if not domain:
            # Name-confidence identity: no employer domain is known, so
            # there is no careers page to read. Skipped visibly (the
            # payload counts companies_without_domain), not guessed at.
            return []
        page_url = f"https://{domain}/careers"
        _refuse_linkedin_target(page_url, "Firecrawl careers page")
        text = self._client.scrape_markdown(page_url)
        return people_from_page_text(text, self._taxonomy,
                                     company_key=company["company_key"],
                                     page_url=page_url)


SOURCE_BUILDERS: dict[str, type] = {
    "serp": SerpXRaySource,
    "tavily": TavilyXRaySource,
    "firecrawl": FirecrawlPageSource,
}


def build_source(name: str, *, client=None,
                 sleep: Callable[[float], None] = time.sleep):
    """The provider seam: sources are pluggable by name (spec §4)."""
    try:
        builder = SOURCE_BUILDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown lead source {name!r}; known: {sorted(SOURCE_BUILDERS)}"
        ) from None
    return builder(client, sleep=sleep)


# ---------------------------------------------------------------------------
# Public-page parser (pure)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Z][A-Za-z'’\-]+(?: [A-Z][A-Za-z'’\-]+){1,3}$")
_LINE_NOISE_RE = re.compile(r"@|https?://|www\.|\btel\b|\bphone\b",
                            re.IGNORECASE)


def _clean_name_fragment(fragment: str) -> str | None:
    candidate = fragment.strip(" \t•·,;:–—-|").strip()
    return candidate or None


def people_from_page_text(text: str,
                          taxonomy: tuple[str, ...] | list[str],
                          *,
                          company_key: str,
                          page_url: str) -> list[dict]:
    """Person-at-title mentions on one public page (pure; B10 skips).

    Recognizes "Name, Title" and "Title: Name" line shapes where Title
    is a role-taxonomy hit. Deliberately shallow: a page with no
    parseable people yields zero leads — it never invents names.
    """
    lowered_terms = [(t, t.lower()) for t in taxonomy]
    leads: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line or _LINE_NOISE_RE.search(line):
            continue
        lowered = line.lower()
        hit = next((t for t, low in lowered_terms if low in lowered), None)
        if not hit:
            continue
        idx = lowered.find(hit.lower())
        before, after = line[:idx], line[idx + len(hit):]
        candidate = (_clean_name_fragment(before)
                     or _clean_name_fragment(after))
        if not candidate or not _NAME_RE.match(candidate):
            continue
        leads.append({
            "profile_url": f"{page_url}#{normalize_name_key(candidate)}",
            "full_name": candidate,
            "title": hit,
            "company_key": company_key,
            "source": LEAD_SOURCE_PUBLIC_PAGE,
            "evidence_url": page_url,
        })
    return leads


# ---------------------------------------------------------------------------
# Durable run state: daily usage and terminal source retirement
# ---------------------------------------------------------------------------


def _usage_path():
    return config.APP_DIR / "discovery-usage.json"


def _retirement_path():
    return config.APP_DIR / "discovery-retired.json"


def discovery_runs_used(now: datetime | None = None) -> int:
    """Discovery runs recorded for the current UTC day."""
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    path = _usage_path()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Surfaced, not swallowed; the counter self-heals. The cap is a
        # pacing guard against provider spend, not a security boundary.
        log.warning("Unreadable discovery-usage counter, treating as 0: %s",
                    exc)
        return 0
    if isinstance(data, dict) and data.get("utc_date") == today:
        try:
            return int(data.get("runs", 0))
        except (TypeError, ValueError):
            log.warning("Corrupt discovery-usage counter, treating as 0")
    return 0


def _record_discovery_run(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    used = discovery_runs_used(now) + 1
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    _usage_path().write_text(
        json.dumps({"utc_date": now.date().isoformat(), "runs": used}),
        encoding="utf-8")
    return used


def retired_sources() -> dict[str, dict]:
    """Sources terminally retired by a bot-wall/CAPTCHA (spec §5)."""
    path = _retirement_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Unreadable source-retirement record, ignoring: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def retire_source(name: str, reason: str,
                  now: datetime | None = None) -> None:
    """Record a terminal source retirement.

    A bot-wall is a stop, not a backoff: the record is durable and no
    later run retries through the wall (spec §4, store CAPTCHA policy).
    """
    records = retired_sources()
    records[name] = {
        "reason": reason,
        "retired_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    _retirement_path().write_text(json.dumps(records, indent=2),
                                  encoding="utf-8")


# ---------------------------------------------------------------------------
# Discovery entrypoint
# ---------------------------------------------------------------------------


def discover_hiring_leads(conn: sqlite3.Connection | None = None,
                          *,
                          company_key: str | None = None,
                          min_fit: float | None = None,
                          limit: int | None = None,
                          sources: tuple[str, ...] | None = None,
                          now: datetime | None = None) -> dict:
    """Run lead discovery against high-match companies (spec §4).

    One run = one provider call per (active source, company scanned).
    The daily cap counts runs that reached the provider-scan stage and
    resets each UTC day; it is checked before any network call.
    Companies come from database.high_match_companies — the merged §2
    contract. ``limit`` caps the companies scanned this run (provider
    spend), highest fit first.

    Sources default to the configured provider (leads.yaml
    ``provider``, default tavily — the X-ray source); Firecrawl is
    opt-in by explicit name plus its config flag. An unknown configured
    provider is the typed provider_unsupported refusal. Unavailable
    requested sources are skipped with a warning when at least one
    other source can run; the typed refusals raise LeadsRefusal
    otherwise. Returns the payload dict the agent verb prints.
    """
    if conn is None:
        conn = get_connection()
    now = now or datetime.now(timezone.utc)
    if sources:
        requested = tuple(sources)
    else:
        provider = config.leads_provider()
        if provider not in SOURCE_BUILDERS:
            raise LeadsRefusal(
                PROVIDER_UNSUPPORTED,
                f"leads.yaml provider {provider!r} is not supported — "
                f"known providers: {', '.join(sorted(SOURCE_BUILDERS))}",
                {"provider": provider,
                 "known_providers": sorted(SOURCE_BUILDERS)})
        requested = (provider,)

    # Everything checkable without the network is checked first: cap,
    # provider keys/config gates, retirements, company identity.
    cap = config.leads_daily_discovery_cap()
    used = discovery_runs_used(now)
    if used >= cap:
        raise LeadsRefusal(
            QUOTA_EXHAUSTED,
            f"daily discovery cap reached ({used}/{cap}); resets at UTC "
            "midnight",
            {"daily_discovery_cap": cap, "discovery_used_today": used})

    instances: list = []
    unavailable: list[LeadsRefusal] = []
    for name in requested:
        try:
            instances.append(build_source(name))
        except LeadsRefusal as exc:
            unavailable.append(exc)
    if not instances:
        if len(unavailable) == 1:
            raise unavailable[0]
        env_keys = sorted({k for exc in unavailable
                           for k in exc.details.get("required_env", [])})
        raise LeadsRefusal(
            NO_PROVIDER_KEY,
            "no requested lead source is available — set: "
            + ", ".join(env_keys),
            {"required_env": env_keys})
    for exc in unavailable:
        log.warning("Skipping unavailable lead source: %s", exc)

    retired = retired_sources()
    active = [s for s in instances if s.name not in retired]
    if instances and not active:
        raise LeadsRefusal(
            SOURCE_RETIRED,
            "all requested lead sources are retired for the session — "
            "a bot-wall was hit and Kestrel does not retry through walls",
            {"retired": {name: rec.get("reason")
                         for name, rec in retired.items()}})

    if company_key:
        row = conn.execute(
            "SELECT company_key, display_name, domain, fit_score "
            "FROM company_identity WHERE company_key = ?",
            (company_key,)).fetchone()
        if row is None:
            raise LeadsRefusal(
                COMPANY_UNRESOLVED,
                f"no company identity for {company_key!r} — run scoring "
                "first so the identity layer can resolve it",
                {"company_key": company_key})
        companies = [dict(row)]
    else:
        companies = high_match_companies(conn, min_fit)
        if not companies:
            effective_min_fit = (config.leads_min_company_fit()
                                 if min_fit is None else min_fit)
            orphaned = conn.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE fit_score IS NOT NULL AND company IS NULL"
            ).fetchone()[0]
            raise LeadsRefusal(
                NO_HIGH_MATCH_COMPANIES,
                "no company reaches the high-match threshold — score more "
                "jobs first",
                {"min_fit": effective_min_fit,
                 "scored_jobs_without_company": orphaned})
    if limit is not None:
        companies = companies[:max(0, limit)]

    # Scan. A mid-run bot-wall retires that source terminally and the
    # run continues honestly with the remaining sources.
    discovered = skipped_existing = 0
    used_sources: list[str] = []
    retired_now: dict[str, str] = {}
    for source in active:
        used_sources.append(source.name)
        for company in companies:
            try:
                leads = source.discover_for_company(company)
            except SourceRetired as exc:
                retire_source(source.name, str(exc), now=now)
                retired_now[source.name] = str(exc)
                log.warning("Source %s retired: %s", source.name, exc)
                break
            for lead in leads:
                if upsert_hiring_lead(conn, **lead):
                    discovered += 1
                else:
                    skipped_existing += 1

    used_after = _record_discovery_run(now)
    without_domain = sum(1 for c in companies if not c.get("domain"))
    return {
        "sources_requested": list(requested),
        "sources_used": used_sources,
        "retired_sources": retired_now,
        "companies_scanned": len(companies),
        "companies_without_domain": without_domain,
        "discovered": discovered,
        "skipped_existing": skipped_existing,
        # Results dropped because the metadata names no company
        # affiliation (dogfood finding 2026-11-09) — visible, not silent.
        "dropped_no_affiliation": sum(
            int(getattr(s, "dropped_no_affiliation", 0)) for s in active),
        "scored_jobs_without_company": conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL AND company IS NULL").fetchone()[0],
        "guardrails": {
            # The payload states what Kestrel deliberately does not do;
            # a test holds the code to the statement (spec criterion 7).
            "sends_nothing": True,
            "credentials_stored": "none",
            "profile_pages_fetched": False,
            "daily_discovery_cap": cap,
            "discovery_used_today": used_after,
        },
    }
