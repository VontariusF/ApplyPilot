"""Jobs board filter tests (spec PR D).

Covers the spec-named verifying tests: filter-query tests per parameter,
route tests for filter combinations, and the empty-state render test —
plus the salary parser and the find-jobs-now onboarding handoff route.
"""

import asyncio
import sys
import time

import httpx

from applypilot.database import init_db
from applypilot.portal import queries


def seed_job(**overrides) -> str:
    conn = init_db()
    url = overrides.pop("url", "https://jobs.example/posting-1")
    row = {
        "url": url, "title": "Pwidget Engineer", "site": "ExampleBoard",
        "location": "Remote", "description": "short desc",
        "full_description": "full description text", "fit_score": 9,
        "salary": "", "apply_status": None,
        "discovered_at": "2026-11-01T12:00:00+00:00",
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
# Salary parser
# ---------------------------------------------------------------------------

def test_parse_salary_lower_bound():
    assert queries.parse_salary_lower_bound("$120,000 - $150,000") == 120_000
    assert queries.parse_salary_lower_bound("140k") == 140_000
    assert queries.parse_salary_lower_bound("USD 1.2m a year") == 1_200_000
    assert queries.parse_salary_lower_bound("$85/hr") == 85 * 2080
    assert queries.parse_salary_lower_bound("$5,000 per month") == 60_000
    assert queries.parse_salary_lower_bound("Competitive") is None
    assert queries.parse_salary_lower_bound("") is None
    assert queries.parse_salary_lower_bound(None) is None
    # Noise numbers (timestamps, "3 days ago") don't become salaries.
    assert queries.parse_salary_lower_bound("3 days ago") is None


# ---------------------------------------------------------------------------
# Filter-query tests per parameter
# ---------------------------------------------------------------------------

def test_filter_no_params_returns_everything(isolated_db):
    seed_job()
    seed_job(url="https://jobs.example/p2", fit_score=5, is_remote=0)
    jobs = queries.filtered_jobs()
    assert len(jobs) == 2


def test_filter_by_site(isolated_db):
    seed_job(site="ExampleBoard")
    seed_job(url="https://jobs.example/p2", site="OtherBoard")
    jobs = queries.filtered_jobs(site="OtherBoard")
    assert [j["url"] for j in jobs] == ["https://jobs.example/p2"]


def test_filter_by_location_substring(isolated_db):
    seed_job(location="Austin, TX")
    seed_job(url="https://jobs.example/p2", location="New York, NY")
    jobs = queries.filtered_jobs(location="austin")
    assert [j["url"] for j in jobs] == ["https://jobs.example/posting-1"]


def test_filter_location_ignores_like_wildcards(isolated_db):
    """S3: % and _ in the location box are literals, not LIKE wildcards."""
    seed_job(location="Bay 50% remote")
    seed_job(url="https://jobs.example/p2", location="Remote_OK hub")
    seed_job(url="https://jobs.example/p3", location="RemoteXK hub")
    # A bare "%" previously matched every row regardless of location.
    jobs = queries.filtered_jobs(location="%")
    assert [j["url"] for j in jobs] == ["https://jobs.example/posting-1"]
    # "_" previously behaved as a single-char wildcard, so "Remote_OK"
    # also matched "RemoteXK".
    jobs = queries.filtered_jobs(location="Remote_OK")
    assert [j["url"] for j in jobs] == ["https://jobs.example/p2"]
    # Ordinary substring matching still works alongside the escaping.
    jobs = queries.filtered_jobs(location="hub")
    assert [j["url"] for j in jobs] == ["https://jobs.example/p2",
                                        "https://jobs.example/p3"]


def test_filter_by_discovered_date_range(isolated_db):
    seed_job(discovered_at="2026-10-15T12:00:00+00:00")
    seed_job(url="https://jobs.example/p2", discovered_at="2026-11-02T09:00:00+00:00")
    jobs = queries.filtered_jobs(discovered_from="2026-11-01")
    assert [j["url"] for j in jobs] == ["https://jobs.example/p2"]
    jobs = queries.filtered_jobs(discovered_to="2026-10-31")
    assert [j["url"] for j in jobs] == ["https://jobs.example/posting-1"]
    both = queries.filtered_jobs(discovered_from="2026-10-01", discovered_to="2026-10-31")
    assert [j["url"] for j in both] == ["https://jobs.example/posting-1"]


def test_filter_by_apply_status(isolated_db):
    seed_job(apply_status="applied")
    seed_job(url="https://jobs.example/p2", apply_status="failed")
    jobs = queries.filtered_jobs(apply_status="failed")
    assert [j["url"] for j in jobs] == ["https://jobs.example/p2"]


def test_filter_by_min_salary_excludes_unparseable_only_while_active(isolated_db):
    seed_job(salary="$120,000 - $150,000")
    seed_job(url="https://jobs.example/p2", salary="$90k")
    seed_job(url="https://jobs.example/p3", salary="Competitive", is_remote=0)

    # Filter off: unparseable rows stay.
    assert len(queries.filtered_jobs()) == 3
    # Filter on: only parseable rows at or above the bound survive.
    jobs = queries.filtered_jobs(min_salary=100_000)
    assert [j["url"] for j in jobs] == ["https://jobs.example/posting-1"]
    jobs = queries.filtered_jobs(min_salary=80_000)
    assert {j["url"] for j in jobs} == {
        "https://jobs.example/posting-1", "https://jobs.example/p2"}


def test_filter_by_remote_uses_inferred_flag(isolated_db):
    seed_job(is_remote=1)
    seed_job(url="https://jobs.example/p2", is_remote=0)
    seed_job(url="https://jobs.example/p3", is_remote=None)

    assert {j["url"] for j in queries.filtered_jobs(remote="yes")} == {
        "https://jobs.example/posting-1"}
    assert {j["url"] for j in queries.filtered_jobs(remote="no")} == {
        "https://jobs.example/p2"}
    assert len(queries.filtered_jobs(remote="")) == 3


def test_filter_min_score(isolated_db):
    seed_job(fit_score=9)
    seed_job(url="https://jobs.example/p2", fit_score=5)
    jobs = queries.filtered_jobs(stage="scored", min_score=7)
    assert [j["url"] for j in jobs] == ["https://jobs.example/posting-1"]


def test_filter_min_score_ignores_unscored_stages(isolated_db):
    """Stage semantics match get_jobs_by_stage: no-score stages ignore the bound."""
    seed_job(url="https://jobs.example/failed", fit_score=None,
             score_error="LLM unavailable")
    jobs = queries.filtered_jobs(stage="score_failed", min_score=7)
    assert [j["url"] for j in jobs] == ["https://jobs.example/failed"]


def test_filter_limit(isolated_db):
    for i in range(3):
        seed_job(url=f"https://jobs.example/p{i}")
    assert len(queries.filtered_jobs(limit=2)) == 2
    assert len(queries.filtered_jobs(limit=0)) == 3


def test_filter_combination_narrows_jointly(isolated_db):
    seed_job(site="RemoteOK", is_remote=1, salary="$150k")
    seed_job(url="https://jobs.example/p2", site="RemoteOK", is_remote=1,
             salary="$60k")
    seed_job(url="https://jobs.example/p3", site="Dice", is_remote=0,
             salary="$150k")
    jobs = queries.filtered_jobs(site="RemoteOK", remote="yes", min_salary=100_000)
    assert [j["url"] for j in jobs] == ["https://jobs.example/posting-1"]


# ---------------------------------------------------------------------------
# Route tests for filter combinations
# ---------------------------------------------------------------------------

def test_jobs_route_renders_filter_combination(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(site="RemoteOK", is_remote=1, fit_score=9)
    seed_job(url="https://jobs.example/p2", site="Dice", is_remote=0, fit_score=9)
    r = client.get("/jobs", params={"stage": "scored", "min_score": 7,
                                    "site": "RemoteOK", "remote": "yes"})
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text
    assert "(inferred)" in r.text
    assert "RemoteOK" in r.text


def test_jobs_table_route_htmx_partial_with_combination(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(location="Austin, TX", fit_score=9)
    seed_job(url="https://jobs.example/p2", location="NYC", fit_score=9)
    r = client.get("/jobs/table", params={"stage": "scored", "location": "Austin",
                                          "min_score": 7})
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text
    assert "NYC" not in r.text


def test_jobs_route_tolerates_garbage_filters(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(fit_score=9)
    r = client.get("/jobs", params={"stage": "bogus", "min_salary": "abc",
                                    "remote": "sometimes"})
    assert r.status_code == 200
    assert "Pwidget Engineer" in r.text


# ---------------------------------------------------------------------------
# Empty-state render test
# ---------------------------------------------------------------------------

def test_empty_store_offers_discovery_run(portal_client, seeded_profile):
    client, _ = portal_client
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "No jobs found yet" in r.text
    assert "Find jobs now" in r.text
    assert 'action="/runs/start"' in r.text


def test_filtered_empty_keeps_filter_message(portal_client, seeded_profile):
    client, _ = portal_client
    seed_job(fit_score=9)
    r = client.get("/jobs", params={"stage": "scored", "min_score": 7,
                                    "location": "Nowhere"})
    assert r.status_code == 200
    assert "No jobs match this filter." in r.text
    assert "No jobs found yet" not in r.text


# ---------------------------------------------------------------------------
# Find-jobs-now handoff (spec journey step 5)
# ---------------------------------------------------------------------------

def test_onboarding_find_jobs_starts_pipeline_run(portal_client, seeded_profile,
                                                  monkeypatch):
    import time

    from applypilot.portal import runner as runner_mod

    client, _ = portal_client
    original = runner_mod.start_run

    def fake_start(kind, params, command=None, emit_jsonl=None):
        return original(kind, params, command=[sys.executable, "-c", "pass"],
                        emit_jsonl=False)

    monkeypatch.setattr(runner_mod, "start_run", fake_start)
    r = client.post("/onboarding/find-jobs", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs"

    runs = runner_mod.list_runs()
    assert len(runs) == 1
    assert runs[0]["kind"] == "pipeline"
    row = runs[0]
    deadline = time.time() + 5
    while time.time() < deadline and row["status"] == "running":
        row = runner_mod.get_run(row["id"])
        time.sleep(0.05)
    assert row["status"] == "completed"


def test_onboarding_done_card_offers_find_jobs(portal_client, seeded_profile,
                                               monkeypatch):
    """The confirm step's done card carries the handoff action."""
    from applypilot.portal import app as portal_app_module

    client, _ = portal_client

    class FakeState:
        phase = "done"
        messages = [{"role": "assistant", "content": "saved"}]
        profile_draft = None
        backend = "llm"
        error = None
        seq = 7  # render version stamped by _onboarding_context

    monkeypatch.setattr(portal_app_module.onboarding, "load_state", lambda: FakeState())
    r = client.get("/onboarding/chat")
    assert r.status_code == 200
    assert "Find jobs now" in r.text
    assert 'action="/onboarding/find-jobs"' in r.text


def test_empty_state_find_jobs_button_posts_runs_start(portal_client, seeded_profile,
                                                       monkeypatch):
    """The board's empty state routes through the same runs-start path."""
    from applypilot.portal import runner as runner_mod

    client, _ = portal_client
    kinds: list[str] = []

    def fake_start(kind, params, command=None, emit_jsonl=None):
        kinds.append(kind)
        return {"id": "fake", "status": "running"}

    monkeypatch.setattr(runner_mod, "start_run", fake_start)
    r = client.post("/runs/start", data={"kind": "pipeline"}, follow_redirects=False)
    assert r.status_code == 303
    assert kinds == ["pipeline"]



# ---------------------------------------------------------------------------
# Rescore route: off the event loop (sweep A S2-3)
# ---------------------------------------------------------------------------

def test_rescore_does_not_block_event_loop(portal_env, portal_client,
                                           seeded_profile, monkeypatch):
    """Sweep A S2-3 acceptance: a slow LLM rescore must not freeze the portal.

    The fake scoring call blocks for a known 1s window. A concurrent GET /
    must complete *inside* that window. With the bug present the scoring
    call runs directly on the event loop, so the GET cannot even start
    until the sleep finishes — its completion lands at >= the full blocking
    duration after scoring began.
    """
    from applypilot.portal import app as portal_app_module

    client, _ = portal_client
    app = client.app

    BLOCK_SECONDS = 1.0
    t: dict = {}

    def slow_scoring(url: str):
        t["start"] = time.perf_counter()
        time.sleep(BLOCK_SECONDS)

    monkeypatch.setattr(portal_app_module, "run_scoring", slow_scoring)

    async def scenario() -> tuple[int, int, float]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-portal-token": app.state.token}
        # Localhost base URL — the portal's host guard rejects other hosts.
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
            rescore = asyncio.create_task(
                ac.post("/jobs/rescore", data={"url": "https://jobs.example.com/123"},
                        headers=headers, follow_redirects=False))
            while "start" not in t:  # wait until scoring is actually running
                await asyncio.sleep(0.01)
            page = await ac.get("/", headers=headers)
            # Time from scoring start to GET completion, measured while the
            # event loop is live (before awaiting the rescore task).
            elapsed = time.perf_counter() - t["start"]
            rescore_response = await rescore
            return rescore_response.status_code, page.status_code, elapsed

    rescore_status, page_status, elapsed = asyncio.run(scenario())
    assert rescore_status == 303
    assert page_status == 200
    assert elapsed < 0.9 * BLOCK_SECONDS, \
        f"GET / finished {elapsed:.2f}s after scoring started — event loop was blocked"


def test_board_context_async_matches_sync(portal_client, seeded_profile):
    """Portal sweep P3-7: the board context build runs off the event loop
    via to_thread — the async wrapper returns exactly what the sync build
    does."""
    import asyncio

    from applypilot.portal.app import (_board_view, _jobs_board_context,
                                       _jobs_board_context_async)

    assert asyncio.run(_jobs_board_context_async(_board_view({}))) == (
        _jobs_board_context(_board_view({})))
