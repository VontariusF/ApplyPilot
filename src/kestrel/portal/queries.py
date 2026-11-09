"""Read queries powering the portal pages (thin SQL over the shared store)."""

import re

from kestrel.database import STAGE_CONDITIONS, get_connection

# Stage filter keys for the Jobs page — mirrors get_jobs_by_stage's
# condition registry, plus a row-count subquery for the outcome feed.
# S4: manual_marked/expired surface the terminal apply statuses so the
# board accounts for every row.
STAGES = (
    "discovered", "pending_detail", "enriched", "pending_score", "scored",
    "score_failed", "pending_tailor", "tailored", "pending_apply", "applied",
    "manual_marked", "expired",
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


SORTS = ("best", "newest", "salary")


def _view_condition(name: str) -> str:
    """WHERE condition for a board view stage.

    Pipeline stages come from STAGE_CONDITIONS (untouched). Two read-only
    extras union existing fields for the board chips — they are views over
    existing columns, not pipeline stages:
    - "new": found, no score outcome yet. The literal discovered stage is
      1=1 (every job is discovered), so its condition cannot distinguish.
    - "apply_failed": failed apply attempts (apply_status), for the
      Needs-attention chip. captcha rows are terminal by decision and a
      distinct status — they never match this condition.
    """
    if name == "new":
        return "fit_score IS NULL AND score_error IS NULL"
    if name == "apply_failed":
        return "apply_status = 'failed'"
    return STAGE_CONDITIONS.get(name, "1=1")


def filtered_jobs(stage: str = "discovered",
                  site: str = "",
                  location: str = "",
                  discovered_from: str = "",
                  discovered_to: str = "",
                  apply_status: str = "",
                  min_score: int | None = None,
                  min_salary: float | None = None,
                  remote: str = "",
                  dismissed: str = "",
                  limit: int = 50,
                  offset: int = 0,
                  sort: str = "best",
                  stages: str = "") -> list[dict]:
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

    The dismissed flag (spec Jobs panel) is board curation: the default
    view (dismissed="") hides dismissed rows, dismissed="only" shows just
    them (the Undo section). It is not a pipeline stage.

    `stages` (comma-separated) ORs together existing stage conditions —
    the plain-language chips span several stages ("New" = discovered +
    pending_detail + enriched) without adding pipeline stages. `sort`
    orders the board: "best" (fit score), "newest" (discovered date) in
    SQL, "salary" in Python via the same lower-bound parser the salary
    filter uses (unparseable rows sort last). `offset` skips into the
    filtered, sorted list — server-side pagination, applied after the
    Python-side salary filter so page boundaries stay stable.
    """
    jobs = _select_jobs(stage=stage, stages=stages, site=site, location=location,
                        discovered_from=discovered_from, discovered_to=discovered_to,
                        apply_status=apply_status, min_score=min_score,
                        min_salary=min_salary, remote=remote, dismissed=dismissed,
                        sort=sort)
    if limit <= 0:
        return jobs
    return jobs[offset:offset + limit]


def count_filtered_jobs(stage: str = "discovered",
                        site: str = "",
                        location: str = "",
                        discovered_from: str = "",
                        discovered_to: str = "",
                        apply_status: str = "",
                        min_score: int | None = None,
                        min_salary: float | None = None,
                        remote: str = "",
                        dismissed: str = "",
                        stages: str = "") -> int:
    """How many jobs match the board filters, ignoring limit/offset —
    the pagination count."""
    return len(_select_jobs(stage=stage, stages=stages, site=site, location=location,
                            discovered_from=discovered_from, discovered_to=discovered_to,
                            apply_status=apply_status, min_score=min_score,
                            min_salary=min_salary, remote=remote, dismissed=dismissed,
                            sort="best"))


def _select_jobs(stage: str, stages: str, site: str, location: str,
                 discovered_from: str, discovered_to: str, apply_status: str,
                 min_score: int | None, min_salary: float | None, remote: str,
                 dismissed: str, sort: str) -> list[dict]:
    """All jobs matching the board filters, sorted. Shared by
    filtered_jobs (which slices) and count_filtered_jobs (which counts)."""
    if stages.strip():
        names = [s.strip() for s in stages.split(",") if s.strip()]
        parts = [_view_condition(s) for s in names]
        condition = "(" + " OR ".join(parts) + ")"
    else:
        condition = STAGE_CONDITIONS.get(stage, "1=1")
    where = [condition]
    params: list = []

    # get_jobs_by_stage semantics: a min-score bound applies only to the
    # stages that hold fit scores. Applying it elsewhere (score_failed,
    # pending_*) would hide rows that legitimately have no score.
    if "?" in condition:
        # One min-score bind per "?" — a multi-stage OR concatenates one
        # condition per selected stage, so e.g. pending_tailor,pending_cover
        # carries two placeholders (F5) and one param left the old binding
        # of the merged statement short.
        params.extend([min_score if min_score is not None else 7] * condition.count("?"))
    elif min_score is not None and stage in ("scored", "tailored", "applied"):
        where.append("fit_score >= ?")
        params.append(min_score)

    if dismissed == "only":
        where.append("dismissed_at IS NOT NULL")
    else:
        where.append("dismissed_at IS NULL")

    if site:
        where.append("site = ?")
        params.append(site)
    if location:
        # S3: escape LIKE wildcards so user-typed % and _ match
        # themselves only — "%" as a location must not match every row.
        escaped = location.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("location LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
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

    if sort == "newest":
        order_by = "ORDER BY discovered_at DESC"
    elif sort == "salary":
        order_by = "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    else:
        order_by = "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"

    rows = get_connection().execute(
        f"SELECT * FROM jobs WHERE {' AND '.join(where)} "
        f"{order_by}",
        params,
    ).fetchall()
    jobs = [dict(r) for r in rows]

    if min_salary is not None:
        jobs = [j for j in jobs
                if (bound := parse_salary_lower_bound(j.get("salary"))) is not None
                and bound >= min_salary]

    if sort == "salary":
        # Highest lower-bound first; unparseable or missing salary sorts last.
        jobs.sort(key=lambda j: parse_salary_lower_bound(j.get("salary")) or -1,
                  reverse=True)

    return jobs


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


def dismissed_jobs() -> list[dict]:
    """All board-dismissed jobs, most recent dismissal first (Undo list)."""
    rows = get_connection().execute(
        "SELECT * FROM jobs WHERE dismissed_at IS NOT NULL "
        "ORDER BY dismissed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def dismissed_count() -> int:
    """How many jobs sit in the board's collapsed Dismissed section."""
    return get_connection().execute(
        "SELECT COUNT(*) FROM jobs WHERE dismissed_at IS NOT NULL"
    ).fetchone()[0]


def count_jobs() -> int:
    """Total rows in the store — the empty state's 'why it's empty' signal."""
    return get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
