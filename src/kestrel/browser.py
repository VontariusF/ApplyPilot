# SPDX-License-Identifier: AGPL-3.0-only
"""Guarded access to the optional Playwright browser engine.

Playwright ships behind the ``[browser]`` extra, not as a core dependency:
an agent-driven Kestrel needs no built-in browser — the host agent's own
browser covers page fetching. Modules that drive Playwright directly
import through here, so a missing extra exits with both routes spelled
out instead of an ImportError traceback.
"""

_BROWSER_HINT = (
    "This feature needs Kestrel's built-in browser engine, which is an "
    "optional dependency. Either install it with "
    "`pip install 'kestrel-agent[browser]'` for the built-in fallback "
    "engine and built-in scraping, or drive Kestrel through your coding "
    "agent — its own browser can fetch pages."
)


def sync_playwright():
    """Return ``playwright.sync_api.sync_playwright``, or exit with guidance.

    SystemExit (not a raised exception) matches the codebase's fatal-setup
    convention (config.py's tier gate): the message lands on stderr with no
    traceback, and stdout stays clean for agent-verb JSON consumers.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(_BROWSER_HINT)
    return sync_playwright
