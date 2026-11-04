"""Composio discovery source: job listings via the Composio v3.1 tool API.

First source: the ``web3_career`` toolkit (web3.career), whose single tool
``WEB3_CAREER_LIST_JOBS`` returns the newest listings. Behavior below follows
the live-verified spike (``spike-composio``), not Composio's docs — the two
disagree in several places that shape this code:

- Argument-validation failures arrive as **HTTP 200** with
  ``successful: false`` (the upstream 400 is embedded in ``data.message``).
  Clients must branch on ``successful``, never on the HTTP status alone.
- ``salary_*`` and ``estimated_*`` fields are mutually exclusive groups and
  fully nullable in practice.
- ``include_description: false`` nulls ``id``/``company``/``tags`` and more,
  so listings are deduped on ``apply_url`` (the store's primary key), never
  on the numeric provider id.
- A bogus toolkit slug is silently accepted at session creation, so the
  expected tool's registration in ``tool_router_tools`` is verified before
  executing.

Connection discipline: an ACTIVE toolkit connection must already exist (the
integration spike provisioned web3.career under user ``applypilot-spike``).
This module never creates or recreates connections; a missing one surfaces
as a degraded-source error naming the fix (Composio error 1810).

The API key comes from ``COMPOSIO_API_KEY`` in the environment only — it is
never logged, prompted, or embedded in errors (S1/S2 discipline).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import httpx

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.filters import location_filters, location_ok as _location_ok
from applypilot.discovery.workday import strip_html
from applypilot.llm import MAX_RATE_LIMIT_WAIT_SECONDS, mask_key

log = logging.getLogger(__name__)

BASE_URL = "https://backend.composio.dev"
_SESSION_PATH = "/api/v3.1/tool_router/session"
_EXECUTE_PATH = "/api/v3.1/tools/execute/"

# The only toolkit with a spike-verified job-search mapping. Other slugs in
# COMPOSIO_TOOLKITS are skipped with a warning rather than executed against
# assumed schemas (spike §6.1: "any adapter must not assume other slugs").
TOOL_REGISTRY: dict[str, dict[str, str]] = {
    "web3_career": {"tool": "WEB3_CAREER_LIST_JOBS", "version": "20261101_00"},
}

DEFAULT_TOOLKIT = "web3_career"
# User namespace the ACTIVE connection lives under; Composio resolves the
# connection by user_id + toolkit at execution time. Override with
# COMPOSIO_USER_ID when your connection was created under another id.
DEFAULT_USER_ID = "applypilot-spike"

# Rate limits observed live (spike §5): management 600/60s, execution
# 2000/60s, Retry-After advertised on every response — read, never hardcoded.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_TIMEOUT_SECONDS = 30.0


class ComposioError(Exception):
    """A source-level Composio failure.

    Messages carry only API-provided diagnostics (code/slug/message/
    suggested_fix/log_id) and configuration hints — never the API key or any
    other credential value.
    """


# ---------------------------------------------------------------------------
# Field mapping (spike-verified contract)
# ---------------------------------------------------------------------------


def _num(value) -> int | None:
    """Coerce a schema 'integer|number|null' salary value to int, else None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.debug("Composio salary value not numeric: %r", value)
        return None


def _format_salary(raw: dict) -> str | None:
    """Render a human salary string from the two mutually-exclusive, fully
    nullable salary groups (spike §3e.2: explicit and estimated never coexist)."""
    cur = str(raw.get("salary_currency") or "").strip()
    unit = str(raw.get("salary_unit") or "").strip()
    mn = _num(raw.get("salary_min_value"))
    mx = _num(raw.get("salary_max_value"))

    def _join(prefix: str, low: int | None, high: int | None) -> str:
        if low is not None and high is not None:
            body = f"{low:,}-{high:,}"
        elif low is not None:
            body = f"{low:,}+"
        else:
            body = f"up to {high:,}"
        return f"{prefix} {body}".strip()

    if mn is not None or mx is not None:
        salary = _join(cur, mn, mx)
    else:
        e_avg = _num(raw.get("estimated_avg_salary"))
        e_mn = _num(raw.get("estimated_min_salary"))
        e_mx = _num(raw.get("estimated_max_salary"))
        currency_prefix = f"{cur} " if cur else ""
        if e_mn is not None or e_mx is not None:
            salary = f"est. {currency_prefix}{_join('', e_mn, e_mx)}".strip()
        elif e_avg is not None:
            salary = f"est. {currency_prefix}{e_avg:,}".strip()
        else:
            return None
    return f"{salary}/{unit}" if unit else salary


