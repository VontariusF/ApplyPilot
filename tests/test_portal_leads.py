"""Portal /leads board (hiring-leads spec art_u0YGUqpk §6, criterion A6).

Route tests in the pattern of the /jobs route tests: render with seeded
leads, the mark transition, refusals, and the vocabulary single-source
guarantee (the portal imports the shared LEAD_* constants — no local
copy for statuses to drift from).
"""

import inspect

import pytest

from kestrel import database
from kestrel.database import init_db
from kestrel.portal import queries

HX = {"HX-Request": "true"}


def seed_company(key: str = "acme-robotics.com", display: str = "Acme Robotics",
                 fit: float | None = 9.0) -> None:
    """Seed a company through the real path — a scored job, then the
    mirror refresh. Inserting into company_identity directly no longer
    survives: refresh drops mirror rows with no backing jobs (sweep
    art_Izev3UNN finding 6), so a fake row would vanish at the next
    init_db and the board join would lose its display name."""
    conn = init_db()
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, company, company_domain, "
        "fit_score, discovered_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"https://jobs.{key}/posting/seed", f"Engineer at {display}",
         display, key, fit, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    database.refresh_company_identity(conn)


def seed_lead(**overrides) -> str:
    conn = init_db()
    kwargs = {
        "profile_url": "https://www.linkedin.com/in/jane-hirer",
        "full_name": "Jane Hirer",
        "title": "Head of Talent",
        "company_key": "acme-robotics.com",
        "source": database.LEAD_SOURCE_SERP,
        "evidence_url": "https://www.acme-robotics.com/team",
    }
    kwargs.update(overrides)
    profile_url = kwargs.pop("profile_url")
    assert database.upsert_hiring_lead(conn, profile_url=profile_url, **kwargs)
    return profile_url


# ---------------------------------------------------------------------------
# GET /leads — board rendering
# ---------------------------------------------------------------------------

def test_leads_route_renders_lead_cards(portal_client, seeded_profile):
    client, _ = portal_client
    seed_company(fit=9.0)
    seed_lead()
    r = client.get("/leads")
    assert r.status_code == 200
    assert 'id="leads-board"' in r.text
    assert 'class="card-grid"' in r.text
    assert "Jane Hirer" in r.text
    assert "Head of Talent" in r.text
    assert "Acme Robotics" in r.text  # joined display name, not the raw key
    assert "9/10" in r.text  # company fit pill
    assert "Open on LinkedIn" in r.text
    assert 'href="https://www.linkedin.com/in/jane-hirer"' in r.text
    assert 'rel="noopener noreferrer"' in r.text  # plain href, the user's click
    assert "badge discovered" in r.text
    assert "serp" in r.text
    assert "evidence" in r.text


