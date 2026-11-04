"""Portal authentication: localhost token, cookie handling, host checks.

A token is generated at first launch and stored at 0600 under
~/.applypilot/. Every request must present it — as a cookie, an
X-Portal-Token or Bearer header, or a ?token= query parameter (the
launch URL uses the query form once, then a cookie is set). This defends
against other local users (who cannot read the file) and DNS-rebinding
pages (whose cross-site requests carry no SameSite=Strict cookie).
"""

import secrets
from pathlib import Path

from applypilot import config

COOKIE_NAME = "portal_token"

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


def extract_token(
    cookie_token: str | None,
    header_token: str | None,
    query_token: str | None,
) -> str | None:
    """First present token source, in cookie > header > query order."""
    return cookie_token or header_token or query_token


def host_is_allowed(host_header: str | None) -> bool:
    """True when the Host header names localhost (with any port)."""
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") <= 1 else host_header
    # Bare IPv6 like [::1] arrives bracketed on some stacks.
    host = host.strip("[]")
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
