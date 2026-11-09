"""Jobs board verbs (spec Jobs panel rework, part 2): dismiss/undo,
tailor-now, apply-now — route behavior, failure pins, busy states.

Failure cases pinned per spec:
- apply-now without tailored documents refuses and the card offers Tailor now;
- dismissing the last visible job leaves a sane empty state with the
  Dismissed section (and Undo) still reachable.
"""

import json
from datetime import datetime, timezone

import pytest

from kestrel import database
from kestrel.database import get_connection, init_db
from kestrel.portal import runner

HX = {"HX-Request": "true"}


@pytest.fixture()
def start_spy(monkeypatch):
    """Replace runner.start_run (the real spawn) with a recording fake."""
    calls: list[tuple[str, dict]] = []

    def fake_start(kind, params, **kwargs):
        calls.append((kind, dict(params)))
        return {"id": f"fake-{len(calls)}", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    return calls


def seed_job(**overrides) -> str:
    conn = init_db()
    url = overrides.pop("url", "https://jobs.example/posting-1")
    row = {
        "url": url, "title": "Pwidget Engineer", "site": "ExampleBoard",
        "location": "Remote", "description": "short desc",
        "full_description": "full description text", "fit_score": 9,
        "salary": "", "apply_status": None,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "is_remote": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    conn.commit()
    return url


# ---------------------------------------------------------------------------
# Dismiss / undo
# ---------------------------------------------------------------------------

def test_dismiss_moves_job_to_dismissed_section(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job()
    r = client.post("/jobs/dismiss", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert 'id="jobs-board"' in r.text
    # Left the default view; the collapsed section carries it with Undo.
    assert 'class="card-grid"' in r.text
    assert r.text.count("Pwidget Engineer") == 1  # only in the dismissed section
    assert "Dismissed (1)" in r.text
    assert "Undo" in r.text
    # The dismissed card offers no apply/tailor verbs.
    assert "Tailor now" not in r.text
    assert "Apply now" not in r.text
    row = get_connection().execute(
        "SELECT dismissed_at FROM jobs WHERE url = ?", (url,)).fetchone()
    assert row["dismissed_at"] is not None


def test_dismiss_stores_timestamp_and_filtered_jobs_excludes(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job()
    client.post("/jobs/dismiss", data={"url": url})
    from kestrel.portal import queries

    assert queries.filtered_jobs(stage="discovered", min_score=None, limit=0) == []
    only = queries.filtered_jobs(stage="discovered", dismissed="only",
                                 min_score=None, limit=0)
    assert [j["url"] for j in only] == [url]


def test_dismiss_non_htx_redirects(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job()
    r = client.post("/jobs/dismiss", data={"url": url}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs"


def test_dismiss_validates_input(portal_client, seeded_profile):
    client, _ = portal_client
    assert client.post("/jobs/dismiss", data={}).status_code == 400
    assert client.post("/jobs/dismiss", data={"url": "https://nope.example/x"},
                       headers=HX).status_code == 404


def test_undismiss_restores_job_to_board(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job()
    client.post("/jobs/dismiss", data={"url": url})
    r = client.post("/jobs/undismiss", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text
    assert "Dismissed (" not in r.text
    row = get_connection().execute(
        "SELECT dismissed_at FROM jobs WHERE url = ?", (url,)).fetchone()
    assert row["dismissed_at"] is None


def test_last_visible_job_dismissal_keeps_sane_empty_state(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job()
    r = client.post("/jobs/dismiss", data={"url": url}, headers=HX)
    assert r.status_code == 200
    # The board explains why it is empty — never a bare grid — and the
    # dismissed job stays reachable for Undo.
    assert "Nothing on the board right now." in r.text
    assert "No jobs found yet" not in r.text  # store is not empty, so no first-run CTA
    assert "Dismissed (1)" in r.text


# ---------------------------------------------------------------------------
# Tailor now / Apply now
# ---------------------------------------------------------------------------

def test_tailor_now_htx_stays_on_board(portal_client, seeded_profile, start_spy):
    client, _ = portal_client
    url = seed_job()
    r = client.post("/jobs/tailor-now", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert 'id="jobs-board"' in r.text
    assert "Tailoring started" in r.text
    assert start_spy == [("tailor", {"url": url, "min_score": "0"})]


def test_apply_now_without_documents_offers_tailor_now(portal_client, seeded_profile,
                                                       start_spy):
    client, _ = portal_client
    url = seed_job()  # no tailored_resume_path
    # Card: Tailor now, never Apply now.
    board = client.get("/jobs")
    assert "Tailor now" in board.text
    assert "Apply now" not in board.text
    # Route: refuses to submit, without starting any run.
    r = client.post("/jobs/apply-now", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert "no tailored documents" in r.text
    assert start_spy == []
    non_hx = client.post("/jobs/apply-now", data={"url": url},
                         follow_redirects=False)
    assert non_hx.status_code == 303
    assert "error=" in non_hx.headers["location"]
    assert start_spy == []


def test_apply_now_with_documents_starts_one_job_run(portal_client, seeded_profile,
                                                     start_spy):
    client, _ = portal_client
    url = seed_job(tailored_resume_path="/x/resume.txt",
                   cover_letter_path="/x/cover.txt")
    # Card renders the confirmation flow, not a direct submit.
    board = client.get("/jobs")
    assert "Apply now" in board.text
    assert "Confirm &amp; apply" in board.text
    # The confirmation card names the documents it will submit.
    assert "tailored resume" in board.text
    assert "cover letter" in board.text
    r = client.post("/jobs/apply-now", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert "Applying now" in r.text
    assert start_spy == [("apply", {"url": url})]


def test_apply_now_rejects_unknown_job(portal_client, seeded_profile, start_spy):
    client, _ = portal_client
    r = client.post("/jobs/apply-now", data={"url": "https://nope.example/x"},
                    headers=HX)
    assert r.status_code == 404
    assert start_spy == []


# ---------------------------------------------------------------------------
# Busy state: a running single-job run shows a spinner on its card
# ---------------------------------------------------------------------------

def test_busy_card_shows_spinner_and_hides_actions(portal_client, seeded_profile,
                                                   monkeypatch):
    from kestrel.portal import app as portal_app

    url = seed_job(tailored_resume_path="/x/resume.txt")
    monkeypatch.setattr(portal_app, "_busy_verbs", lambda: {url: "apply"})
    client, _ = portal_client
    r = client.get("/jobs")
    assert "Submitting your application…" in r.text
    assert 'class="spinner"' in r.text
    # No action buttons while busy.
    assert "Apply now" not in r.text
    assert "Dismiss" not in r.text


def test_busy_card_tailor_verb_label(portal_client, seeded_profile, monkeypatch):
    from kestrel.portal import app as portal_app

    url = seed_job()
    monkeypatch.setattr(portal_app, "_busy_verbs", lambda: {url: "tailor"})
    client, _ = portal_client
    r = client.get("/jobs")
    assert "Tailoring documents…" in r.text


# ---------------------------------------------------------------------------
# Rescore inline feedback (replaces the silent block)
# ---------------------------------------------------------------------------

def test_rescore_htx_renders_inline_feedback(portal_client, seeded_profile,
                                             monkeypatch):
    from kestrel.portal import app as portal_app

    url = seed_job()
    monkeypatch.setattr(portal_app, "run_scoring", lambda **kwargs: None)
    client, _ = portal_client
    r = client.post("/jobs/rescore", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert "Score updated just now." in r.text
    assert 'id="fit-assessment"' in r.text


# ---------------------------------------------------------------------------
# Board SSE feed
# ---------------------------------------------------------------------------

def test_board_snapshot_reflects_run_and_store_changes(portal_client, seeded_profile,
                                                       start_spy):
    from kestrel.portal.app import _board_snapshot

    before = _board_snapshot()
    url = seed_job(url="https://jobs.example/extra")
    after_job = _board_snapshot()
    assert before != after_job  # store growth is visible
    # A dismissal changes board state too, so every open board refreshes.
    database.set_job_dismissed(get_connection(), url, dismissed=True)
    assert _board_snapshot() != after_job


def test_busy_verbs_reads_registry_params(portal_client, seeded_profile,
                                          monkeypatch):
    from kestrel.portal import app as portal_app

    url = seed_job()
    monkeypatch.setattr(portal_app.runner, "running_runs",
                        lambda: [{"id": "r1", "kind": "tailor",
                                  "params": json.dumps({"url": url}),
                                  "status": "running"}])
    assert portal_app._busy_verbs() == {url: "tailor"}


def test_rescore_noop_renders_honest_error(portal_client, seeded_profile,
                                           monkeypatch):
    """Portal sweep P3-3: a no-op rescore (no full description to assess,
    or every scoring call failed) must not render the success state."""
    from kestrel.portal import app as portal_app

    url = seed_job()
    monkeypatch.setattr(portal_app, "run_scoring",
                        lambda **kwargs: {"scored": 0, "errors": 1})
    client, _ = portal_client
    r = client.post("/jobs/rescore", data={"url": url}, headers=HX)
    assert r.status_code == 200
    assert "Rescore did not score this job" in r.text
    assert "Score updated just now." not in r.text