def test_leads_route_empty_state(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.get("/leads")
    assert r.status_code == 200
    assert "No hiring leads yet" in r.text


def test_leads_route_status_chip_filters(portal_client, seeded_profile):
    client, _ = portal_client
    seed_company()
    seed_lead()
    seed_lead(profile_url="https://www.linkedin.com/in/sam-founder",
              full_name="Sam Founder", title="Founder",
              last_status=database.LEAD_STATUS_CONNECTED)
    connected = client.get("/leads?status=connected")
    assert connected.status_code == 200
    assert "Sam Founder" in connected.text
    assert "Jane Hirer" not in connected.text
    # The chips carry the shared vocabulary: the three states + all.
    all_leads = client.get("/leads")
    for chip in ("Discovered", "Connected", "Dismissed", "All"):
        assert chip in all_leads.text


def test_leads_route_matched_role_absent_when_no_taxonomy_hit(
        portal_client, seeded_profile):
    client, _ = portal_client
    seed_company()
    seed_lead(title="Chief People Officer")
    r = client.get("/leads")
    assert "Jane Hirer" in r.text
    # No invented badge — honest absence (the pure helper returns None).
    assert queries.matched_role("Chief People Officer") is None


def test_matched_role_whole_word_matches_the_attribution_gate():
    """The badge must use the attribution gate's whole-word rule: a
    fused token like 'Salesforce Engineer' is not a 'Sales' hit, while
    a whole-word term still badges regardless of surrounding
    punctuation (sweep art_Izev3UNN suggestion 6)."""
    assert queries.matched_role(
        "Salesforce Engineer", ("Sales",)) is None
    assert queries.matched_role(
        "Talented Intern", ("Talent",)) is None
    assert queries.matched_role(
        "Talent Partner - Acme", ("Talent Partner",)) == "Talent Partner"
    assert queries.matched_role(
        "Talent@Acme", ("Talent",)) == "Talent"


def test_leads_table_route_renders_board_partial(portal_client, seeded_profile):
    client, _ = portal_client
    seed_company()
    seed_lead()
    r = client.get("/leads/table")
    assert r.status_code == 200
    assert 'id="leads-board"' in r.text
    assert "Jane Hirer" in r.text


def test_leads_route_in_side_nav(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.get("/leads")
    assert 'href="/leads"' in r.text


# ---------------------------------------------------------------------------
# POST /leads/mark — the one write, user-driven
# ---------------------------------------------------------------------------

def test_leads_mark_dismissed_transitions_and_sets_decided_at(
        portal_client, seeded_profile):
    client, _ = portal_client
    seed_company()
    profile_url = seed_lead()
    r = client.post("/leads/mark",
                    data={"profile_url": profile_url,
                          "status": database.LEAD_STATUS_DISMISSED},
                    headers=HX)
    assert r.status_code == 200
    assert "dismissed" in r.text  # re-rendered board partial
    row = init_db().execute(
        "SELECT last_status, decided_at FROM hiring_leads WHERE profile_url = ?",
        (profile_url,)).fetchone()
    assert row["last_status"] == database.LEAD_STATUS_DISMISSED
    assert row["decided_at"] is not None
    # The marked lead leaves the default discovered view.
    board = client.get("/leads")
    assert "Jane Hirer" not in board.text


def test_leads_mark_connected_non_htmx_redirects(portal_client, seeded_profile):
    client, _ = portal_client
    seed_company()
    profile_url = seed_lead()
    r = client.post("/leads/mark",
                    data={"profile_url": profile_url,
                          "status": database.LEAD_STATUS_CONNECTED},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/leads"
    row = init_db().execute(
        "SELECT last_status FROM hiring_leads WHERE profile_url = ?",
        (profile_url,)).fetchone()
    assert row["last_status"] == database.LEAD_STATUS_CONNECTED


def test_leads_mark_refuses_non_decision_statuses(portal_client, seeded_profile):
    client, _ = portal_client
    profile_url = seed_lead()
    # "discovered" is ingestion, never a decision — refused by the route.
    r = client.post("/leads/mark",
                    data={"profile_url": profile_url,
                          "status": database.LEAD_STATUS_DISCOVERED})
    assert r.status_code == 400
    r = client.post("/leads/mark",
                    data={"profile_url": profile_url, "status": "hired"})
    assert r.status_code == 400
    # Nothing was written by either refusal.
    row = init_db().execute(
        "SELECT last_status, decided_at FROM hiring_leads "
        "WHERE profile_url = ?", (profile_url,)).fetchone()
    assert row["last_status"] == database.LEAD_STATUS_DISCOVERED
    assert row["decided_at"] is None


def test_leads_mark_unknown_lead_is_404(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.post("/leads/mark",
                    data={"profile_url": "https://www.linkedin.com/in/ghost",
                          "status": database.LEAD_STATUS_DISMISSED})
    assert r.status_code == 404


def test_leads_mark_requires_profile_url(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.post("/leads/mark",
                    data={"status": database.LEAD_STATUS_DISMISSED})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Vocabulary single source (spec A6: the portal reads the shared constants)
# ---------------------------------------------------------------------------

def test_portal_uses_shared_lead_vocabulary():
    """The portal derives its mark/validation sets from database.LEAD_*,
    so no surface can drift from the adapters' vocabulary."""
    assert queries.LEAD_MARK_STATUSES == (database.LEAD_STATUS_DISMISSED,
                                          database.LEAD_STATUS_CONNECTED)
    views = [value for _, value in queries.LEAD_STATUS_VIEWS]
    assert views[:3] == list(database.LEAD_STATUSES)
    assert views[3] == ""  # "all" is a view, not a status


def test_portal_leads_routes_never_write_status_literals(portal_client):
    """The leads routes hold no status string literals: every status
    comparison rides the shared constants. (The card templates' hidden
    inputs are display copy pinned by the tests above.)"""
    _, app = portal_client
    mark_src = ""
    for route in app.routes:
        if route.path == "/leads/mark":
            mark_src = inspect.getsource(route.endpoint)
    assert mark_src  # the route exists under this app
    for status in database.LEAD_STATUSES:
        assert f'"{status}"' not in mark_src
        assert f"'{status}'" not in mark_src


def test_leads_page_links_never_automate_contact(portal_client, seeded_profile):
    """Guardrail, observable from the render: the only outbound surface is
    the person's own click — a plain href, no fetch/no send verbs."""
    client, app = portal_client
    seed_company()
    seed_lead()
    r = client.get("/leads")
    assert 'hx-post="/leads/mark"' in r.text  # only write is the status mark
    assert "form" in r.text
    # No route on the leads board could send anything: the app defines
    # exactly the three leads routes.
    leads_routes = [route.path for route in app.routes
                    if route.path.startswith("/leads")]
    assert sorted(leads_routes) == ["/leads", "/leads/mark", "/leads/table"]


@pytest.mark.parametrize("status", ["connected", "dismissed"])
def test_leads_mark_round_trips_each_decision(portal_client, seeded_profile,
                                              status):
    client, _ = portal_client
    profile_url = seed_lead(
        title="Recruiting Manager",
        profile_url=f"https://www.linkedin.com/in/lead-{status}")
    r = client.post("/leads/mark",
                    data={"profile_url": profile_url, "status": status},
                    headers=HX)
    assert r.status_code == 200
    row = init_db().execute(
        "SELECT last_status FROM hiring_leads WHERE profile_url = ?",
        (profile_url,)).fetchone()
    assert row["last_status"] == status
