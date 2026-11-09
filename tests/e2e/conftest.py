"""Playwright E2E harness: a real Chromium against the real portal server.

The portal boots in-process (uvicorn in a background thread) instead of a
subprocess so the shared `fake_llm` fixture and the app_dir/portal_env
store isolation apply to the serving app. Every fixture is function-scoped:
each test gets its own store, token, and port.

Journeys run through the playwright ASYNC API, each inside its own
asyncio.run. The sync API is deliberately not used: it drives the browser
from a greenlet parked inside a running event loop on the main thread, and
inside pytest that loop stays registered as running for every later test —
breaking any test that calls asyncio.run afterwards (the SSE suites do).
A per-journey loop is created and fully unwound, so nothing leaks.

Runs headless everywhere by default; set KESTREL_E2E_HEADED=1 to watch
it locally. When the Chromium binary is missing, the suite SKIPS with the
install hint instead of failing — plain `pytest` stays green for
contributors who have not run `playwright install chromium`. CI installs
the browser explicitly (`.github/workflows/ci.yml`), so the matrix runs
these tests for real.
"""

import asyncio
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


def _headed() -> bool:
    """True when the operator asked to watch the browser (local debugging)."""
    return os.environ.get("KESTREL_E2E_HEADED", "").strip().lower() in {"1", "true", "yes"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# Server under test
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_server(portal_env, seeded_profile, fake_llm, monkeypatch):
    """The portal over real HTTP: isolated store, fake provider, real server.

    Depends on `fake_llm` even though route rendering alone never calls the
    LLM: the contract is that the whole server lifetime runs on the fake,
    so any surface that grows an LLM call stays off the network.
    """
    import uvicorn

    from kestrel.portal import app as portal_app_module
    from kestrel.portal.app import create_app

    # Speed up SSE idle-close in tests (generators read these at runtime).
    monkeypatch.setattr(portal_app_module, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(portal_app_module, "CHAT_POLL_SECONDS", 0.05)

    app = create_app()
    port = _free_port()
    # log_config=None and default access logging: uvicorn's configure_logging
    # sets uvicorn.access propagate=False (for access_log=False, via dictConfig
    # otherwise) as a PROCESS-GLOBAL side effect, which breaks caplog-based
    # suites (test_portal_auth's redaction test) for the rest of the session —
    # the same discipline as portal_env's env snapshot.
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_config=None,
        timeout_graceful_shutdown=5,  # SSE streams must not wedge shutdown
    ))
    thread = threading.Thread(target=server.run, daemon=True, name="portal-e2e")
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("portal server did not start within 10s")

    yield SimpleNamespace(base_url=f"http://127.0.0.1:{port}", token=app.state.token)

    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("portal server did not shut down within 10s")


@pytest.fixture()
def harmless_runs(monkeypatch):
    """Presets spawn real subprocesses that outlive a Start click but do no
    stage work.

    The route-level pattern (tests/test_portal_pages.py) applied to the
    serving app: the registry row, streaming, and lifecycle stay real —
    only the child is swapped for `python -c "import time; time.sleep(3)"`,
    so a Start click in the browser can never run discovery or apply
    stages. The child must outlive the click's render, though: a child
    that exits instantly (`pass`) lets the runner flip the card back to
    idle within one 50ms poll — before the browser's wait for the Stop
    button observes the running state — a race that flakes under load.
    """
    import sys

    from kestrel.portal import runner

    original = runner.start_run

    def fake_start(kind, params, command=None, emit_jsonl=None):
        return original(kind, params,
                        command=[sys.executable, "-c",
                                 "import time; time.sleep(3)"],
                        emit_jsonl=False)

    monkeypatch.setattr(runner, "start_run", fake_start)


@pytest.fixture()
def seeded_job(portal_env):
    """One scored job in the isolated store, so the board has a row to show."""
    from datetime import datetime, timezone

    from kestrel.database import init_db

    conn = init_db()
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, site, location, description, "
        "full_description, fit_score, score_reasoning, discovered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("https://jobs.example/posting-1", "Pwidget Engineer", "ExampleBoard",
         "Remote", "short desc", "full description text", 9,
         "Strong match on pwidget skills.",
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return "https://jobs.example/posting-1"


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

class ChromiumMissing(RuntimeError):
    """The Playwright Chromium binary is not installed on this machine."""


# CI runners starve while the three Python-matrix jobs share the queue's CPU
# budget: healthy journeys have hit Playwright's 30s default cap on locator
# clicks, waits, and text reads — a different e2e test each run, and main's
# own CI flakes the same way. Raise the class-level default; assertions that
# genuinely expect absence still pass instantly.
E2E_TIMEOUT_MS = 120_000


@pytest.fixture()
def chromium():
    """A fresh Chromium page per journey, as an async context manager."""
    from playwright.async_api import async_playwright

    @asynccontextmanager
    async def new_page():
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=not _headed())
            except Exception as exc:  # playwright raises its own Error type here
                text = str(exc)
                if "Executable" in text or "install" in text.lower():
                    raise ChromiumMissing(text) from exc
                raise
            try:
                page = await browser.new_page()
                page.set_default_timeout(E2E_TIMEOUT_MS)
                yield page
            finally:
                await browser.close()

    return new_page


@pytest.fixture()
def run_journey(chromium):
    """Run an async browser journey; skip (not fail) when Chromium is absent."""
    def run(scenario) -> None:
        try:
            asyncio.run(scenario)
        except ChromiumMissing as exc:
            pytest.skip(
                "Playwright Chromium is not installed — "
                f"run `playwright install chromium` ({exc})"
            )
    return run
