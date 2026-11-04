"""Jobs board navigation (spec Jobs panel rework, part 3): sort select,
server-side pagination, plain-language filter chips, dismissible
active-filter chips.

The 230-line filter matrix (test_jobs_board_filters.py) keeps passing
against the new presentation; this module pins the NEW controls.
"""

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.database import init_db

HX = {"HX-Request": "true"}


@pytest.fixture()
def start_spy(monkeypatch):
    """Replace runner.start_run (the real spawn) with a recording fake."""
    from applypilot.portal import runner

    calls: list[tuple[str, dict]] = []

    def fake_start(kind, params, **kwargs):
        calls.append((kind, dict(params)))
        return {"id": f"fake-{len(calls)}", "kind": kind, "status": "running"}

    monkeypatch.setattr(runner, "start_run", fake_start)
    return calls


def seed_job(url: str, title: str, **overrides) -> None:
    conn = init_db()
    row = {
        "url": url, "title": title, "site": "ExampleBoard",
        "location": "Austin", "description": "short desc",
        "full_description": "full description text", "fit_score": None,
        "salary": "", "apply_status": None,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "is_remote": 0,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    conn.commit()


def seed_five(conn_reset=False) -> list[str]:
    """Five scored jobs, staggered ages and salaries, for sort/pagination."""
    now = datetime.now(timezone.utc)
    specs = [
        ("https://jobs.example/1", "Alpha", 9, "$120,000/yr", 0),
        ("https://jobs.example/2", "Bravo", 8, "$90,000/yr", 1),
        ("https://jobs.example/3", "Charlie", 7, "$60,000/yr", 2),
        ("https://jobs.example/4", "Delta", 6, "Competitive", 3),
        ("https://jobs.example/5", "Echo", 5, "$150,000/yr", 4),
    ]
    for i, (url, title, score, salary, days_ago) in enumerate(specs):
        seed_job(url, title, fit_score=score, salary=salary,
                 discovered_at=(now - timedelta(days=days_ago)).isoformat())
    return [s[0] for s in specs]


def card_titles(html: str) -> list[str]:
    """Titles in board order, from the main card grid (the Dismissed
    section's grid is excluded — those rows left the view)."""
    import re

    grid = html.split('class="card-grid"', 1)[1]
    grid = grid.split('class="dismissed-section"', 1)[0]
    return re.findall(r'class="card-title"><a[^>]*>([^<]+)</a>', grid)


# ---------------------------------------------------------------------------
# Sort select: best (default) / newest / highest salary
# ---------------------------------------------------------------------------

def test_default_sort_is_best_match(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    html = client.get("/jobs/table?min_score=0").text
    assert card_titles(html) == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


def test_sort_newest_orders_by_discovery_date(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    html = client.get("/jobs/table?sort=newest&min_score=0").text
    assert card_titles(html) == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    # Newest first means oldest last: seed Echo oldest.
    html = client.get("/jobs/table?sort=newest&min_score=0&limit=2").text
    assert card_titles(html) == ["Alpha", "Bravo"]


def test_sort_salary_highest_first_unparseable_last(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    html = client.get("/jobs/table?sort=salary&min_score=0").text
    # 150k, 120k, 90k, 60k, then "Competitive" (unparseable) last.
    assert card_titles(html) == ["Echo", "Alpha", "Bravo", "Charlie", "Delta"]


def test_sort_garbage_falls_back_to_best(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    html = client.get("/jobs/table?sort=nonsense&min_score=0").text
    assert card_titles(html) == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


# ---------------------------------------------------------------------------
# Server-side pagination: Prev/Next + count via filtered_jobs offset
# ---------------------------------------------------------------------------

def test_pagination_slices_server_side(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    base = "/jobs/table?min_score=0&limit=2"
    page1 = client.get(base + "&page=1").text
    page2 = client.get(base + "&page=2").text
    page3 = client.get(base + "&page=3").text
    assert card_titles(page1) == ["Alpha", "Bravo"]
    assert card_titles(page2) == ["Charlie", "Delta"]
    assert card_titles(page3) == ["Echo"]
    assert "5 jobs in this view" in page1
    assert "Page 2 of 3" in page2


def test_pagination_links_and_bounds(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    page1 = client.get("/jobs/table?min_score=0&limit=2&page=1").text
    page3 = client.get("/jobs/table?min_score=0&limit=2&page=3").text
    # Page 1: Previous is disabled; Page 3: Next is disabled.
    assert "page-disabled" in page1
    assert 'href="/jobs?' in page1  # Next is a real link
    assert "page-disabled" in page3
    # Beyond-the-end page clamps to the last page.
    clamped = client.get("/jobs/table?min_score=0&limit=2&page=99").text
    assert "Page 3 of 3" in clamped
    assert card_titles(clamped) == ["Echo"]


def test_verb_preserves_page_view(portal_client, seeded_profile, start_spy):
    client, _ = portal_client
    seed_five()
    r = client.post("/jobs/dismiss", data={"url": "https://jobs.example/3",
                                           "page": "2", "limit": "2",
                                           "min_score": "0"},
                    headers=HX)
    assert r.status_code == 200
    # Charlie (page 2's first row) is gone; the view stays on page 2 —
    # now clamped to 2 pages after the dismissal.
    assert "Charlie" not in card_titles(r.text)
    assert "Page 2 of 2" in r.text


# ---------------------------------------------------------------------------
# Plain-language chips mapping to existing query surfaces
# ---------------------------------------------------------------------------

def test_board_renders_five_chips(portal_client, seeded_profile):
    client, _ = portal_client
    html = client.get("/jobs").text
    for label in ("New", "Scored", "Ready to apply", "Applied",
                  "Needs attention"):
        assert f">{label}</a>" in html
    assert "chip=new" in html


def test_chip_new_spans_discovery_stages(portal_client, seeded_profile):
    client, _ = portal_client
    now = datetime.now(timezone.utc).isoformat()
    seed_job("https://jobs.example/fresh", "Fresh", fit_score=None,
             discovered_at=now)                      # discovered
    seed_job("https://jobs.example/enriched", "Enriched", fit_score=None,
             full_description="long text", discovered_at=now)  # enriched
    seed_job("https://jobs.example/scored", "Scored", fit_score=8,
             discovered_at=now)                      # scored, NOT new
    html = client.get("/jobs/table?chip=new").text
    titles = card_titles(html)
    assert "Fresh" in titles and "Enriched" in titles
    assert "Scored" not in titles


def test_chip_attention_unions_score_failures_and_failed_applies(
        portal_client, seeded_profile):
    client, _ = portal_client
    now = datetime.now(timezone.utc).isoformat()
    seed_job("https://jobs.example/sf", "ScoreFailed", score_error="boom",
             discovered_at=now)
    seed_job("https://jobs.example/af", "ApplyFailed", fit_score=8,
             apply_status="failed", discovered_at=now)
    seed_job("https://jobs.example/ok", "Healthy", fit_score=8,
             discovered_at=now)
    html = client.get("/jobs/table?chip=attention").text
    titles = card_titles(html)
    assert "ScoreFailed" in titles and "ApplyFailed" in titles
    assert "Healthy" not in titles


def test_chip_ready_and_applied(portal_client, seeded_profile):
    client, _ = portal_client
    now = datetime.now(timezone.utc).isoformat()
    seed_job("https://jobs.example/ready", "ReadyJob", fit_score=8,
             tailored_resume_path="/x/resume.txt", discovered_at=now)
    seed_job("https://jobs.example/applied", "AppliedJob", fit_score=8,
             tailored_resume_path="/x/resume.txt", apply_status="applied",
             applied_at=now, discovered_at=now)
    ready = card_titles(client.get("/jobs/table?chip=ready").text)
    applied = card_titles(client.get("/jobs/table?chip=applied").text)
    # The tailored stage is not disjoint from applied: a job keeps its
    # tailored documents after applying, so "Ready to apply" (mapped to
    # the tailored stage per spec) still lists applied jobs.
    assert ready == ["ReadyJob", "AppliedJob"]
    assert applied == ["AppliedJob"]


def test_chip_survives_verb_post(portal_client, seeded_profile, start_spy):
    client, _ = portal_client
    now = datetime.now(timezone.utc).isoformat()
    seed_job("https://jobs.example/new1", "NewJob", fit_score=None,
             discovered_at=now)
    r = client.post("/jobs/dismiss",
                    data={"url": "https://jobs.example/new1", "chip": "new"},
                    headers=HX)
    assert r.status_code == 200
    # Response re-renders the SAME chip view (empty now, chips row intact).
    assert "chip=new" in r.text


# ---------------------------------------------------------------------------
# Surviving filters, jargon removed; dismissible active-filter chips
# ---------------------------------------------------------------------------

def test_filter_form_jargon_removed(portal_client, seeded_profile):
    client, _ = portal_client
    html = client.get("/jobs").text
    assert "Pay at least" in html
    assert "Match score at least" in html
    assert "Work setup" in html
    # The old jargon is gone.
    assert "Fit ≥" not in html
    assert "Salary ≥" not in html
    assert "<label>Stage" not in html
    assert 'name="limit"' not in html  # page size is not a user filter anymore
    # The swap target matches the board's id (the old #jobs-table is gone).
    assert 'hx-target="#jobs-board"' in html


def test_active_filters_render_as_dismissible_chips(portal_client, seeded_profile):
    client, _ = portal_client
    html = client.get("/jobs?site=ExampleBoard&location=Austin&min_salary=80000"
                      "&min_score=9&remote=yes&discovered_from=2026-01-01").text
    for label in ("Board: ExampleBoard", "Location: Austin",
                  "Pay at least $80,000", "Match 9/10 or better",
                  "Remote only", "Found since 2026-01-01"):
        assert label in html
    # Removing the site filter drops exactly that param.
    board_chip = [line for line in html.splitlines()
                  if "Board: ExampleBoard" in line][0]
    assert "site=" not in board_chip
    assert "location=Austin" in board_chip  # other filters survive


def test_default_view_shows_no_min_score_chip(portal_client, seeded_profile):
    client, _ = portal_client
    html = client.get("/jobs").text
    assert "Match 7/10 or better" not in html  # 7 is the baseline, not a choice


def test_filtered_empty_still_offers_clear_filters(portal_client, seeded_profile):
    client, _ = portal_client
    seed_five()
    html = client.get("/jobs?location=Atlantis").text
    assert "No jobs match this filter." in html
    assert 'href="/jobs"' in html
