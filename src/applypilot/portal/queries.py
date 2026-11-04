"""Read queries powering the portal pages (thin SQL over the shared store)."""

from applypilot.database import get_connection

# Stage filter keys for the Jobs page — mirrors get_jobs_by_stage's
# condition registry, plus a row-count subquery for the outcome feed.
STAGES = (
    "discovered", "pending_detail", "enriched", "pending_score", "scored",
    "pending_tailor", "tailored", "pending_apply", "applied",
)

_JOB_LIST_COLUMNS = (
    "url, title, site, location, salary, fit_score, apply_status, "
    "discovered_at, applied_at, tailored_resume_path, cover_letter_path"
)


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
