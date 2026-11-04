"""Credential redaction primitives.

Security remediation S1/S3: secret values must never reach error messages,
agent prompts written to disk, or log files. This module is the single place
that knows how to mask them; callers own the policy of when to redact.
"""

from typing import Iterable

# Marker left in place of a redacted value. Distinctive enough to spot in
# logs, short enough not to disrupt surrounding text.
REDACTED = "[REDACTED]"


def redact_proxy(proxy_str: str) -> str:
    """Mask proxy credentials (user:pass) for safe use in errors and logs.

    host:port are not secrets and are kept for debuggability; everything
    after them (user, pass, or any malformed remainder) is masked. Input
    that does not split into host:port at all is redacted entirely, since
    it could be a bare credential.

    S3: ValueError/log sites must never echo the raw host:port:user:pass
    string -- it propagates into stage error strings and console output.
    """
    parts = proxy_str.split(":")
    if len(parts) == 1:
        return REDACTED
    if len(parts) == 2:
        return proxy_str  # host:port carries no credentials
    return ":".join(parts[:2]) + ":" + REDACTED


def redact_secret_values(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of each secret value with REDACTED.

    Empty/None secrets are skipped so a missing optional credential does
    not redact the empty string (which would corrupt the whole text).
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text
