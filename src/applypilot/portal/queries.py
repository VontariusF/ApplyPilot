"""Read queries powering the portal pages (thin SQL over the shared store)."""

import re

from applypilot.database import STAGE_CONDITIONS, get_connection

# Stage filter keys for the Jobs page — mirrors get_jobs_by_stage's
# condition registry, plus a row-count subquery for the outcome feed.
STAGES = (
    "discovered", "pending_detail", "enriched", "pending_score", "scored",
    "score_failed", "pending_tailor", "tailored", "pending_apply", "applied",
)

_JOB_LIST_COLUMNS = (
    "url, title, site, location, salary, fit_score, apply_status, score_error, "
    "discovered_at, applied_at, tailored_resume_path, cover_letter_path, is_remote"
)

# Remote filter values for the jobs board (spec PR D). "yes"/"no" filter on
# the inferred is_remote column; rows with no inference yet match neither.
REMOTE_VALUES = ("", "yes", "no")


def recent_outcomes(limit: int = 10) -> list[dict]:
    """Most recently applied/attempted jobs for the dashboard feed."""
    conn = get_connection()
    rows = conn.execute(
        f"SELECT {_JOB_LIST_COLUMNS}, apply_error "
        "FROM jobs WHERE applied_at IS NOT NULL OR apply_status IS NOT NULL "
        "ORDER BY COALESCE(applied_at, last_attempted_at, discovered_at) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def job_detail(url: str) -> dict | None:
    """Full row for the job detail view."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return dict(row) if row else None


def split_score_reasoning(stored: str | None) -> tuple[str, str]:
    """Split the stored score_reasoning blob into (keywords, reasoning).

    scorer.py persists f"{keywords}\\n{reasoning}" — the first line is the
    comma-separated ATS keyword list, the remainder the reasoning prose.
    Legacy rows (or empty keyword lists) degrade honestly: no newline means
    everything is reasoning; an empty first line means no keywords.
    """
    if not stored:
        return "", ""
    first, sep, rest = stored.partition("\n")
    if not sep:
        return "", first.strip()
    return first.strip(), rest.strip()


# ---------------------------------------------------------------------------
# Jobs board filters (spec PR D)
# ---------------------------------------------------------------------------

# First number in a salary string is treated as the lower bound of a range.
# "k"/"m" suffixes multiply; commas are separators. Hours/months annualize.
_SALARY_NUMBER = re.compile(r"(\d[\d,]*\.?\d*)\s*(k|m)?\b", re.IGNORECASE)
_HOURLY = re.compile(r"(/hr\b|/hour\b|per hour|an hour)", re.IGNORECASE)
_MONTHLY = re.compile(r"(/mo\b|/month\b|per month)", re.IGNORECASE)

# Below this an annual figure is noise (e.g. "85/hr" parsed before
# annualization, or "3 days ago" leaking into the field), not a salary.
_MIN_PLAUSIBLE_ANNUAL = 5_000.0
_HOURS_PER_YEAR = 2080


def parse_salary_lower_bound(salary: str | None) -> float | None:
    """Best-effort numeric lower bound from free-text salary (spec PR D).

    "$120,000 - $150,000" -> 120000; "140k" -> 140000; "$85/hr" -> 176800;
    "Competitive" or "" -> None. The store keeps salary as text, so the
    filter excludes unparseable rows only while the salary filter is
    active — exactly what this function's None encodes.
    """
    if not salary:
        return None
    match = _SALARY_NUMBER.search(salary)
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    elif _HOURLY.search(salary):
        value *= _HOURS_PER_YEAR
    elif _MONTHLY.search(salary):
        value *= 12
    if value < _MIN_PLAUSIBLE_ANNUAL:
        return None
    return value


def filtered_jobs(stage: str = "discovered",
                  site: str = "",
                  location: str = "",
                  discovered_from: str = "",
                  discovered_to: str = "",
                  apply_status: str = "",
                  min_score: int | None = None,
                  min_salary: float | None = None,
                  remote: str = "",
                  limit: int = 50) -> list[dict]:
    """Jobs board rows behind every filter on the filter bar.

    Each parameter narrows independently; empty values are no-ops, so a
    test can exercise one parameter at a time. site is exact-match (the
    dropdown is fed from the store), location a substring match, and the
    discovered-date range compares the store's discovered_at — there is
    no posted_at column, so the UI labels this honestly as "Discovered".

    The salary filter is the one filter that runs in Python over the SQL
    results: salary is free text, so rows that do not parse to a number
    are excluded only while min_salary is active. The remote filter uses
    the inferred is_remote column; rows with no inference (NULL) match
    neither "yes" nor "no".
    """
    condition = STAGE_CONDITIONS.get(stage, "1=1")
    where = [condition]
    params: list = []

    # get_jobs_by_stage semantics: a min-score bound applies only to the
    # stages that hold fit scores. Applying it elsewhere (score_failed,
    # pending_*) would hide rows that legitimately have no score.
    if "?" in condition:
        params.append(min_score if min_score is not None else 7)
    elif min_score is not None and stage in ("scored", "tailored", "applied"):
        where.append("fit_score >= ?")
        params.append(min_score)

    if site:
        where.append("site = ?")
        params.append(site)
    if location:
        where.append("location LIKE ?")
        params.append(f"%{location}%")
    if discovered_from:
        where.append("substr(discovered_at, 1, 10) >= ?")
        params.append(discovered_from)
    if discovered_to:
        where.append("substr(discovered_at, 1, 10) <= ?")
        params.append(discovered_to)
    if apply_status:
        where.append("apply_status = ?")
        params.append(apply_status)
    if remote == "yes":
        where.append("is_remote = 1")
    elif remote == "no":
        where.append("is_remote = 0")

    rows = get_connection().execute(
        f"SELECT * FROM jobs WHERE {' AND '.join(where)} "
        "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC",
        params,
    ).fetchall()
    jobs = [dict(r) for r in rows]

    if min_salary is not None:
        jobs = [j for j in jobs
                if (bound := parse_salary_lower_bound(j.get("salary"))) is not None
                and bound >= min_salary]

    return jobs[:limit] if limit > 0 else jobs


def distinct_sites() -> list[str]:
    """Site names for the filter dropdown, alphabetical."""
    rows = get_connection().execute(
        "SELECT DISTINCT site FROM jobs "
        "WHERE site IS NOT NULL AND site != '' ORDER BY site"
    ).fetchall()
    return [r[0] for r in rows]


def distinct_apply_statuses() -> list[str]:
    """Apply-status values seen in the store, for the filter dropdown."""
    rows = get_connection().execute(
        "SELECT DISTINCT apply_status FROM jobs "
        "WHERE apply_status IS NOT NULL AND apply_status != '' ORDER BY apply_status"
    ).fetchall()
    return [r[0] for r in rows]


def count_jobs() -> int:
    """Total rows in the store — the empty state's 'why it's empty' signal."""
    return get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