def _compose_location(raw: dict) -> str | None:
    """Trim the provider location (may carry a leading space — spike §3e.8),
    fall back to city/country, and mark remote per the JobSpy convention."""
    loc = str(raw.get("location") or "").strip()
    if not loc:
        bits = [str(b).strip() for b in (raw.get("city"), raw.get("country")) if b]
        loc = ", ".join(bits)
    if not loc:
        return None
    if raw.get("is_remote") and "remote" not in loc.lower():
        loc = f"{loc} (Remote)"
    return loc


def map_web3_job(raw: dict) -> dict:
    """Map one Web3Job listing to the jobs-store row shape (pure).

    Raises ValueError when the listing cannot be stored (no apply_url) —
    callers skip the row and keep the batch alive (B10).

    Contract (spike-verified):
    - ``apply_url`` is the dedupe key and the application link; it is stored
      verbatim (provider ToS forbids rewriting or adding tracking params).
    - ``id``/``company``/``tags``/``is_remote`` are nullable in practice
      (``include_description: false`` nulls them) — every read tolerates null.
    - ``description`` is raw HTML and is stripped to text; company and tags
      fold into the description because the shared store has no dedicated
      columns and the scorer reads the description.
    """
    url = str(raw.get("apply_url") or "").strip()
    if not url:
        raise ValueError("listing has no apply_url (dedupe key)")

    header_bits: list[str] = []
    company = str(raw.get("company") or "").strip() or None
    if company:
        header_bits.append(f"Company: {company}")
    tags = raw.get("tags") or []
    if tags:
        header_bits.append("Tags: " + ", ".join(str(t) for t in tags))
    body_text = strip_html(str(raw["description"])) if raw.get("description") else ""
    description = "\n\n".join([*header_bits, body_text.strip()]).strip() or None

    return {
        "url": url,
        "title": str(raw.get("title") or "").strip() or None,
        "salary": _format_salary(raw),
        "description": description,
        "location": _compose_location(raw),
        # The provider link is the application link — stored verbatim (ToS).
        "application_url": url,
    }


def store_web3_jobs(conn: sqlite3.Connection, raw_jobs: list[dict],
                    accept_locs: list[str] | None = None,
                    reject_locs: list[str] | None = None) -> tuple[int, int, int, int]:
    """Store mapped listings, applying the shared location filter.

    Returns (new, existing, skipped, filtered_location).

    B10: one malformed listing never aborts the batch — it is skipped with a
    warning. Location filtering matches the other sources (B11 semantics:
    remote jobs always pass; empty accept list accepts all non-remote).
    Dedupe rides on the store's ``url`` primary key (= apply_url).
    """
    accept_locs = accept_locs or []
    reject_locs = reject_locs or []
    now = datetime.now(timezone.utc).isoformat()
    new = existing = skipped = filtered = 0
    for raw in raw_jobs:
        try:
            job = map_web3_job(raw)
        except ValueError as exc:
            log.warning("Skipping malformed Composio listing: %s", exc)
            skipped += 1
            continue
        if not _location_ok(job["location"], accept_locs, reject_locs):
            filtered += 1
            continue

        description = job["description"]
        # JobSpy convention: a substantial description satisfies enrichment
        # up front (detail_scraped_at set), so the enrich stage skips it.
        full_description = description if description and len(description) > 200 else None
        detail_scraped_at = now if full_description else None
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["url"], job["title"], job["salary"], job["description"], job["location"],
                 "web3_career", "composio", now, full_description, job["application_url"],
                 detail_scraped_at),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing, skipped, filtered


# ---------------------------------------------------------------------------
# Client (spike-verified REST recipe over httpx)
# ---------------------------------------------------------------------------


