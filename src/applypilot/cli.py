"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import os
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    url: Optional[str] = typer.Option(
        None, "--url",
        help="Scope tailor/cover stages to this exact job URL (single-job runs).",
    ),
    results_per_site: Optional[int] = typer.Option(
        None, "--results-per-site",
        help="Override discovery's results per board for this run (Find jobs preset knob).",
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        url=url,
        results_per_site=results_per_site,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def rescore(
    url: Optional[str] = typer.Argument(
        None,
        help="Re-score this specific job URL (must have a captured description).",
    ),
    all_jobs: bool = typer.Option(False, "--all", help="Re-score every job that has a description."),
    limit: int = typer.Option(0, "--limit", "-l", help="Max jobs to re-score in this pass (with --all)."),
) -> None:
    """Re-score jobs against your resume (re-runs the LLM fit score).

    Re-scoring overwrites the stored fit_score; scoring failures are recorded
    on the job as score_error and shown in the portal.
    """
    if not url and not all_jobs:
        console.print("[red]Nothing to re-score.[/red] Pass a job URL or --all.")
        raise typer.Exit(code=1)
    if url and all_jobs:
        console.print("[red]Pass either a job URL or --all, not both.[/red]")
        raise typer.Exit(code=1)

    _bootstrap()

    from applypilot.config import check_tier

    check_tier(2, "AI scoring")

    from applypilot.scoring.scorer import run_scoring

    result = run_scoring(limit=limit, rescore=True) if all_jobs else run_scoring(url=url)

    if result["errors"]:
        console.print(
            f"[red]{result['errors']} scoring failure(s).[/red] The error is stored per job "
            "(score_error) and shown on the job detail page in the portal."
        )
    console.print(
        f"[green]Scored {result['scored']} job(s) in {result['elapsed']:.1f}s.[/green]"
    )

    if result["errors"]:
        raise typer.Exit(code=1)


@app.command()
def regenerate(
    url: str = typer.Argument(..., help="Job URL whose documents to regenerate."),
    cover: bool = typer.Option(False, "--cover", help="Also clear and regenerate the cover letter."),
) -> None:
    """Clear a job's tailoring state and re-enqueue it for the tailor stage (D3).

    Existing documents stay on disk — history keeps every version. The next
    'applypilot run tailor' (and 'cover' with --cover) generates fresh ones.
    """
    _bootstrap()

    from applypilot.database import get_connection, reset_job_tailoring

    conn = get_connection()
    if not reset_job_tailoring(conn, url, include_cover=cover):
        console.print(f"[red]No job found:[/red] {url}")
        raise typer.Exit(code=1)

    cleared = ["tailored resume"] + (["cover letter"] if cover else [])
    console.print(f"[green]Cleared:[/green] {', '.join(cleared)} for {url}")

    # Re-enqueueing needs the tailor stage's queue conditions met.
    row = conn.execute(
        "SELECT fit_score, full_description FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row and (row[0] is None or row[0] < 7 or not row[1]):
        console.print(
            "[yellow]Note:[/yellow] this job will not re-enqueue until it has a "
            "full description and fit score >= 7 — run 'applypilot run score' first."
        )
    else:
        console.print(
            "Run [bold]applypilot run tailor"
            + (" cover" if cover else "")
            + "[/bold] to regenerate."
        )


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    engine: Optional[str] = typer.Option(
        None, "--engine",
        help=("Auto-apply engine: 'claude' (Claude Code, default) or 'droid' (Factory "
              "Droid). Falls back to the AUTOAPPLY_ENGINE env var, then 'claude'."),
    ),
    model: str = typer.Option(
        "haiku", "--model", "-m",
        help=("Model id passed verbatim to the selected engine: Claude model ids for "
              "claude; Droid model ids (including custom:<name> entries from "
              "~/.factory/config.json) for droid."),
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    # Engine resolution first — a bad name should fail readably before any
    # bootstrap side effects.
    from applypilot.apply.engines import resolve_engine_name

    try:
        engine_name = resolve_engine_name(engine)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 0: the droid engine needs its credential before anything starts
    if engine_name == "droid" and not os.environ.get("FACTORY_API_KEY"):
        console.print(
            "[red]FACTORY_API_KEY is not set.[/red] The droid engine authenticates with "
            "FACTORY_API_KEY — add it to ~/.applypilot/.env or export it."
        )
        raise typer.Exit(code=1)

    # Check 1: Tier 3 required (an engine CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, manual_command
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        generated = gen_prompt(target, min_score=min_score, model=model, engine=engine_name)
        if not generated:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote prompt to:[/green] {generated}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(f"  {manual_command(engine_name, generated, model)}")
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Engine:   {engine_name}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        engine=engine_name,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def portal(
    port: int = typer.Option(8765, "--port", "-p", help="Port to bind on 127.0.0.1."),
) -> None:
    """Launch the localhost web portal (requires the [portal] extra)."""
    try:
        import uvicorn

        from applypilot.portal import create_app
    except ImportError as e:
        console.print(f"[red]Portal dependencies missing:[/red] {e}")
        console.print(
            "Install with: [bold]pip install 'applypilot[portal]'[/bold]"
        )
        raise typer.Exit(code=1)

    app = create_app()

    console.print("\n[bold green]ApplyPilot portal[/bold green]")
    console.print(f"  Local URL: http://127.0.0.1:{port}/")
    console.print("  Token saved at ~/.applypilot/portal_token (0600).")
    console.print(
        "[dim]  First visit asks for the token in the portal's login form —"
        " it is never sent in the URL, so it cannot leak into logs or browser"
        " history.[/dim]\n"
    )
    console.print("[dim]  Ctrl+C to stop. The portal serves only 127.0.0.1.[/dim]\n")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # Composio (optional discovery source — web3.career)
    from applypilot.discovery.composio import doctor_row
    results.append(("Composio discovery", *doctor_row()))

    # --- Tier 2 checks ---
    import os
    from applypilot.llm import describe_provider

    try:
        info = describe_provider()
        results.append((
            "LLM provider",
            ok_mark,
            f"{info['provider']}: {info['base_url']} (model {info['model']}, key {info['key_hint']})",
        ))
    except RuntimeError as exc:
        results.append(("LLM provider", fail_mark, str(exc)))

    # --- Tier 3 checks ---
    from applypilot import engine_auth

    # Claude Code CLI (default engine) — install AND sign-in state
    claude = engine_auth.claude_status()
    if claude.state == engine_auth.SIGNED_IN:
        method = f", sign-in method: {claude.auth_method}" if claude.auth_method else ""
        results.append(("Claude Code CLI", ok_mark, f"{claude.binary} (signed in{method})"))
    elif claude.state == engine_auth.SIGNED_OUT:
        note = f"Installed at {claude.binary} but not signed in — run `claude auth login` (needed for auto-apply)"
        if claude.error:
            note += f" [{claude.error}]"
        results.append(("Claude Code CLI", warn_mark, note))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Factory Droid CLI (optional second engine) — install + key state
    droid = engine_auth.droid_status()
    if droid.state == engine_auth.SIGNED_IN:
        results.append(("Factory Droid CLI", ok_mark, f"{droid.binary} (FACTORY_API_KEY set)"))
    elif droid.state == engine_auth.SIGNED_OUT:
        results.append(("Factory Droid CLI", warn_mark,
                        f"Installed at {droid.binary}; set FACTORY_API_KEY in .env to use it"))
    else:
        results.append(("Factory Droid CLI", warn_mark,
                        "Install from https://docs.factory.ai/cli (only needed for --engine droid)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # FACTORY_API_KEY (optional, droid engine)
    factory_key = os.environ.get("FACTORY_API_KEY")
    if factory_key:
        results.append(("FACTORY_API_KEY", ok_mark, "Droid engine auth available"))
    else:
        results.append(("FACTORY_API_KEY", "[dim]optional[/dim]",
                        "Set FACTORY_API_KEY in .env to use --engine droid"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs an LLM provider — run 'applypilot init')[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code or Droid CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code or Droid CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
