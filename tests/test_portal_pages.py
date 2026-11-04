"""Portal page tests: dashboard stats, jobs list/detail/mark, runs page.

The pages read the same SQLite store the CLI writes; these tests seed rows
directly and assert the rendered output and the manual-override effects.
"""

from datetime import datetime, timezone

from applypilot.database import get_connection, init_db


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
    from applypilot import config
    return config.APP_DIR / "tailored_resumes" / "resume.txt"


def test_dashboard_renders_stage_counters(portal_client):
    client, _ = portal_client
    seed_job(applied_at=datetime.now(timezone.utc).isoformat(),
             apply_status="applied")
    r = client.get("/")
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text  # recent outcomes feed
    assert "Score distribution" in r.text


def test_dashboard_empty_state(portal_client):
    client, _ = portal_client
    r = client.get("/")
    assert r.status_code == 200
    assert "No apply outcomes yet" in r.text


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
    from applypilot.portal import runner

    runner.start_run("pipeline", {}, command=[sys.executable, "-c", "pass"],
                     emit_jsonl=False)
    r = client.get("/runs")
    assert r.status_code == 200
    assert "Start pipeline run" in r.text
    assert "Start apply run" in r.text
    assert "pipeline" in r.text


def test_runs_start_route_spawns_and_records(portal_client, monkeypatch):
    """The route itself: form -> registry row + subprocess (fake entry point)."""
    import sys
    import time

    from applypilot.portal import runner as runner_mod

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


def test_log_page_renders_redacted_backfill(portal_client, portal_env):
    import json as _json

    from applypilot import config

    client, _ = portal_client
    config.ENV_PATH.write_text("GEMINI_API_KEY=secret-gemini-key\n", encoding="utf-8")
    log_path = portal_env / "runs" / "page.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("used secret-gemini-key during startup\n", encoding="utf-8")

    from applypilot.portal import runner as runner_mod

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
