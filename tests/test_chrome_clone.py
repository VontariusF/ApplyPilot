"""S5: the worker Chrome profile clone must exclude credential stores.

Saved passwords (Login Data), session cookies (Cookies, incl. the newer
Default/Network/Cookies location), and autofill data (Web Data) must never
ride along -- including their SQLite journal/WAL siblings and including the
worker-to-worker clone path.
"""

import pytest

from applypilot import config
from applypilot.apply import chrome

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
