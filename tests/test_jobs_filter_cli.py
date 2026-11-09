"""Integration tests for deep-filtering Layer 2 (spec art_we4AUxEy).

The composable `jobs filter` surface: `filter_jobs()` query builder AND-
composition, every CLI predicate alone and combined, sort orders, limit,
the --json dimension contract, --urls pipe output, and typed usage errors
for unknown seniority tokens, malformed durations, and bad sort keys.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from kestrel import database
from kestrel.cli import app
from kestrel.database import FilterPredicates, filter_jobs

runner = CliRunner()

RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
OLD = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()

# resume.txt content written into the seeded app dir; j1's score is bound
# to this hash (fresh) — every other scored row is stale against it.
RESUME_TEXT = "engineer with nine years of platform experience\n"


def _hash(text: str) -> str:
    from kestrel.scoring.scorer import resume_sha256
    return resume_sha256(text)


def _seed(conn, url, *, title=None, location=None, site=None,
          is_remote=None, date_posted=None, salary_min=None,
          salary_max=None, salary_currency=None, salary_interval=None,
          seniority=None, fit_score=None, scored_resume_hash=None,
          discovered_at="2026-11-01T00:00:00+00:00",
          full_description=None, salary=None):
    conn.execute(
        "INSERT INTO jobs (url, title, location, site, is_remote, date_posted,"
        " salary_min, salary_max, salary_currency, salary_interval, seniority,"
        " fit_score, scored_resume_hash, discovered_at, full_description, salary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, title, location, site, is_remote, date_posted, salary_min,
         salary_max, salary_currency, salary_interval, seniority, fit_score,
         scored_resume_hash, discovered_at, full_description, salary),
    )


@pytest.fixture()
def seeded(app_dir):
    """A store with known rows across every filterable dimension."""
    database.init_db()
    conn = database.get_connection()
    fresh_hash = _hash(RESUME_TEXT)
    _seed(conn, "https://jobs.example/j1", title="Senior Platform Engineer",
          location="Mexico City", site="LinkedIn", is_remote=1,
          date_posted=RECENT, salary_min=180000, salary_max=200000,
          salary_currency="USD", salary_interval="yearly",
          seniority="senior", fit_score=8, scored_resume_hash=fresh_hash,
          full_description="Build the kubernetes platform.")
    _seed(conn, "https://jobs.example/j2", title="Staff Engineer",
          location="Austin", site="Indeed", is_remote=1,
          date_posted=OLD, salary_min=190000, salary_max=190000,
          salary_currency="USD", salary_interval="yearly",
          seniority="staff", fit_score=9, scored_resume_hash="deadbeef",
          full_description="Requires security clearance.")
    _seed(conn, "https://jobs.example/j3", title="Backend Engineer",
          location="Berlin", site="LinkedIn", is_remote=0,
          date_posted=None, salary_min=110000, salary_max=120000,
          salary_currency=None, salary_interval=None,
          seniority=None, fit_score=7, scored_resume_hash=None,
          full_description="Python services.")
    _seed(conn, "https://jobs.example/j4", title="Junior Analyst",
          location="Mexico City", site="Glassdoor", is_remote=1,
          date_posted=RECENT, salary_min=55000, salary_max=60000,
          salary_currency="EUR", salary_interval="yearly",
          seniority="junior", fit_score=5, scored_resume_hash="feedface",
          full_description="Entry level data analysis.")
    _seed(conn, "https://jobs.example/j5", title="Platform Engineering Lead",
          location=None, site="HiringCafe", is_remote=1,
          date_posted=RECENT, seniority="lead",
          full_description="Own the kubernetes fleet.")
    conn.commit()
    (app_dir / "resume.txt").write_text(RESUME_TEXT, encoding="utf-8")
    return {"fresh_hash": fresh_hash, "urls": [
        "https://jobs.example/j1", "https://jobs.example/j2",
        "https://jobs.example/j3", "https://jobs.example/j4",
        "https://jobs.example/j5",
    ]}


def _urls(rows):
    return [row["url"] for row in rows]


def _db_predicates(**kwargs) -> FilterPredicates:
    return FilterPredicates(**kwargs)


# ---------------------------------------------------------------------------
# filter_jobs(): each predicate alone, composition, sort, limit
# ---------------------------------------------------------------------------

def test_zero_predicates_lists_everything(seeded):
    rows = filter_jobs(_db_predicates())
    assert len(rows) == 5
    # Every row carries the derived stale flag.
    assert all(isinstance(row["stale"], bool) for row in rows)


def test_posted_within_excludes_old_and_unknown_dates(seeded):
    rows = filter_jobs(_db_predicates(posted_within_days=7))
    assert _urls(rows) == ["https://jobs.example/j1",
                           "https://jobs.example/j4",
                           "https://jobs.example/j5"]


def test_location_substring_case_insensitive(seeded):
    rows = filter_jobs(_db_predicates(location="mexico"))
    assert _urls(rows) == ["https://jobs.example/j1",
                           "https://jobs.example/j4"]


def test_remote_and_on_site_are_opposites(seeded):
    assert _urls(filter_jobs(_db_predicates(remote=True))) == [
        "https://jobs.example/j1", "https://jobs.example/j2",
        "https://jobs.example/j4", "https://jobs.example/j5",
    ]
    assert _urls(filter_jobs(_db_predicates(remote=False))) == [
        "https://jobs.example/j3",
    ]


def test_seniority_any_of_matches_any_token(seeded):
    rows = filter_jobs(_db_predicates(seniority=["STAFF", "junior"]))
    assert _urls(rows) == ["https://jobs.example/j2",
                           "https://jobs.example/j4"]


def test_min_salary_currency_matching(seeded):
    # USD threshold at 65k: the USD rows match, EUR is excluded, and the
    # unknown-currency row stays (unknown != foreign).
    rows = filter_jobs(_db_predicates(min_salary=65000, currency="USD"))
    assert _urls(rows) == ["https://jobs.example/j1", "https://jobs.example/j2",
                           "https://jobs.example/j3"]
    # Currency-matched to EUR: only the EUR row.
    assert _urls(filter_jobs(_db_predicates(min_salary=150000, currency="EUR"))) == []
    # No currency: every row reaching the threshold (j4's 60k max falls
    # short of 70k).
    assert _urls(filter_jobs(_db_predicates(min_salary=70000))) == [
        "https://jobs.example/j1", "https://jobs.example/j2",
        "https://jobs.example/j3",
    ]


def test_score_band_is_inclusive_on_both_ends(seeded):
    rows = filter_jobs(_db_predicates(min_score=7, max_score=8))
    assert _urls(rows) == ["https://jobs.example/j1", "https://jobs.example/j3"]


def test_site_any_of_case_insensitive(seeded):
    rows = filter_jobs(_db_predicates(sites=["linkedin", "indeed"]))
    assert _urls(rows) == ["https://jobs.example/j1", "https://jobs.example/j2",
                           "https://jobs.example/j3"]


def test_keyword_any_of_over_title_and_description(seeded):
    rows = filter_jobs(_db_predicates(keywords=["kubernetes"]))
    assert _urls(rows) == ["https://jobs.example/j1", "https://jobs.example/j5"]


def test_exclude_keyword_removes_rows(seeded):
    rows = filter_jobs(_db_predicates(exclude_keywords=["clearance"]))
    assert "https://jobs.example/j2" not in _urls(rows)
    assert len(rows) == 4


def test_predicates_and_together(seeded):
    rows = filter_jobs(_db_predicates(
        posted_within_days=7, seniority=["senior", "lead"],
        min_salary=150000, currency="USD", min_score=6,
        exclude_keywords=["clearance"]))
    assert _urls(rows) == ["https://jobs.example/j1"]


def test_stale_only_keeps_unbound_and_mismatched_scores(seeded):
    # j1's score is bound to the current resume hash (fresh); j2/j4 hold
    # other hashes, j3 was never bound, j5 was never scored — all stale.
    rows = filter_jobs(_db_predicates(stale_only=True),
                       current_resume_hash=seeded["fresh_hash"])
    assert _urls(rows) == ["https://jobs.example/j2", "https://jobs.example/j3",
                           "https://jobs.example/j4", "https://jobs.example/j5"]


def test_stale_flag_marks_the_fresh_row(seeded):
    rows = filter_jobs(_db_predicates(),
                       current_resume_hash=seeded["fresh_hash"])
    by_url = {row["url"]: row["stale"] for row in rows}
    assert by_url["https://jobs.example/j1"] is False
    assert by_url["https://jobs.example/j2"] is True


def test_sort_score_desc_nulls_last(seeded):
    rows = filter_jobs(_db_predicates(sort="score"))
    assert _urls(rows)[:3] == ["https://jobs.example/j2",
                               "https://jobs.example/j1",
                               "https://jobs.example/j3"]
    assert rows[-1]["url"] == "https://jobs.example/j5"  # unscored last


def test_sort_asc_reverses(seeded):
    rows = filter_jobs(_db_predicates(sort="score", ascending=True, limit=2))
    assert _urls(rows) == ["https://jobs.example/j4",
                           "https://jobs.example/j3"]


def test_sort_date_nulls_last(seeded):
    rows = filter_jobs(_db_predicates(sort="date"))
    assert rows[-1]["url"] == "https://jobs.example/j3"  # NULL date last
    assert rows[0]["url"] == "https://jobs.example/j1"   # url tiebreak


def test_sort_salary_uses_annualized_top_of_range(seeded):
    rows = filter_jobs(_db_predicates(sort="salary"))
    assert _urls(rows)[:2] == ["https://jobs.example/j1",
                               "https://jobs.example/j2"]


def test_limit_caps_rows(seeded):
    rows = filter_jobs(_db_predicates(limit=2))
    assert len(rows) == 2


def test_unknown_sort_key_raises_valueerror(seeded):
    with pytest.raises(ValueError, match="Unknown sort key"):
        filter_jobs(_db_predicates(sort="pay"))


# ---------------------------------------------------------------------------
# CLI: --urls, --json contract, typed usage errors
# ---------------------------------------------------------------------------

def test_cli_urls_pipe_output(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--posted-within", "7d",
                                 "--seniority", "senior,lead",
                                 "--min-salary", "150000", "--min-score", "6",
                                 "--urls"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["https://jobs.example/j1"]


def test_cli_json_reports_filterable_dimensions(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--posted-within", "7d",
                                 "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 3
    assert len(payload["jobs"]) == 3
    expected_keys = {
        "url", "title", "location", "site", "discovered_at", "date_posted",
        "salary_min", "salary_max", "salary_currency", "salary_interval",
        "seniority", "fit_score", "stale",
        # Spec Layer 3 extended the contract: rerank state plus the newest
        # rescore movement (prev_score / score_delta / rescored_at).
        "rerank_marked_at", "prev_score", "score_delta", "rescored_at",
    }
    for job in payload["jobs"]:
        assert set(job) == expected_keys
    j1 = next(j for j in payload["jobs"] if j["url"].endswith("j1"))
    assert j1 == {
        "url": "https://jobs.example/j1",
        "title": "Senior Platform Engineer",
        "location": "Mexico City",
        "site": "LinkedIn",
        "discovered_at": "2026-11-01T00:00:00+00:00",
        "date_posted": RECENT,
        "salary_min": 180000.0,
        "salary_max": 200000.0,
        "salary_currency": "USD",
        "salary_interval": "yearly",
        "seniority": "senior",
        "fit_score": 8,
        "stale": False,
        # Layer 3: never rerank-marked, never rescored.
        "rerank_marked_at": None,
        "prev_score": None,
        "score_delta": None,
        "rescored_at": None,
    }


def test_cli_json_stale_flags_current_resume(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--stale-only", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 4
    assert all(job["stale"] for job in payload["jobs"])
    assert not any(job["url"].endswith("j1") for job in payload["jobs"])


def test_cli_default_table_renders(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "matching jobs" in result.output


def test_cli_unknown_seniority_token_is_typed_error(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--seniority", "senior,exec"])
    assert result.exit_code == 2
    assert "exec" in result.output


def test_cli_malformed_duration_is_typed_error(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--posted-within", "7x"])
    assert result.exit_code == 2
    assert "--posted-within" in result.output


def test_cli_zero_duration_is_typed_error(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--posted-within", "0d"])
    assert result.exit_code == 2


def test_cli_unknown_sort_key_is_typed_error(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--sort", "pay"])
    assert result.exit_code == 2
    assert "score" in result.output


def test_cli_nonpositive_limit_is_typed_error(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--limit", "0"])
    assert result.exit_code == 2


def test_cli_currency_without_min_salary_is_typed_error(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--currency", "USD"])
    assert result.exit_code == 2
    assert "--min-salary" in result.output


def test_cli_min_salary_defaults_to_profile_currency(app_dir, seeded):
    (app_dir / "profile.json").write_text(
        json.dumps({"compensation": {"salary_currency": "EUR"}}),
        encoding="utf-8")
    result = runner.invoke(
        app, ["jobs", "filter", "--min-salary", "150000", "--json"])
    assert result.exit_code == 0, result.output
    # The profile says EUR: the EUR row's max (60k) is below the
    # threshold and no USD row may match — the result is empty.
    assert json.loads(result.output) == {"count": 0, "jobs": []}


def test_cli_min_salary_without_profile_matches_any_currency(app_dir, seeded):
    result = runner.invoke(
        app, ["jobs", "filter", "--min-salary", "150000", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # No profile → no currency default: any currency reaching 150k
    # matches (j1 200k USD, j2 190k USD; j3 tops out at 120k).
    assert payload["count"] == 2


# ---------------------------------------------------------------------------
# CLI flag → SQL translation (audit M2/L1) and LIKE wildcard escaping (L2)
# ---------------------------------------------------------------------------

def test_cli_on_site_flag_selects_non_remote_rows(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--on-site", "--urls"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["https://jobs.example/j3"]


def test_cli_remote_flag_selects_remote_rows(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--remote", "--urls"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "https://jobs.example/j1", "https://jobs.example/j2",
        "https://jobs.example/j4", "https://jobs.example/j5",
    ]


def test_cli_without_remote_flags_returns_both(seeded):
    result = runner.invoke(app, ["jobs", "filter", "--urls"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "https://jobs.example/j1", "https://jobs.example/j2",
        "https://jobs.example/j3", "https://jobs.example/j4",
        "https://jobs.example/j5",
    ]


@pytest.mark.parametrize(
    ("flags", "expected_urls"),
    [
        pytest.param(["--location", "mexico"], ["j1", "j4"], id="location"),
        pytest.param(["--site", "linkedin"], ["j1", "j3"], id="site"),
        pytest.param(["--max-score", "7"], ["j3", "j4"], id="max-score"),
        pytest.param(["--sort", "score", "--asc", "--limit", "2"],
                     ["j4", "j3"], id="asc"),
        pytest.param(["--exclude-keyword", "clearance"],
                     ["j1", "j3", "j4", "j5"], id="exclude-keyword"),
    ],
)
def test_cli_flag_translates_to_expected_rows(seeded, flags, expected_urls):
    result = runner.invoke(app, ["jobs", "filter", *flags, "--urls"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"https://jobs.example/{url}" for url in expected_urls
    ]


def _seed_wildcard_rows(conn):
    """Rows carrying literal % and _ where a LIKE wildcard would lie."""
    _seed(conn, "https://jobs.example/w1", title="Growth 50% engineer",
          is_remote=1, full_description="Equity above 50%.")
    _seed(conn, "https://jobs.example/w2", title="Growth 500 engineer",
          is_remote=1)
    _seed(conn, "https://jobs.example/w3", title="ml_ops specialist",
          is_remote=1)
    _seed(conn, "https://jobs.example/w4", title="mlaops specialist",
          is_remote=1)
    conn.commit()


def test_like_wildcards_in_input_match_literally(seeded):
    _seed_wildcard_rows(database.get_connection())

    # A trailing % is a literal percent sign, not a wildcard.
    assert _urls(filter_jobs(_db_predicates(keywords=["50%"]))) == [
        "https://jobs.example/w1"]
    # An underscore is a literal underscore, not a single-char wildcard.
    assert _urls(filter_jobs(_db_predicates(keywords=["ml_ops"]))) == [
        "https://jobs.example/w3"]
    # Exclusions follow the same rule.
    kept = _urls(filter_jobs(_db_predicates(exclude_keywords=["50%"])))
    assert "https://jobs.example/w1" not in kept
    assert {"https://jobs.example/w2", "https://jobs.example/w3",
            "https://jobs.example/w4"} <= set(kept)
    kept = _urls(filter_jobs(_db_predicates(exclude_keywords=["ml_ops"])))
    assert "https://jobs.example/w3" not in kept
    assert {"https://jobs.example/w1", "https://jobs.example/w2",
            "https://jobs.example/w4"} <= set(kept)


def test_cli_keyword_wildcards_match_literally(seeded):
    _seed_wildcard_rows(database.get_connection())
    result = runner.invoke(app, ["jobs", "filter", "--keyword", "50%", "--urls"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["https://jobs.example/w1"]