class ComposioClient:
    """Thin HTTP client for the Composio v3.1 tool API.

    Plain httpx (a base dependency) against the raw REST endpoints — the
    recipe the spike verified live. ``transport`` accepts an
    ``httpx.MockTransport`` for fake-HTTP tests; the base install needs
    nothing beyond what ``applypilot`` already ships.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        user_id: str | None = None,
        timeout: float = _TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        backoff_base: float = 2.0,
    ):
        if not api_key:
            raise ValueError("Composio API key is required (COMPOSIO_API_KEY)")
        self._user_id = (
            user_id
            or os.getenv("COMPOSIO_USER_ID", "").strip()
            or DEFAULT_USER_ID
        )
        # Explicit connected-account override; unset lets Composio resolve
        # the user's connection by user_id + toolkit (spike §4 row 5).
        self._account_id = os.getenv("COMPOSIO_CONNECTED_ACCOUNT_ID", "").strip()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"x-api-key": api_key},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "ComposioClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- Endpoints -------------------------------------------------------------

    def create_session(self, toolkit_slug: str, tool_slugs: list[str]) -> dict:
        """Create a v3.1 tool-router session scoped to one toolkit.

        Composio does NOT validate the toolkit slug (a bogus slug returns 201
        silently with no tools — spike §4 row 3), so callers must verify the
        expected tool appears in ``tool_router_tools`` before executing.
        """
        body = self._request("POST", _SESSION_PATH, {
            "toolkit_slug": toolkit_slug,
            "user_id": self._user_id,
            "preload": {"tools": list(tool_slugs)},
        })
        return body if isinstance(body, dict) else {}

    def execute_tool(self, tool_slug: str, arguments: dict, version: str) -> dict:
        """Execute a tool and return its ``data`` payload.

        Recipe verified live (spike §3b/§6.2): x-api-key auth, body
        ``{"arguments", "user_id", "version"}`` with the version pinned (the
        REST default pins to "00000000_00"). Raises ComposioError on both
        failure shapes: platform errors (non-2xx) and envelope failures
        (HTTP 200 with ``successful: false``).
        """
        payload: dict = {"arguments": arguments, "user_id": self._user_id, "version": version}
        if self._account_id:
            payload["connected_account_id"] = self._account_id
        body = self._request("POST", _EXECUTE_PATH + tool_slug, payload)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # -- Transport -------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict) -> dict:
        """Send one request with bounded retry on 429/5xx/transport errors."""
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(method, path, json=payload)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    # Exception type only: transport text may embed request
                    # state, and the error must stay credential-free (S3).
                    raise ComposioError(
                        f"Composio request failed: {type(exc).__name__}"
                    ) from exc
                self._sleep(self._backoff_seconds(attempt))
                continue
            if response.status_code in _RETRYABLE_STATUSES and attempt < self._max_retries:
                self._sleep(self._retry_wait_seconds(response, attempt))
                continue
            return self._parse_response(response)
        raise ComposioError("Composio request retry loop exited unexpectedly")

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict:
        """Branch on the two distinct Composio failure shapes (spike §4):

        - platform errors: non-2xx JSON ``{"error": {message, code, slug}}``
        - upstream/tool validation: HTTP 200 with ``successful: false`` and
          the real message inside the envelope — branch on ``successful``,
          never on the HTTP status alone.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise ComposioError(
                f"Composio returned non-JSON response (HTTP {response.status_code})"
            ) from exc
        if not isinstance(body, dict):
            raise ComposioError(
                f"Composio returned an unexpected response shape (HTTP {response.status_code})"
            )

        if response.is_success:
            if body.get("successful") is False:
                raise ComposioError(_envelope_error_message(body))
            return body

        error = body.get("error")
        if isinstance(error, dict):
            message = f"Composio error {error.get('code')} ({error.get('slug')}): {error.get('message')}"
            if error.get("suggested_fix"):
                message += f" {error['suggested_fix']}"
            raise ComposioError(message)
        raise ComposioError(f"Composio returned HTTP {response.status_code}")

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** attempt), 60.0)

    def _retry_wait_seconds(self, response: httpx.Response, attempt: int) -> float:
        """Rate-limit wait: Retry-After, then x-ratelimit-reset (spike §5).

        Header hints are clamped to the shared ceiling: x-ratelimit-reset is
        an epoch timestamp on many APIs, which would otherwise sleep ~1.7e9 s.
        """
        for header in ("retry-after", "x-ratelimit-reset"):
            value = response.headers.get(header)
            if value:
                try:
                    return min(max(0.0, float(value)), MAX_RATE_LIMIT_WAIT_SECONDS)
                except ValueError:
                    log.debug("Composio %s header not numeric: %r", header, value)
        return self._backoff_seconds(attempt)


