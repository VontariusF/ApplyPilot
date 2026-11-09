"""Jobs board card grid (spec Jobs panel rework, part 1).

The table becomes a card grid: salary, relative age, score pill, remote
tooltip. The 230-line filter matrix (test_jobs_board_filters.py) keeps
passing against the new presentation; these tests pin what the cards show.
"""

from datetime import datetime, timedelta, timezone

from kestrel.database import init_db


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
# Relative age (the card's "how old is this listing" line)
# ---------------------------------------------------------------------------

def test_relative_age_buckets():
    from kestrel.portal.app import _relative_age

    now = datetime.now(timezone.utc)
    assert _relative_age(None) == ""
    assert _relative_age("not a date") == ""
    assert _relative_age(now.isoformat()) == "just now"
    assert _relative_age((now - timedelta(seconds=90)).isoformat()) == "1 minute ago"
    assert _relative_age((now - timedelta(hours=3)).isoformat()) == "3 hours ago"
    assert _relative_age((now - timedelta(days=2)).isoformat()) == "2 days ago"
    assert _relative_age((now - timedelta(days=21)).isoformat()) == "3 weeks ago"
    assert _relative_age((now - timedelta(days=90)).isoformat()) == "3 months ago"


# ---------------------------------------------------------------------------
# Card grid rendering
# ---------------------------------------------------------------------------

def test_jobs_route_renders_card_grid(portal_client, seeded_profile):
    client, _ = portal_client
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    seed_job(salary="$120,000 - $150,000", discovered_at=old)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert 'class="card-grid"' in r.text
    assert 'class="job-card"' in r.text
    assert "$120,000 - $150,000" in r.text
    assert "9/10" in r.text  # score pill on the card
    assert "days ago" in r.text  # relative age, not a raw timestamp
    # The remote annotation is a tooltip, not visible copy.
    assert "(inferred)" in r.text
    assert ">Yes</span> <span class=\"dim\">(inferred)</span>" not in r.text


def test_jobs_route_cards_omit_empty_salary_with_placeholder(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(salary="")
    r = client.get("/jobs")
    assert "Pay not listed" in r.text


def test_jobs_table_route_renders_board_partial(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job()
    seed_job(url="https://jobs.example/p2", location="NYC")
    r = client.get("/jobs/table")
    assert r.status_code == 200
    assert 'id="jobs-board"' in r.text
    assert "Pwidget Engineer" in r.text
    # No location filter, so the NYC seed renders as a second card.
    assert "NYC" in r.text


# ---------------------------------------------------------------------------
# Empty states
# ---------------------------------------------------------------------------

def test_empty_store_leads_with_find_jobs(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "No jobs found yet" in r.text
    assert "Find jobs now" in r.text
    assert 'action="/runs/start"' in r.text


def test_filtered_empty_offers_clear_filters(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job()
    r = client.get("/jobs", params={"location": "Nowhere"})
    assert r.status_code == 200
    assert "No jobs match this filter." in r.text
    assert "Clear filters" in r.text
    assert "Find jobs now" not in r.text


# ---------------------------------------------------------------------------
# Detail view: plain-language fit summary + prominent salary
# ---------------------------------------------------------------------------

def test_detail_fit_summary_is_plain_language(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job(score_reasoning="pwidget, crm\nStrong match on pwidget skills.")
    r = client.get("/jobs/detail", params={"url": url})
    assert r.status_code == 200
    assert "Why it’s a 9/10:" in r.text
    assert "Strong match on pwidget skills." in r.text
    assert "<pre class=\"reasoning\">" not in r.text


def test_detail_fit_summary_uses_an_for_eight(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job(fit_score=8, score_reasoning="kw\nreasoning text")
    r = client.get("/jobs/detail", params={"url": url})
    assert "Why it’s an 8/10:" in r.text


def test_detail_shows_salary_prominently(portal_client, seeded_profile):
    client, _ = portal_client
    url = seed_job(salary="$120,000 - $150,000")
    r = client.get("/jobs/detail", params={"url": url})
    assert 'class="salary-line"' in r.text
    assert "$120,000 - $150,000" in r.text
