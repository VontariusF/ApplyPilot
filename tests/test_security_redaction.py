"""S3: proxy credentials must never surface in error messages or logs.

Covers the shared redaction primitives (applypilot.security) and both
discovery call sites: jobspy.parse_proxy's ValueError and workday.setup_proxy's
warning.
"""

import logging

import pytest

from applypilot.discovery import jobspy, workday
from applypilot.security import REDACTED, redact_proxy, redact_secret_values

# Malformed proxy (5 segments: the realistic case of a password containing a
# colon). Only malformed input reaches the error paths being tested.
BAD_PROXY = "proxy.example.com:8080:proxyuser:s3cret-pass:extra"


def test_redact_proxy_masks_credentials():
    four_part = "proxy.example.com:8080:proxyuser:s3cret-pass"
    assert redact_proxy(four_part) == "proxy.example.com:8080:[REDACTED]"


def test_redact_proxy_keeps_credential_free_host_port():
    assert redact_proxy("proxy.example.com:8080") == "proxy.example.com:8080"


def test_redact_proxy_bare_token_redacted_entirely():
    # A single segment could be a stray credential; nothing is safe to show.
    assert redact_proxy("just-a-token") == REDACTED


def test_redact_proxy_malformed_extra_segments():
    assert redact_proxy("host:port:user:pass:extra") == "host:port:[REDACTED]"


def test_redact_secret_values_replaces_all_occurrences():
    assert redact_secret_values("a SECRET b SECRET c", ["SECRET"]) == "a [REDACTED] b [REDACTED] c"


def test_redact_secret_values_skips_empty_secret():
    # Empty secrets must not redact the empty string (whole-text corruption).
    assert redact_secret_values("unchanged", ["", None]) == "unchanged"


def test_parse_proxy_error_hides_credentials():
    with pytest.raises(ValueError) as exc_info:
        jobspy.parse_proxy(BAD_PROXY)
    message = str(exc_info.value)
    # The credential values are gone; host:port stays for debuggability.
    assert "s3cret-pass" not in message
    assert "proxyuser" not in message
    assert REDACTED in message
    assert "proxy.example.com:8080" in message


def test_workday_proxy_warning_hides_credentials(caplog, monkeypatch):
    monkeypatch.setattr(workday, "_opener", None)
    with caplog.at_level(logging.WARNING, logger="applypilot.discovery.workday"):
        workday.setup_proxy(BAD_PROXY)

    assert "s3cret-pass" not in caplog.text
    assert "proxyuser" not in caplog.text
    assert REDACTED in caplog.text
    # Degraded gracefully: a credential-free opener is still configured.
    assert workday._opener is not None
