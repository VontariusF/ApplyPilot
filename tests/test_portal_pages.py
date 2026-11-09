"""Portal page tests: dashboard stats, jobs list/detail/mark, runs page.

The pages read the same SQLite store the CLI writes; these tests seed rows
directly and assert the rendered output and the manual-override effects.
"""

from datetime import datetime, timezone

from kestrel.database import get_connection, init_db


def seed_job(**overrides) -> str:
    conn = init_db()
    url = overrides.pop("url", "https://jobs.example/posting-1")
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "url": url, "title": "Pwidget Engineer", "site": "ExampleBoard",
        "location": "Remote", "description": "short desc",
        "full_description": "full description text", "fit_score": 9,
        "score_reasoning": "Strong match on pwidget skills.",
        "discovered_at": now, "tailored_resume_path": str(portal_resume()),
        "application_url": url,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    conn.commit()
    return url


def portal_resume() -> object:
    from kestrel import config
    return config.APP_DIR / "tailored_resumes" / "resume.txt"


def test_dashboard_renders_stage_counters(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(applied_at=datetime.now(timezone.utc).isoformat(),
             apply_status="applied")
    r = client.get("/")
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text  # recent outcomes feed
    assert "Score distribution" in r.text


def test_dashboard_empty_state(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.get("/")
    assert r.status_code == 200
    assert "No apply outcomes yet" in r.text


def test_dashboard_redirects_to_onboarding_without_profile(portal_client):
    """Fresh store: onboarding is the front door (spec journey step 1)."""
    client, _ = portal_client
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding"


def test_jobs_page_filters_by_stage(portal_client):
    client, _ = portal_client
    seed_job()
    seed_job(url="https://jobs.example/low", fit_score=4)
    r = client.get("/jobs", params={"stage": "scored", "min_score": 7})
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text
    assert "Low fit" not in r.text


def test_jobs_detail_shows_reasoning_and_docs(portal_client):
    client, _ = portal_client
    url = seed_job()
    r = client.get("/jobs/detail", params={"url": url})
    assert r.status_code == 200
    assert "Strong match on pwidget skills." in r.text
    assert "Mark applied" in r.text
    assert "Mark failed" in r.text
    # Full description is rendered (autoescaped by jinja).
    assert "full description text" in r.text


def test_jobs_detail_missing_404(portal_client):
    client, _ = portal_client
    r = client.get("/jobs/detail", params={"url": "https://nope.example/x"})
    assert r.status_code == 404


def test_mark_applied_updates_db(portal_client):
    client, _ = portal_client
    url = seed_job()
    r = client.post("/jobs/mark", data={"url": url, "status": "applied"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = get_connection().execute(
        "SELECT applied_at, apply_status FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row["apply_status"] == "applied"
    assert row["applied_at"] is not None


def test_mark_failed_records_reason(portal_client):
    client, _ = portal_client
    url = seed_job()
    client.post("/jobs/mark", data={
        "url": url, "status": "failed", "reason": "login wall"
    })
    row = get_connection().execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row["apply_status"] == "failed"
    assert row["apply_error"] == "login wall"


def test_mark_rejects_bad_status(portal_client):
    client, _ = portal_client
    r = client.post("/jobs/mark", data={"url": "https://x.example", "status": "hacked"})
    assert r.status_code == 400


def test_jobs_mark_redirect_encodes_url(portal_client):
    """A job URL containing '&' and '=' must survive the redirect intact.

    The redirect Location is percent-encoded, so following it lands on the
    job detail page instead of a truncated-url 404 (sweep A finding 4).
    """
    client, _ = portal_client
    url = seed_job(url="https://jobs.lever.co/acme/abc?team=platform&mode=board")
    r = client.post("/jobs/mark", data={"url": url, "status": "applied"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text


def test_jobs_redirect_encodes_url_location(portal_client, monkeypatch):
    """mark/regenerate/rescore build an encoded /jobs/detail?url= target."""
    from urllib.parse import quote

    import kestrel.portal.app as portal_app

    client, _ = portal_client
    url = seed_job(url="https://jobs.lever.co/acme/abc?team=platform&mode=board")
    encoded = f"/jobs/detail?url={quote(url, safe='')}"

    monkeypatch.setattr(portal_app, "run_scoring", lambda url=None: None)

    r = client.post("/jobs/mark", data={"url": url, "status": "applied"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == encoded

    r = client.post("/jobs/regenerate", data={"url": url}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == encoded

    r = client.post("/jobs/rescore", data={"url": url}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == encoded


def test_regenerate_refuses_while_apply_in_progress(portal_client):
    """P2-4: resetting tailoring mid-run would unlink the PDF that the
    active apply run is about to upload — the same plain-language refusal
    as /jobs/apply-now, not a silent document wipe."""
    client, _ = portal_client
    url = seed_job(apply_status="in_progress",
                   tailored_resume_path="/x/resume.txt")

    r = client.post("/jobs/regenerate", data={"url": url},
                    headers={"HX-Request": "true"})

    assert r.status_code == 409
    assert "applying to this job right now" in r.text
    row = get_connection().execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row["tailored_resume_path"] == "/x/resume.txt"  # untouched


def test_reset_failed_action(portal_client):
    client, _ = portal_client
    url = seed_job()
    get_connection().execute(
        "UPDATE jobs SET apply_status = 'failed', apply_error = 'x' WHERE url = ?",
        (url,),
    )
    get_connection().commit()
    r = client.post("/jobs/reset-failed", follow_redirects=False)
    assert r.status_code == 303
    row = get_connection().execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row["apply_status"] is None


def test_runs_page_renders_history(portal_client):
    import sys

    client, _ = portal_client
    from kestrel.portal import runner

    runner.start_run("pipeline", {}, command=[sys.executable, "-c", "pass"],
                     emit_jsonl=False)
    r = client.get("/runs")
    assert r.status_code == 200
    # The raw run forms collapsed into the Automation preset cards; the
    # Runs page is history only.
    assert "Start pipeline run" not in r.text
    assert "Start apply run" not in r.text
    assert "Run history" in r.text


def test_runs_start_route_spawns_and_records(portal_client, monkeypatch):
    """The route itself: form -> registry row + subprocess (fake entry point)."""
    import sys
    import time

    from kestrel.portal import runner as runner_mod

    client, _ = portal_client
    original = runner_mod.start_run

    def fake_start(kind, params, command=None, emit_jsonl=None):
        # Never launch real stages from a test: swap in the fake entry point.
        return original(kind, params, command=[sys.executable, "-c", "pass"],
                        emit_jsonl=False)

    monkeypatch.setattr(runner_mod, "start_run", fake_start)
    r = client.post("/runs/start", data={"kind": "pipeline", "min_score": "7"},
                    follow_redirects=False)
    assert r.status_code == 303

    runs = runner_mod.list_runs()
    assert len(runs) == 1
    assert runs[0]["kind"] == "pipeline"
    row = runs[0]
    deadline = time.time() + 5
    while time.time() < deadline and row["status"] == "running":
        row = runner_mod.get_run(row["id"])
        time.sleep(0.05)
    assert row["status"] == "completed"


def test_runs_start_rejects_bad_params(portal_client):
    client, _ = portal_client
    r = client.post("/runs/start", data={"kind": "pipeline", "workers": "999"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_runs_start_refused_while_run_active(portal_client):
    """F-2: a manual start against an active run is refused with a plain
    error redirect — a double click must not spawn a concurrent run that
    together blows past the daily cap."""
    client, _ = portal_client
    from urllib.parse import unquote

    from kestrel.portal import runner as runner_mod

    runner_mod.init_registry()
    get_connection().execute(
        "INSERT INTO runs (id, kind, params, status, pid, started_at) "
        "VALUES ('active-1', 'apply', '{}', 'running', 1, "
        "'2026-11-04T00:00:00+00:00')")
    get_connection().commit()

    r = client.post("/runs/start", data={"kind": "pipeline"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "already active" in unquote(r.headers["location"])
    assert len(runner_mod.list_runs()) == 1  # nothing new spawned


def test_log_page_renders_redacted_backfill(portal_client, portal_env):
    import json as _json

    from kestrel import config

    client, _ = portal_client
    config.ENV_PATH.write_text("GEMINI_API_KEY=secret-gemini-key\n", encoding="utf-8")
    log_path = portal_env / "runs" / "page.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("used secret-gemini-key during startup\n", encoding="utf-8")

    from kestrel.portal import runner as runner_mod

    runner_mod.init_registry()
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO runs (id, kind, params, status, pid, pgid, started_at, log_path, event_path) "
        "VALUES ('logp1', 'pipeline', ?, 'completed', 1, 1, '2026-01-01', ?, NULL)",
        (_json.dumps({}), str(log_path)),
    )
    conn.commit()

    r = client.get("/runs/logp1/log")
    assert r.status_code == 200
    assert "secret-gemini-key" not in r.text
    assert "[REDACTED]" in r.text


# ---------------------------------------------------------------------------
# Distinct captcha status — board rendering (spec art_tBtaZIWB)
# ---------------------------------------------------------------------------


def test_captcha_renders_its_own_board_badge(portal_client):
    client, _ = portal_client
    seed_job(url="https://jobs.example/captcha",
             apply_status="captcha", apply_error="captcha")
    board = client.get("/jobs")
    assert board.status_code == 200
    assert 'badge captcha' in board.text
    assert "captcha" in board.text


def test_captcha_appears_in_the_status_filter_dropdown(portal_client):
    """The dropdown is data-driven over distinct_apply_statuses(); a captcha
    row must surface as a filterable status (verify, don't assume)."""
    client, _ = portal_client
    seed_job(url="https://jobs.example/captcha",
             apply_status="captcha", apply_error="captcha")
    board = client.get("/jobs")
    assert board.status_code == 200
    assert '<option value="captcha"' in board.text


def test_captcha_filter_selects_captcha_rows(portal_client):
    client, _ = portal_client
    seed_job(url="https://jobs.example/captcha",
             apply_status="captcha", apply_error="captcha")
    seed_job(url="https://jobs.example/failed",
             apply_status="failed", apply_error="engine died")
    board = client.get("/jobs", params={"apply_status": "captcha"})
    assert board.status_code == 200
    assert "https://jobs.example/captcha" in board.text
    assert "https://jobs.example/failed" not in board.text


def test_captcha_not_counted_by_the_failed_chip(portal_client):
    """The Needs-attention chip unions score_failed and apply_failed; a
    captcha block is a distinct terminal status, not an apply failure."""
    client, _ = portal_client
    seed_job(url="https://jobs.example/captcha",
             apply_status="captcha", apply_error="captcha")
    seed_job(url="https://jobs.example/failed",
             apply_status="failed", apply_error="engine died")
    board = client.get("/jobs", params={"chip": "attention"})
    assert board.status_code == 200
    assert "https://jobs.example/failed" in board.text
    assert "https://jobs.example/captcha" not in board.text

