"""Portal revamp tests (spec PR G).

Covers the new document surfaces and the re-skin plumbing:
- preview / download / version-history routes (auth-required, path-contained),
- the sidebar shell + shared base on every page (login included),
- explicit light/dark theme rendering for every page,
- the keywords/reasoning split of the stored score_reasoning blob,
- CSS-level responsive smoke (real-width checks run in the browser evidence).
"""

from applypilot import config, database
from applypilot.portal.queries import split_score_reasoning

from tests.test_portal_pages import portal_resume, seed_job

# ---------------------------------------------------------------------------
# Route auth (spec: preview + version endpoints are auth-required)
# ---------------------------------------------------------------------------


def test_preview_and_version_routes_require_token(portal_env):
    from fastapi.testclient import TestClient

    from applypilot.portal.app import create_app

    anon = TestClient(create_app(), base_url="http://127.0.0.1")
    routes = [
        "/jobs/preview?url=https://x.example/job&doc=resume",
        "/jobs/download?url=https://x.example/job&doc=resume",
        "/jobs/versions?url=https://x.example/job",
    ]
    for route in routes:
        r = anon.get(route)
        assert r.status_code == 401, f"{route} returned {r.status_code}"


# ---------------------------------------------------------------------------
# Preview route
# ---------------------------------------------------------------------------


def write_resume(text: str = "Vontarius Falls\nSenior Pwidget Engineer\n") -> object:
    path = portal_resume()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_preview_renders_document_text(portal_client):
    client, _ = portal_client
    write_resume()
    url = seed_job()
    r = client.get("/jobs/preview", params={"url": url, "doc": "resume"})
    assert r.status_code == 200
    assert "Senior Pwidget Engineer" in r.text
    assert "Download .txt" in r.text
    assert "side-nav" in r.text  # shares the sidebar shell


def test_preview_cover_letter(portal_client):
    client, _ = portal_client
    cover = config.APP_DIR / "cover_letters" / "cover.txt"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_text("Dear hiring team,", encoding="utf-8")
    url = seed_job(cover_letter_path=str(cover))
    r = client.get("/jobs/preview", params={"url": url, "doc": "cover"})
    assert r.status_code == 200
    assert "Dear hiring team," in r.text


def test_preview_missing_document_404(portal_client):
    client, _ = portal_client
    url = seed_job()  # path seeded, file never written
    r = client.get("/jobs/preview", params={"url": url, "doc": "resume"})
    assert r.status_code == 404
    assert "has been generated" in r.text