def _envelope_error_message(body: dict) -> str:
    """Human message from a ``successful: false`` envelope.

    ``log_id`` is included: Composio support asks for it, and it is a
    server-generated opaque id carrying no secrets.
    """
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    message = body.get("error") or data.get("message") or "unknown tool error"
    if not isinstance(message, str):
        message = str(message)
    log_id = body.get("log_id")
    suffix = f" (log_id: {log_id})" if log_id else ""
    return f"Composio tool execution failed: {message}{suffix}"


# ---------------------------------------------------------------------------
# Discovery entry point
# ---------------------------------------------------------------------------


def _resolve_toolkits(composio_block: dict) -> list[str]:
    """Toolkit list: COMPOSIO_TOOLKITS env (comma list) → searches.yaml
    ``composio.toolkits`` → default."""
    env = os.getenv("COMPOSIO_TOOLKITS", "")
    if env.strip():
        return [t.strip() for t in env.split(",") if t.strip()]
    from_block = composio_block.get("toolkits")
    if from_block:
        return [str(t).strip() for t in from_block if str(t).strip()]
    return [DEFAULT_TOOLKIT]


def _tool_arguments(composio_block: dict) -> dict:
    """Search parameters from the searches.yaml ``composio`` block.

    All optional and passed through unvalidated: invalid values come back as
    HTTP 200 ``successful: false`` envelopes → source error (spike §4 rows
    10-12), so the 54-value tag enum is not duplicated here.
    """
    args: dict = {}
    if composio_block.get("tag"):
        args["tag"] = str(composio_block["tag"])
    if composio_block.get("limit") is not None:
        args["limit"] = int(composio_block["limit"])
    if composio_block.get("country"):
        args["country"] = str(composio_block["country"])
    if composio_block.get("remote_only"):
        args["remote_only"] = True
    if composio_block.get("include_description") is False:
        args["include_description"] = False
    return args


def _zero_stats() -> dict:
    return {"new": 0, "existing": 0, "skipped_listings": 0, "filtered": 0, "toolkits": []}


