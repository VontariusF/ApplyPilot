"""S5: the worker Chrome profile clone must exclude credential stores.

Saved passwords (Login Data), session cookies (Cookies, incl. the newer
Default/Network/Cookies location), and autofill data (Web Data) must never
ride along -- including their SQLite journal/WAL siblings and including the
worker-to-worker clone path.
"""

import pytest

from kestrel import config
from kestrel.apply import chrome

CREDENTIAL_NAMES = {
    "Login Data", "Login Data-journal", "Login Data-wal",
    "Cookies", "Cookies-journal", "Cookies-wal",
    "Web Data", "Web Data-journal", "Web Data-wal",
}


def make_source_profile(root):
    """Fabricate a Chrome User Data dir whose credential stores must not clone."""
    default = root / "Default"
    network = default / "Network"
    network.mkdir(parents=True)
    (root / "Local State").write_text("{}")
    (default / "Preferences").write_text("{}")
    (default / "Bookmarks").write_text("[]")
    for name in CREDENTIAL_NAMES:
        (default / name).write_text("credentials")
    (network / "Cookies").write_text("session cookies")
    (network / "TransportSecurity").write_text("{}")


@pytest.fixture
def isolated_clone(tmp_path, monkeypatch):
    source = tmp_path / "chrome-source"
    make_source_profile(source)
    workers = tmp_path / "workers"
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", workers)
    monkeypatch.setattr(config, "get_chrome_user_data", lambda: source)
    return workers


def test_clone_excludes_credential_stores(isolated_clone):
    profile_dir = chrome.setup_worker_profile(worker_id=0)

    copied = [p.name for p in profile_dir.rglob("*") if p.is_file()]
    leaked = sorted(name for name in CREDENTIAL_NAMES if name in copied)
    assert not leaked, f"credential stores cloned: {leaked}"
    # Non-credential profile data still copies, at every level.
    assert (profile_dir / "Default" / "Preferences").exists()
    assert (profile_dir / "Default" / "Bookmarks").exists()
    assert (profile_dir / "Default" / "Network" / "TransportSecurity").exists()


def test_worker_to_worker_clone_excludes_credentials(isolated_clone):
    chrome.setup_worker_profile(worker_id=0)
    # Simulate an older worker profile that still carries a credential store.
    legacy_cookies = isolated_clone / "worker-0" / "Default" / "Cookies"
    legacy_cookies.write_text("session cookies")

    profile_dir = chrome.setup_worker_profile(worker_id=1)

    assert not (profile_dir / "Default" / "Cookies").exists()
    assert (profile_dir / "Default" / "Preferences").exists()


def test_existing_worker_profile_is_reused(isolated_clone):
    first = chrome.setup_worker_profile(worker_id=0)
    (first / "Default" / "Preferences").write_text('{"marker": true}')

    second = chrome.setup_worker_profile(worker_id=0)

    assert second == first
    assert (second / "Default" / "Preferences").read_text() == '{"marker": true}'


@pytest.fixture
def fresh_machine(tmp_path, monkeypatch):
    """A machine where Chrome has never run: no user-data dir at all (F-3)."""
    workers = tmp_path / "workers"
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", workers)
    monkeypatch.setattr(config, "get_chrome_user_data",
                        lambda: tmp_path / "no-chrome-here")
    return workers


def test_first_run_with_no_chrome_profile_starts_fresh(fresh_machine):
    """F-3: on a machine where Chrome has never run, setup must create an
    empty worker profile instead of crashing on the missing source dir —
    the old code raised FileNotFoundError from source.iterdir() and the
    launcher burned the job's apply attempt on that environment error."""
    profile_dir = chrome.setup_worker_profile(worker_id=0)

    assert profile_dir.is_dir()
    assert profile_dir.parent == fresh_machine


def test_fresh_bootstrap_then_worker_clone_still_works(fresh_machine):
    """After the first-run bootstrap, the next worker clones worker-0 (now
    real) instead of falling back to the still-missing user profile."""
    first = chrome.setup_worker_profile(worker_id=0)
    (first / "Default").mkdir()
    (first / "Default" / "Preferences").write_text("{}")

    second = chrome.setup_worker_profile(worker_id=1)

    assert (second / "Default" / "Preferences").exists()