def test_preview_unknown_doc_kind_404(portal_client):
    client, _ = portal_client
    url = seed_job()
    r = client.get("/jobs/preview", params={"url": url, "doc": "bogus"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Download route
# ---------------------------------------------------------------------------


def test_download_serves_attachment(portal_client):
    client, _ = portal_client
    write_resume()
    url = seed_job()
    r = client.get("/jobs/download", params={"url": url, "doc": "resume"})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "resume.txt" in r.headers.get("content-disposition", "")


def test_download_pdf_sibling(portal_client):
    client, _ = portal_client
    path = write_resume()
    url = seed_job()
    # No PDF generated yet -> honest 404, never an on-demand conversion.
    r = client.get("/jobs/download", params={"url": url, "doc": "resume",
                                             "format": "pdf"})
    assert r.status_code == 404
    pdf = path.with_suffix(".pdf")
    pdf.write_bytes(b"%PDF-1.4 fake")
    r = client.get("/jobs/download", params={"url": url, "doc": "resume",
                                             "format": "pdf"})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF-1.4")


def test_download_refuses_path_outside_document_dirs(portal_client):
    """A tampered job row must not turn the route into arbitrary-file read."""
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/etc/hostname")
    r = client.get("/jobs/download", params={"url": url, "doc": "resume"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Version history route
# ---------------------------------------------------------------------------


def test_versions_page_lists_versions_with_state(portal_client):
    client, _ = portal_client
    path = write_resume()
    url = seed_job()
    conn = database.get_connection()
    vid = database.record_document_version(conn, url, database.KIND_RESUME, 1,
                                           str(path), "approved")
    conn.execute("UPDATE jobs SET applied_resume_version_id = ? WHERE url = ?",
                 (vid, url))
    conn.commit()
    r = client.get("/jobs/versions", params={"url": url})
    assert r.status_code == 200
    assert "Tailored resume" in r.text
    assert "v1" in r.text
    assert "current" in r.text
    assert "uploaded with application" in r.text


def test_versions_page_empty_state(portal_client):
    client, _ = portal_client
    url = seed_job()
    r = client.get("/jobs/versions", params={"url": url})
    assert r.status_code == 200
    assert "No tailored resume versions recorded yet" in r.text


def test_versions_page_unknown_job_404(portal_client):
    client, _ = portal_client
    r = client.get("/jobs/versions", params={"url": "https://nope.example/x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Theme rendering: every page, both themes
# ---------------------------------------------------------------------------

THEMED_PAGES = ["/jobs", "/runs", "/settings", "/onboarding", "/engine"]


def test_every_page_renders_in_both_themes(portal_client, seeded_profile):
    client, _ = portal_client
    pages = THEMED_PAGES + ["/"]  # dashboard needs the seeded profile
    for page in pages:
        for theme in ("light", "dark"):
            r = client.get(page, params={"theme": theme})
            assert r.status_code == 200, f"{page} ({theme}) failed"
            assert f'data-theme="{theme}"' in r.text, \
                f"{page} missing data-theme={theme}"


def test_login_page_renders_in_both_themes(portal_env):
    """The login form IS the 401 body (middleware-served, no GET route)."""
    from fastapi.testclient import TestClient

    from applypilot.portal.app import create_app

    anon = TestClient(create_app(), base_url="http://127.0.0.1")
    for theme in ("light", "dark"):
        r = anon.get("/login", params={"theme": theme})
        assert r.status_code == 401
        assert f'data-theme="{theme}"' in r.text


def test_unrecognized_theme_param_is_ignored(portal_env):
    """Only light/dark pin the theme; anything else falls back to OS preference."""
    from fastapi.testclient import TestClient

    from applypilot.portal.app import create_app

    anon = TestClient(create_app(), base_url="http://127.0.0.1")
    r = anon.get("/login", params={"theme": "neon"})
    assert r.status_code == 401
    assert "data-theme" not in r.text


# ---------------------------------------------------------------------------
# Sidebar shell
# ---------------------------------------------------------------------------


def test_login_uses_shared_sidebar_shell(portal_env):
    """The login form (401 body, middleware-served) extends the shared base."""
    from fastapi.testclient import TestClient

    from applypilot.portal.app import create_app

    anon = TestClient(create_app(), base_url="http://127.0.0.1")
    r = anon.get("/login")
    assert r.status_code == 401
    assert "side-nav" in r.text
    assert 'aria-label="Main"' in r.text


def test_base_shell_lists_all_sections(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.get("/jobs")
    for label in ("/jobs", "/", "/runs", "/onboarding", "/engine", "/settings"):
        assert f'href="{label}"' in r.text


def test_base_has_viewport_meta_and_responsive_breakpoints(portal_client):
    """Responsive smoke at the CSS level; real widths are checked in the
    browser evidence pass (375/768/1280)."""
    from applypilot.portal.app import PORTAL_DIR

    client, _ = portal_client
    r = client.get("/jobs")
    assert 'name="viewport"' in r.text
    css = (PORTAL_DIR / "static" / "portal.css").read_text(encoding="utf-8")
    for width in ("375", "768", "1280"):
        assert f"max-width: {width}px" in css, f"missing {width}px breakpoint"
    assert "prefers-color-scheme: dark" in css


# ---------------------------------------------------------------------------
# Keywords / reasoning split
# ---------------------------------------------------------------------------


def test_split_score_reasoning_separates_first_line():
    keywords, reasoning = split_score_reasoning("python, kubernetes\nSolid fit for the platform team.")
    assert keywords == "python, kubernetes"
    assert reasoning == "Solid fit for the platform team."


def test_split_score_reasoning_no_newline_is_all_reasoning():
    # Legacy rows stored plain reasoning without the keyword line.
    keywords, reasoning = split_score_reasoning("Strong match on pwidget skills.")
    assert keywords == ""
    assert reasoning == "Strong match on pwidget skills."


def test_split_score_reasoning_empty_inputs():
    assert split_score_reasoning(None) == ("", "")
    assert split_score_reasoning("") == ("", "")
    assert split_score_reasoning("\nsome reasoning") == ("", "some reasoning")


def test_job_detail_shows_keywords_and_reasoning_separately(portal_client):
    client, _ = portal_client
    url = seed_job(score_reasoning="python, kubernetes\nSolid fit for the platform team.")
    r = client.get("/jobs/detail", params={"url": url})
    assert r.status_code == 200
    assert "keyword-chips" in r.text
    assert '<span class="chip">python</span>' in r.text
    assert '<span class="chip">kubernetes</span>' in r.text
    # Exact paragraph content proves the keyword line did not leak into the
    # plain-language fit summary.
    assert ('<p class="fit-summary"><strong>Why it’s a 9/10:</strong> '
            'Solid fit for the platform team.</p>') in r.text