def run_composio_discovery(cfg: dict | None = None, client: ComposioClient | None = None) -> dict:
    """Main entry point wired into the pipeline's discover stage.

    Enablement matches the other discovery sources (registry/config
    presence): the source runs when COMPOSIO_API_KEY is set or a
    ``composio:`` block exists in searches.yaml, and quietly no-ops
    otherwise.

    Per toolkit: a session is created scoped to the toolkit (verifying the
    known tool registers — bogus slugs 201 silently), then the tool executes
    via the REST recipe and listings are stored with per-row guards (B10).

    Source-level failures (bad key, missing ACTIVE connection, upstream
    validation errors) raise ComposioError when every mapped toolkit failed —
    the discover-stage wrapper converts that into the discovery summary,
    exactly like the other sources (B7).

    Args:
        cfg: Override the search config dict (loads searches.yaml when None).
        client: Injected client (tests pass a fake over a mock transport).
    """
    cfg = config.load_search_config() if cfg is None else cfg
    cfg = cfg or {}
    block = cfg.get("composio") or {}
    # Key presence is enablement — an empty ``composio:`` block still means
    # "enabled with defaults".
    block_present = "composio" in cfg
    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()

    if not api_key:
        if block_present:
            log.warning(
                "Composio source is enabled in searches.yaml but COMPOSIO_API_KEY "
                "is not set — skipping Composio discovery"
            )
        else:
            log.debug("Composio discovery not configured (no COMPOSIO_API_KEY, no composio block)")
        return _zero_stats()

    toolkits = _resolve_toolkits(block)
    mapped = [(tk, TOOL_REGISTRY[tk]) for tk in toolkits if tk in TOOL_REGISTRY]
    for tk in toolkits:
        if tk not in TOOL_REGISTRY:
            log.warning(
                "Composio toolkit '%s' has no verified job-search mapping — skipping "
                "(known: %s)", tk, ", ".join(sorted(TOOL_REGISTRY)),
            )
    if not mapped:
        log.warning("No configured Composio toolkits have a verified mapping: %s", toolkits)
        return _zero_stats()

    own_client = client is None
    cl = client or ComposioClient(api_key=api_key)
    totals = _zero_stats()
    failures: list[str] = []
    try:
        init_db()
        for toolkit_slug, entry in mapped:
            tool = entry["tool"]
            try:
                session = cl.create_session(toolkit_slug, [tool])
                if tool not in (session.get("tool_router_tools") or []):
                    raise ComposioError(
                        f"Composio session for toolkit '{toolkit_slug}' did not register "
                        f"tool {tool} — check the toolkit slug and your Composio account"
                    )
                data = cl.execute_tool(tool, _tool_arguments(block), entry["version"])
            except ComposioError as exc:
                failures.append(f"{toolkit_slug}: {exc}")
                continue

            raw_jobs = data.get("jobs") or []
            attribution = data.get("attribution")
            if attribution:
                # Provider ToS presentation rules — logged so CLI users see them.
                log.info("web3.career attribution: %s", attribution)
            result_count = data.get("result_count")
            if isinstance(result_count, int) and result_count != len(raw_jobs):
                log.debug("Composio result_count %s != jobs returned (%s)",
                          result_count, len(raw_jobs))

            new, existing, skipped, filtered = store_web3_jobs(
                get_connection(), raw_jobs, *location_filters(cfg or {}))
            totals["new"] += new
            totals["existing"] += existing
            totals["skipped_listings"] += skipped
            totals["filtered"] += filtered
            totals["toolkits"].append(toolkit_slug)
            log.info(
                "Composio '%s': %d listings -> %d new, %d dupes, %d skipped, %d filtered",
                toolkit_slug, len(raw_jobs), new, existing, skipped, filtered,
            )
    finally:
        if own_client:
            cl.close()

    for failure in failures:
        log.error("Composio discovery error — %s", failure)

    if failures and len(failures) == len(mapped):
        # Every toolkit failed — surface a source error through the
        # discover-stage summary like the other sources (B7).
        raise ComposioError("; ".join(failures))

    return totals


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def composio_state() -> tuple[str, str]:
    """(state, note) for the Composio discovery check, in clean vocabulary.

    state is ``"ok"`` | ``"warn"`` | ``"optional"`` — presentation-free, so
    the portal's System Check page can reuse the same check logic the CLI
    doctor renders. The note is CLI-console copy (may name config files);
    portal surfaces use their own plain-language text and read only the
    state. The API key is masked through llm.mask_key (last 4 max) — the
    full key never reaches any surface (S2 discipline).
    """
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    try:
        cfg = config.load_search_config()
    except Exception as exc:  # doctor reports; it never crashes on user config
        return ("warn",
                f"searches.yaml unreadable, Composio config unknown: {type(exc).__name__}")
    block_present = isinstance(cfg, dict) and "composio" in cfg

    if not key:
        if block_present:
            return ("warn",
                    "composio source enabled in searches.yaml but COMPOSIO_API_KEY is not set")
        return ("optional",
                "Set COMPOSIO_API_KEY in .env to enable web3.career discovery via Composio")

    block = (cfg or {}).get("composio") or {}
    toolkits = _resolve_toolkits(block)
    note = f"toolkits: {', '.join(toolkits)} | key {mask_key(key)}"
    return "ok", note


def doctor_row() -> tuple[str, str]:
    """(status, note) row for ``applypilot doctor`` — Rich marks over the
    clean state from composio_state()."""
    state, note = composio_state()
    return {
        "ok": "[green]OK[/green]",
        "warn": "[yellow]WARN[/yellow]",
        "optional": "[dim]optional[/dim]",
    }[state], note
