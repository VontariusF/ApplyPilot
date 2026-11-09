"""Shared fixtures for the Kestrel test suites.

Two layers of isolation, so no test can touch a real ~/.kestrel:

1. KESTREL_DIR is redirected to a throwaway dir BEFORE the kestrel
   package is imported, so config.py binds APP_DIR (and every derived path)
   to it for tests that don't opt into the app_dir fixture.
2. Tests that need per-test isolation use `app_dir`, which repoints every
   Kestrel path constant at a fresh temp directory.

Kestrel resolves ~/.kestrel into module-level path constants at import
time; every consumer reads them as attributes at call time, so tests can
repoint the attributes at a temp directory instead of touching the real home
directory.
"""

import os
import tempfile
import threading

import pytest

os.environ.setdefault("KESTREL_DIR", tempfile.mkdtemp(prefix="kestrel-test-"))

from kestrel import config, database  # noqa: E402  (after the env redirect)
from kestrel.portal import automation  # noqa: E402  (after the env redirect)


@pytest.fixture(autouse=True)
def _scheduler_probe_reachable(monkeypatch):
    """Scheduled starts must not make real provider requests in tests.

    run_tick gates every scheduled start on the cached provider probe
    (P1-1); the default here is a reachable provider so scheduler tests
    exercise their start paths. Tests of the gate itself re-point
    automation._provider_probe at the real function (see
    test_portal_automation._REAL_PROBE_GATE).
    """
    monkeypatch.setattr(
        automation, "_provider_probe", lambda: ("reachable", ""))


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """Repoint every Kestrel path at a fresh temp app directory."""
    app = tmp_path / "kestrel"
    app.mkdir()
    paths = {
        "APP_DIR": app,
        "DB_PATH": app / "kestrel.db",
        "PROFILE_PATH": app / "profile.json",
        "PROFILE_MD_PATH": app / "profile.md",
        "RESUME_PATH": app / "resume.txt",
        "RESUME_PDF_PATH": app / "resume.pdf",
        "SEARCH_CONFIG_PATH": app / "searches.yaml",
        "ENV_PATH": app / ".env",
        "ONBOARDING_STATE_PATH": app / "onboarding-state.json",
        "TAILORED_DIR": app / "tailored_resumes",
        "COVER_LETTER_DIR": app / "cover_letters",
        "LOG_DIR": app / "logs",
        "CHROME_WORKER_DIR": app / "chrome-workers",
        "APPLY_WORKER_DIR": app / "apply-workers",
    }
    for name, path in paths.items():
        monkeypatch.setattr(config, name, path)
    # database.py binds DB_PATH via `from kestrel.config import DB_PATH`,
    # so it holds its own copy and must be repointed separately.
    monkeypatch.setattr(database, "DB_PATH", paths["DB_PATH"])
    return app


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point the database layer at a per-test SQLite file."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    yield db_path
    database.close_connection(str(db_path))


@pytest.fixture()
def barrier():
    """A reusable threading.Barrier factory."""
    def make(n: int) -> threading.Barrier:
        return threading.Barrier(n)
    return make


# ---------------------------------------------------------------------------
# LLM provider fake
# ---------------------------------------------------------------------------

class FakeLLMClient:
    """Scripted stand-in for llm.LLMClient at the provider boundary.

    Duck-types the client surface the codebase consumes (`chat`, `ask`).
    Queued responses are popped FIFO; once the queue is empty, `default`
    answers. Every chat() call is recorded in `calls` (the message lists),
    so tests can assert on what was actually sent to the provider.
    """

    def __init__(self, responses=(), default="ok"):
        self.responses = list(responses)
        self.default = default
        self.calls = []

    def chat(self, messages, temperature=0.0, max_tokens=4096):
        self.calls.append([dict(m) for m in messages])
        if self.responses:
            return self.responses.pop(0)
        return self.default

    def ask(self, prompt, **kwargs):
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def _chat_compat(self, messages, temperature=0.0, max_tokens=4096,
                     timeout=None):
        # Same provider boundary as chat(), named for the OpenAI-compat
        # endpoint — llm.probe_provider() reaches through it to time the
        # reachability check, so the fake must answer it too (raises
        # httpx errors to simulate an unreachable endpoint).
        return self.chat(messages, temperature=temperature,
                         max_tokens=max_tokens)

    def respond(self, *responses):
        """Queue replies for upcoming chat() calls (FIFO)."""
        self.responses.extend(responses)


@pytest.fixture()
def fake_llm(monkeypatch):
    """Swap the LLM provider for a scripted fake, at the provider level.

    The seam is `llm._instance` — the singleton every get_client() call
    returns — so ONE patch covers every import style: onboarding.LLMBackend
    calls llm.get_client() at call time, while the scoring/enrichment/
    discovery modules bind `from kestrel.llm import get_client` at
    import time and still reach the same singleton. The llm.get_client
    module attribute is swapped too, so a mid-test reset_client() (the
    settings-save path) cannot rebuild a real client on that path.

    Known edge: code that resets the singleton and then calls a *directly
    imported* get_client() would rebuild from the environment and fail
    loudly (tests run with no provider keys) — a visible error, never a
    real network call.
    """
    from kestrel import llm

    fake = FakeLLMClient()
    monkeypatch.setattr(llm, "_instance", fake)
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Portal fixtures

@pytest.fixture()
def portal_env(app_dir, tmp_path, monkeypatch):
    """Full portal isolation: repointed paths, clean env, fresh writers."""
    from kestrel.apply import eventlog

    # Settings saves live-refresh os.environ since S2-2 (that is the fix),
    # so snapshot around each portal test — a saved key must not leak into
    # other tests sharing the interpreter.
    env_snapshot = dict(os.environ)

    db_path = app_dir / "kestrel.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.delenv("KESTREL_EVENT_LOG", raising=False)
    eventlog.reset_writer_cache()
    yield app_dir
    database.close_connection(str(db_path))
    eventlog.reset_writer_cache()
    os.environ.clear()
    os.environ.update(env_snapshot)


@pytest.fixture()
def portal_client(portal_env, monkeypatch):
    """Authenticated portal test client against the isolated store."""
    from fastapi.testclient import TestClient

    from kestrel.portal.app import create_app

    # Speed up SSE idle-close in tests (generators read these at runtime).
    from kestrel.portal import app as portal_app_module
    monkeypatch.setattr(portal_app_module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(portal_app_module, "CHAT_POLL_SECONDS", 0.01)

    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers["x-portal-token"] = app.state.token
    yield client, app


def write_profile(profile: dict | None = None):
    """Seed a minimal profile.json (the onboarding-complete marker)."""
    import json

    from kestrel import config, onboarding

    if profile is None:
        profile, _ = onboarding.normalize_profile(
            {"_kestrel_profile": {"personal": {"full_name": "Test Person",
                                                  "email": "test@example.com"}},
             "_kestrel_search": {}})
    config.write_secret_text(config.PROFILE_PATH, json.dumps(profile))
    return profile


@pytest.fixture()
def seeded_profile(app_dir):
    """A store that has completed onboarding (dashboard reachable)."""
    return write_profile()

