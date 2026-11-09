# SPDX-License-Identifier: AGPL-3.0-only
"""System Check: the portal's in-process diagnostic surface.

Replaces the terminal ``kestrel doctor`` for portal users: the same
check logic, run in-process and rendered as a plain-language list. Every
check composes the existing sources of truth — engine_auth statuses,
the LLM provider detector, config paths, the Composio doctor state — and
never reimplements them. Each check carries a pass/warn/fail state, a
one-sentence meaning, and a fix affordance where the portal has a page
for it; checks only the machine itself can fix say so in plain text
instead of a stub button.

No check output ever mentions shell commands or config-file paths: the
page is a zero-terminal surface (the copy-scan test guards this).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass

from kestrel import config, engine_auth, llm, onboarding
from kestrel.discovery import composio

# The three states a check can be in (badge + wording switch on these).
PASS = "pass"
WARN = "warn"
FAIL = "fail"

STATE_LABELS = {PASS: "OK", WARN: "Needs attention", FAIL: "Missing"}


@dataclass(frozen=True)
class Fix:
    """A portal-page fix affordance: where to go and what to do there."""

    label: str
    href: str


@dataclass(frozen=True)
class Check:
    """One dependency of the portal, and what to do when it needs work."""

    id: str
    label: str
    state: str  # PASS | WARN | FAIL
    meaning: str  # one plain sentence — what this result means
    fix: Fix | None = None  # button only where a portal page can act


# ---------------------------------------------------------------------------
# Check builders — each imports its logic from the source that owns it
# ---------------------------------------------------------------------------


def _disk_checks() -> list[Check]:
    """Profile / resume / search-setup checks over the config path constants."""
    fix_profile = Fix("Start your profile", "/onboarding")
    if config.PROFILE_PATH.exists():
        profile = Check(
            "profile", "Your profile", PASS,
            "Your profile is saved — the interview captured your details.",
        )
    else:
        profile = Check(
            "profile", "Your profile", FAIL,
            "No profile yet — the jobs board and document tailoring need one.",
            fix_profile,
        )

    fix_resume = Fix("Add your resume", "/onboarding")
    if config.RESUME_PATH.exists():
        resume = Check(
            "resume", "Your resume", PASS,
            "Your resume is saved — the AI reads it when tailoring documents.",
        )
    elif config.RESUME_PDF_PATH.exists():
        resume = Check(
            "resume", "Your resume", WARN,
            "Your resume is saved, but only as a PDF — the AI needs a plain-text "
            "version to read it.",
            fix_resume,
        )
    else:
        resume = Check(
            "resume", "Your resume", FAIL,
            "No resume yet — matching and tailoring need one.",
            fix_resume,
        )

    if config.SEARCH_CONFIG_PATH.exists():
        search = Check(
            "search", "Job-search preferences", PASS,
            "Your job-search preferences are saved.",
        )
    else:
        search = Check(
            "search", "Job-search preferences", WARN,
            "No search preferences yet — a built-in example is used until you "
            "set your own.",
            Fix("Finish setup", "/onboarding"),
        )

    return [profile, resume, search]


def _discovery_checks() -> list[Check]:
    """Where jobs come from: the board reader and the optional Composio source."""
    try:
        jobspy_available = importlib.util.find_spec("jobspy") is not None
    except (ImportError, ValueError):
        jobspy_available = False
    if jobspy_available:
        jobspy = Check(
            "jobspy", "Job-board reader", PASS,
            "The job-board reader is ready — it finds listings on the major boards.",
        )
    else:
        jobspy = Check(
            "jobspy", "Job-board reader", WARN,
            "The job-board reader is missing from this install — job discovery "
            "finds nothing until it is restored. Reinstalling Kestrel "
            "brings it back.",
        )

    state, _note = composio.composio_state()  # note is CLI copy; the page writes its own
    if state == "ok":
        composio_check = Check(
            "composio", "Web3 job source", PASS,
            "Your Web3 job-listing source is connected.",
        )
    elif state == "warn":
        composio_check = Check(
            "composio", "Web3 job source", WARN,
            "A job source you switched on needs its Composio access key before "
            "it can find listings. Add the key on this machine, then check again.",
        )
    else:  # optional, not configured — healthy without it
        composio_check = Check(
            "composio", "Web3 job source", PASS,
            "Optional. A source for Web3 job listings — everything works "
            "without it.",
        )

    return [jobspy, composio_check]


def _ai_checks() -> list[Check]:
    """Provider + engine checks, from onboarding.readiness() and engine_auth."""
    ready = onboarding.readiness()

    if not ready["llm_ready"]:
        provider = Check(
            "provider", "AI provider", FAIL,
            "No AI provider is configured yet — nothing AI-powered can run.",
            Fix("Open provider settings", "/settings"),
        )
    else:
        # Env-var presence alone proves nothing: a mispointed custom
        # endpoint passes readiness while every apply run fails. The
        # probe sends one minimal request through the same client real
        # runs use (bounded by a short timeout), so green here means the
        # endpoint actually answered and parsed.
        probe_state, detail = llm.probe_provider()
        if probe_state == "reachable":
            provider = Check(
                "provider", "AI provider", PASS,
                "Your AI provider is set up and answered a test request — "
                "summarizing, tailoring, and the interview agent run on it.",
            )
        elif probe_state == "degraded":
            # The endpoint is alive but its responses are unusable — a
            # green check here would hide every later parse failure.
            provider = Check(
                "provider", "AI provider", WARN,
                f"{detail}",
                Fix("Open provider settings", "/settings"),
            )
        elif probe_state == "slow":
            # Slow is not down: the endpoint outlasted the probe's bounds
            # but real runs allow more (reliability sweep P2-6). A FAIL
            # here would contradict runs that succeed against the same
            # endpoint.
            provider = Check(
                "provider", "AI provider", WARN,
                f"{detail}",
                Fix("Open provider settings", "/settings"),
            )
        else:
            provider = Check(
                "provider", "AI provider", FAIL,
                f"{detail} Nothing AI-powered can run until it is fixed.",
                Fix("Open provider settings", "/settings"),
            )

    claude = ready["claude"]
    if claude.state == engine_auth.SIGNED_IN:
        claude_check = Check(
            "claude", "Claude Code engine", PASS,
            "Claude Code is signed in — the default auto-apply engine is ready.",
        )
    elif claude.state == engine_auth.SIGNED_OUT:
        claude_check = Check(
            "claude", "Claude Code engine", WARN,
            "Claude Code is installed but not signed in — signing in unlocks "
            "auto-apply with it.",
            Fix("Check agent sign-in", "/engine"),
        )
    else:
        claude_check = Check(
            "claude", "Claude Code engine", FAIL,
            "Claude Code isn't available on this machine — your own AI "
            "provider can still run everything.",
            Fix("Use my AI provider instead", "/settings"),
        )

    droid = engine_auth.droid_status()
    if droid.state == engine_auth.SIGNED_IN:
        droid_check = Check(
            "droid", "Droid engine", PASS,
            "The Droid engine is ready as an alternative auto-apply engine.",
        )
    elif droid.state == engine_auth.SIGNED_OUT:
        droid_check = Check(
            "droid", "Droid engine", WARN,
            "Droid is installed but its access key is missing — only needed "
            "if you prefer it over Claude Code.",
            Fix("Set up the Droid engine", "/engine"),
        )
    else:
        droid_check = Check(
            "droid", "Droid engine", PASS,
            "Optional. A second auto-apply engine — you only need it if you "
            "prefer it over Claude Code.",
        )

    return [provider, claude_check, droid_check]


def _browser_checks() -> list[Check]:
    """What the auto-apply browser needs: Chrome, Node.js, CAPTCHA key."""
    try:
        chrome_found = config.get_chrome_path() is not None
    except FileNotFoundError:
        chrome_found = False
    chrome = Check(
        "chrome", "Chrome browser", PASS if chrome_found else FAIL,
        "Chrome is ready — it's the browser the auto-apply engine drives."
        if chrome_found else
        "Chrome isn't installed on this machine — applying to jobs needs it. "
        "Install Chrome, then check again.",
    )

    npx_found = shutil.which("npx") is not None
    node = Check(
        "node", "Node.js", PASS if npx_found else FAIL,
        "Node.js is ready — it runs the browser helper during auto-apply."
        if npx_found else
        "Node.js isn't installed — the browser helper can't run without it. "
        "Install Node.js 18 or newer from nodejs.org, then check again.",
    )

    config.load_env()  # mirror the CLI doctor: .env keys reach the environment first
    capsolver_set = bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())
    capsolver = Check(
        "capsolver", "CAPTCHA solving", PASS,
        "CAPTCHA solving is enabled — sites that ask get solved automatically."
        if capsolver_set else
        "Optional. Only needed when a job site shows a CAPTCHA — add a key "
        "in Settings anytime.",
        None if capsolver_set else Fix("Add a key in Settings", "/settings"),
    )

    return [chrome, node, capsolver]


# Grouping order for the page: setup first, then inputs, AI, and browser.
_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Your setup", ("profile", "resume", "search")),
    ("Where jobs come from", ("jobspy", "composio")),
    ("AI that runs the work", ("provider", "claude", "droid")),
    ("The auto-apply browser", ("chrome", "node", "capsolver")),
]


def run_checks() -> list[Check]:
    """Every dependency, in page order. Runs the real checks in-process."""
    return _disk_checks() + _discovery_checks() + _ai_checks() + _browser_checks()


def grouped_checks() -> list[tuple[str, list[Check]]]:
    """run_checks() grouped for the page's section headings."""
    checks = {c.id: c for c in run_checks()}
    return [
        (heading, [checks[cid] for cid in ids if cid in checks])
        for heading, ids in _GROUPS
    ]
