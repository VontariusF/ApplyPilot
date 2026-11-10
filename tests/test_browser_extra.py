# SPDX-License-Identifier: AGPL-3.0-only
"""The [browser] extra contract (spec art_jwQhpPx9, W2).

Playwright is optional packaging, not a core dependency: an agent-driven
install never needs it. These tests pin the metadata split and the guarded
lazy-import path all direct-Playwright modules share.
"""

import importlib.metadata
import sys
import types

import pytest

from kestrel import browser


def _requires_dist() -> list[str]:
    return list(importlib.metadata.metadata("kestrel-agent").get_all("Requires-Dist") or [])


def _core() -> list[str]:
    # Extra-conditional entries render as "name>=x; extra == 'extra'".
    return [req for req in _requires_dist() if "; extra ==" not in req]


def _for_extra(name: str) -> list[str]:
    return [req for req in _requires_dist() if f"extra == '{name}'" in req]


class TestPackaging:
    def test_playwright_absent_from_core_deps(self):
        core = _core()
        assert not any(req.lower().startswith("playwright") for req in core), core

    def test_playwright_present_under_browser_extra(self):
        browser_extra = _for_extra("browser")
        assert any(req.lower().startswith("playwright") for req in browser_extra), _requires_dist()

    def test_dev_extra_includes_browser(self):
        dev = _for_extra("dev")
        assert any(req.lower().startswith("playwright") for req in dev), _requires_dist()


class TestLazyImportGuard:
    @staticmethod
    def _hide_playwright(monkeypatch):
        # Point sys.modules entries at None: any `import playwright` inside
        # the guard raises ImportError, exactly as in a no-extras venv.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    def test_guard_names_both_routes(self, monkeypatch):
        self._hide_playwright(monkeypatch)
        with pytest.raises(SystemExit, match=r"kestrel-agent\[browser\]"):
            browser.sync_playwright()

    def test_guard_names_agent_browsers_route(self, monkeypatch):
        self._hide_playwright(monkeypatch)
        with pytest.raises(SystemExit, match="coding agent"):
            browser.sync_playwright()

    def test_guard_returns_usable_context_manager_when_installed(self):
        # Dev installs carry the extra — the guard hands back a genuine
        # playwright context manager: enterable, and never the raw
        # sync_playwright factory function.
        guard_result = browser.sync_playwright()
        assert callable(getattr(guard_result, "__enter__", None))

    def test_guard_calls_the_stubbed_factory(self, monkeypatch):
        # Regression (dogfood 2026-11-09): the guard returned the imported
        # sync_playwright FUNCTION instead of calling it, so every caller's
        # `with sync_playwright() as p:` got a bare function — no __enter__.
        class FakeContextManager:
            def __enter__(self):
                return object()

            def __exit__(self, *args):
                return None

        def fake_sync_playwright():
            return FakeContextManager()

        fake_api = types.ModuleType("playwright.sync_api")
        fake_api.sync_playwright = fake_sync_playwright
        fake_root = types.ModuleType("playwright")
        fake_root.sync_api = fake_api

        monkeypatch.setitem(sys.modules, "playwright", fake_root)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)

        result = browser.sync_playwright()
        assert isinstance(result, FakeContextManager)
        with browser.sync_playwright() as p:
            assert p is not None


class TestLazySitesRouteThroughGuard:
    """Each direct-Playwright site imports the shared guard, so a missing
    extra surfaces as the guided SystemExit — never an ImportError traceback
    and never a module-level hard dependency."""

    def test_smartextract(self):
        from kestrel.discovery import smartextract

        # Playwright removal (spec art_VGFSMgRh, piece 2): smart-extract no
        # longer routes through the browser guard — HTTP plus host page-
        # captures only, so the module never imports the guard.
        assert "sync_playwright" not in vars(smartextract)

    def test_detail_enrichment(self):
        from kestrel.enrichment import detail

        assert detail.sync_playwright is browser.sync_playwright

    def test_pdf_rendering(self):
        from kestrel.scoring import pdf

        assert pdf.__dict__.get("sync_playwright") is None  # lazy, not module-level
        import inspect

        source = inspect.getsource(pdf.render_pdf)
        assert "from kestrel.browser import sync_playwright" in source
