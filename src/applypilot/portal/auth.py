"""Portal authentication: localhost token, cookie handling, host checks.

A token is generated at first launch and stored at 0600 under
~/.applypilot/. Every request must present it — as a cookie, an
X-Portal-Token or Bearer header. The token is never accepted from the
URL: a ?token= query string lands verbatim in uvicorn access logs,
browser history, and Referer headers (sweep F9), so the one-time entry
point is the POST /login form (served as the 401 body), which swaps the
submitted token for an HttpOnly SameSite=Strict cookie. This defends
against other local users (who cannot read the file) and DNS-rebinding
pages (whose cross-site requests carry no SameSite=Strict cookie).
"""

import logging
import os
import re
import secrets
from pathlib import Path

from applypilot import config

COOKIE_NAME = "portal_token"

# Opt-out of the portal TOKEN check (P2-4): the deployed Mac launchd unit
# runs with this set. Name matches the deployed plist verbatim. Only the
# token check relaxes — the middleware's localhost host-check runs first
# regardless, so an opted-out portal is still unreachable off the machine.
AUTH_DISABLE_ENV = "APPLIPILOT_PORTAL_DISABLE_AUTH"
_AUTH_DISABLE_TRUTHY = {"1", "true", "yes"}


def auth_disabled() -> bool:
    """True when APPLIPILOT_PORTAL_DISABLE_AUTH opts the token check out."""
    return os.environ.get(AUTH_DISABLE_ENV, "").strip().lower() in _AUTH_DISABLE_TRUTHY


# Hosts the portal answers for. Binding is 127.0.0.1; this blocks DNS
# rebinding, where an attacker page resolves a public name to 127.0.0.1
# and the browser sends that name in the Host header.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def token_path() -> Path:
    """Where the portal token lives (resolves config.APP_DIR at call time)."""
    return config.APP_DIR / "portal_token"


def get_or_create_token() -> str:
    """Load the portal token, generating and storing one at 0600 on first use."""
    path = token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    config.write_secret_text(path, token + "\n")
    return token


def token_matches(provided: str | None, expected: str) -> bool:
    """Constant-time token comparison."""
    if not provided:
        return False
    return secrets.compare_digest(provided.strip(), expected)


def extract_token(cookie_token: str | None, header_token: str | None) -> str | None:
    """First present token source, in cookie > header order.

    Query strings are deliberately not a token source: the token must
    never appear in a URL, where access logs, browser history, and
    Referer headers capture it (sweep F9).
    """
    return cookie_token or header_token


# A stale ?token= link (pre-F9 bookmark, old launch URL) must not leak the
# token into the uvicorn access log even though the request now 401s — the
# token is long-lived, so the leaked value stays valid.
_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s\"]+")


def redact_token_query(text: str) -> str:
    """Replace any token=<value> query parameter with token=[REDACTED]."""
    return _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", text)


class _TokenQueryRedactionFilter(logging.Filter):
    """Scrub token query parameters from uvicorn access-log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "token=" in message:
            # The record is re-formatted: msg holds the final text, args
            # are dropped so %-expansion cannot resurrect the raw value.
            record.msg = redact_token_query(message)
            record.args = None
        return True


def install_access_log_redaction() -> None:
    """Attach the token-redaction filter to uvicorn's access logger (once)."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _TokenQueryRedactionFilter) for f in access_logger.filters):
        access_logger.addFilter(_TokenQueryRedactionFilter())


def host_is_allowed(host_header: str | None) -> bool:
    """True when the Host header names localhost (with any port)."""
    if not host_header:
        return False
    host = host_header
    if host.startswith("["):
        # Bracketed IPv6 with a port ("[::1]:8000"): the colon-split kept
        # the whole header and .strip("[]") only removed the leading
        # bracket, so IPv6-localhost was refused.
        host = host[1:host.index("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host in ALLOWED_HOSTS


def set_token_cookie(response, token: str) -> None:
    """Persist the token as an HttpOnly SameSite=Strict cookie."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        max_age=30 * 24 * 3600,
        path="/",
    )
