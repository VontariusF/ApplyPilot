"""Portal security tests: token gate, host check, 0600 token file.

Spec D acceptance: every route 401s without the token; the launch-token
query parameter converts to a cookie; non-localhost Host headers are
refused (DNS-rebinding defense).
"""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from applypilot.portal.app import create_app

# Every GET route the portal serves (plus a bogus run id path).
ROUTES = [
    "/",
    "/jobs",
    "/jobs/table",
    "/jobs/detail?url=https://x.example/job",
    "/runs",
    "/runs/deadbeef/log",
    "/runs/deadbeef/stream",
    "/runs/deadbeef/logstream",
    "/settings",
    "/static/htmx.min.js",
]


@pytest.fixture()
def anon_client(portal_env):
    """Unauthenticated client (no token header/cookie)."""
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    yield client


def test_every_route_401s_without_token(anon_client):
    for route in ROUTES:
        r = anon_client.get(route)
        assert r.status_code == 401, f"{route} returned {r.status_code}"


def test_login_with_wrong_token_401(anon_client):
    r = anon_client.post("/login", data={"token": "wrong-token"})
    assert r.status_code == 401


def test_login_with_correct_token_sets_cookie(anon_client):
    app = anon_client.app
    r = anon_client.post("/login", data={"token": app.state.token},
                         follow_redirects=False)
    assert r.status_code == 303
    assert auth_cookie(anon_client)


def auth_cookie(client) -> str | None:
    jar = client.cookies
    return jar.get("portal_token")


def test_query_token_sets_cookie_and_grants_access(anon_client, seeded_profile):
    app = anon_client.app
    r = anon_client.get(f"/?token={app.state.token}", follow_redirects=False)
    assert r.status_code == 200
    assert auth_cookie(anon_client)
    # Subsequent request rides the cookie alone.
    r2 = anon_client.get("/settings")
    assert r2.status_code == 200


def test_header_token_grants_access(anon_client):
    r = anon_client.get("/", headers={"x-portal-token": anon_client.app.state.token})
    assert r.status_code == 200


def test_bearer_token_grants_access(anon_client):
    r = anon_client.get(
        "/", headers={"authorization": f"Bearer {anon_client.app.state.token}"}
    )
    assert r.status_code == 200


def test_non_localhost_host_refused(portal_env):
    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get(f"/?token={app.state.token}", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_token_file_created_0600(portal_env):
    create_app()
    token_file = portal_env / "portal_token"
    assert token_file.exists()
    if os.name == "posix":
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_token_reused_not_regenerated(portal_env):
    app1 = create_app()
    app2 = create_app()
    assert app1.state.token == app2.state.token
