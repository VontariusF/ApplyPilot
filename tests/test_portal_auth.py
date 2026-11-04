"""Portal security tests: token gate, host check, 0600 token file.

Spec D acceptance: every route 401s without the token; the POST /login
form swaps the submitted token for a cookie; non-localhost Host headers
are refused (DNS-rebinding defense). Sweep F9 acceptance: the query
string is not a token source at all, and no access-log line ever
contains a token value.
"""

import logging
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
    "/jobs/preview?url=https://x.example/job&doc=resume",
    "/jobs/download?url=https://x.example/job&doc=resume",
    "/jobs/versions?url=https://x.example/job",
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


def test_query_token_is_not_a_token_source(anon_client, seeded_profile):
    """F9: a ?token= URL grants nothing and sets no cookie — the token
    must never ride in a URL (access logs, history, Referer)."""
    app = anon_client.app
    r = anon_client.get(f"/?token={app.state.token}", follow_redirects=False)
    assert r.status_code == 401
    assert auth_cookie(anon_client) is None


def test_access_log_never_contains_token_value(anon_client, seeded_profile, caplog):
    """F9 acceptance: no access-log line contains a token value —
    including stale ?token= links, which now 401 but still must not
    leak the (long-lived) token into uvicorn's access log. TestClient
    does not run uvicorn, so records are emitted through the
    uvicorn.access logger exactly as its access formatter would."""
    app = anon_client.app
    access_logger = logging.getLogger("uvicorn.access")
    with caplog.at_level("INFO", logger="uvicorn.access"):
        access_logger.info('%s - "%s %s HTTP/%s" %d',
                           "127.0.0.1", "GET", f"/?token={app.state.token}", "1.1", 401)
        access_logger.info('%s - "%s %s HTTP/%s" %d',
                           "127.0.0.1", "GET",
                           f"/jobs?stage=scored&token={app.state.token}&min=5", "1.1", 401)
        # An authenticated (cookie) request line must also stay clean.
        access_logger.info('%s - "%s %s HTTP/%s" %d',
                           "127.0.0.1", "GET", "/?stage=scored", "1.1", 200)
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert app.state.token not in logged
    assert "token=[REDACTED]" in logged


def test_redact_token_query_handles_query_and_anchor_params():
    from applypilot.portal.auth import redact_token_query

    line = '127.0.0.1 - "GET /?token=abc123 HTTP/1.1" 401'
    assert "abc123" not in redact_token_query(line)
    assert "token=[REDACTED]" in redact_token_query(line)
    mid = 'GET /jobs?stage=scored&token=sekret&min=5 HTTP/1.1'
    assert redact_token_query(mid) == 'GET /jobs?stage=scored&token=[REDACTED]&min=5 HTTP/1.1'
    # No token parameter: line passes through untouched.
    assert redact_token_query('GET /settings HTTP/1.1') == 'GET /settings HTTP/1.1'


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
    r = client.get("/", headers={"host": "evil.example.com",
                                 "x-portal-token": app.state.token})
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
