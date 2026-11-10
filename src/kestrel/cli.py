# SPDX-License-Identifier: AGPL-3.0-only
"""Kestrel CLI — the main entry point."""

from __future__ import annotations
import json
import logging
import os
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from kestrel import __version__
from kestrel.agent_protocol import _ProtocolUsageGroup, agent_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="kestrel",
    help="The job-application pipeline your coding agent drives — onboarding, scoring, tailoring, cover letters, and apply bookkeeping over a JSON protocol. Provider-backed and built-in-browser modes are optional.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")

# The `agent` sub-app: the JSON protocol any shell-capable coding agent
# drives (spec art_A71wyYS4). A separate module keeps its pure-stdout
# contract clear of the Rich helpers above. _ProtocolUsageGroup converts
# click/typer usage errors into the JSON envelope (audit F9) — a nested
# sub-app never runs its own main(), so the group class is the hook point.
app.add_typer(
    agent_app,
    name="agent",
    help="The agent-native pipeline protocol: your host coding agent drives onboarding, scoring, tailoring, cover letters, and apply bookkeeping — one JSON document per verb on stdout.",
    cls=_ProtocolUsageGroup,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from kestrel.config import load_env, ensure_dirs
    from kestrel.database import init_db

    load_env()
    ensure_dirs()
    init_db()


# Presentation-free states shared by the Rich renderer and the --json
# contract. "fail" renders as MISSING (the historical doctor wording) in
# Rich mode.
_MARKS = {
    "ok": "[green]OK[/green]",
    "warn": "[yellow]WARN[/yellow]",
    "fail": "[red]MISSING[/red]",
    "optional": "[dim]optional[/dim]",
}


def _emit_json(payload: dict) -> None:
    """Write one pure-JSON document to stdout — no Rich markup, no stderr."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _scored_label(provenance: dict) -> str:
    """The status table's 'Scored ...' row label, honest about authorship.

    Reads the scored_by split (spec art_IMViYTMU) instead of assuming: a
    store with only host-written scores says 'host agent'; a store with
    none keeps the legacy 'Scored by LLM' wording (unattributed
    pre-column rows render as they always have); a mix spells both out.
    """
    host = provenance.get("host", 0)
    if host == 0:
        return "Scored by LLM"
    rest = provenance.get("provider", 0) + provenance.get("unattributed", 0)
    if rest == 0:
        return "Scored by host agent"
    return f"Scored (host {host}, LLM {rest})"


def _emit_json_error(code: str, message: str,
                     details: dict | None = None) -> None:
    """Write the --json error envelope to stdout and exit nonzero.

    Details ride along only when present (plan refusals attach expected/
    actual values); the plain two-key envelope is unchanged.
    """
    error: dict = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    _emit_json({"error": error})
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]kestrel[/bold] {__version__}")
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
    """Kestrel — the job-application pipeline your coding agent drives."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from kestrel.wizard.init import run_wizard

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

    from kestrel.pipeline import run_pipeline

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
        from kestrel.config import check_tier
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

    from kestrel.config import check_tier

    check_tier(2, "AI scoring")

    from kestrel.scoring.scorer import run_scoring

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
    'kestrel run tailor' (and 'cover' with --cover) generates fresh ones.
    """
    _bootstrap()

    from kestrel.database import get_connection, reset_job_tailoring

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
            "full description and fit score >= 7 — run 'kestrel run score' first."
        )
    else:
        console.print(
            "Run [bold]kestrel run tailor"
            + (" cover" if cover else "")
            + "[/bold] to regenerate."
        )


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: Optional[int] = typer.Option(
        None, "--min-score",
        help=("Minimum fit score for job selection (default: 7). On "
              "--i-confirm, omitting the flag runs the staged plan's "
              "threshold."),
    ),
    engine: Optional[str] = typer.Option(
        None, "--engine",
        help=("Auto-apply engine: 'claude' (Claude Code, default) or 'droid' (Factory "
              "Droid). Falls back to the AUTOAPPLY_ENGINE env var, then 'claude'."),
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help=("Model id passed verbatim to the selected engine: Claude model ids for "
              "claude; Droid model ids (including custom:<name> entries from "
              "~/.factory/config.json) for droid. Defaults to the AUTOAPPLY_MODEL "
              "setting, then 'haiku'."),
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    daily_cap: Optional[int] = typer.Option(
        None, "--daily-cap",
        help=("Max applications per UTC day this run must respect. The count is "
              "re-checked live before every job, so applications from any path "
              "count. Omit for no cap — a cap applies only when you request "
              "one (this flag, an apply.yaml daily_cap, or a portal preset)."),
    ),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset failed jobs for retry (captcha-blocked jobs are kept terminal and skipped)."),
    plan: bool = typer.Option(
        False, "--plan",
        help=("Gate 2: print the batch this run would apply as ONE pure-JSON "
              "plan (job URLs, per-job estimates, daily-cap headroom, a plan "
              "hash) and stage its confirm token. Commits nothing."),
    ),
    i_confirm: Optional[str] = typer.Option(
        None, "--i-confirm",
        help=("Gate 2: the plan hash from a --plan run staged today (UTC). "
              "Second invocation of plan-then-confirm — verifies the token, "
              "consumes it, and runs exactly the planned batch."),
    ),
) -> None:
    """Launch auto-apply to submit job applications."""
    # Engine resolution first — a bad name should fail readably before any
    # bootstrap side effects.
    from kestrel.apply.engines import resolve_engine_name

    try:
        engine_name = resolve_engine_name(engine)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    _bootstrap()

    # The model default resolves at call time — after _bootstrap() loaded
    # the store's .env into the environment — so the Settings page's saved
    # AUTOAPPLY_MODEL governs direct CLI runs too, matching the portal's
    # run builder (P1-2).
    if model is None:
        from kestrel.config import default_apply_model

        model = default_apply_model()

    # B3: the run's threshold — the flag when given, the config default
    # otherwise. The raw Optional rides into verify_confirm alone: None
    # there means "the staged plan's min_score" (the natural confirm);
    # an explicit int must agree with the staged plan.
    from kestrel import config as _config

    effective_min_score = (min_score if min_score is not None
                           else _config.DEFAULTS["min_score"])

    # --- Gate 2/3 (spec art_A71wyYS4): plan-then-confirm for externally
    # driven batches. --plan is a read-only preview that stages the
    # per-batch token; --i-confirm verifies the token on a second
    # invocation the same UTC day and pins the run to the planned URLs.
    from kestrel.apply.planning import (
        PlanError, consume_staged_plan, stage_batch_plan, verify_confirm,
    )

    if plan and i_confirm is not None:
        _emit_json_error(
            "invalid_usage",
            "--plan and --i-confirm are mutually exclusive — plan first, "
            "then confirm the hash it printed.")
    if (plan or i_confirm is not None) and any(
            (continuous, bool(url), gen, bool(mark_applied),
             bool(mark_failed), reset_failed)):
        _emit_json_error(
            "invalid_usage",
            "--plan/--i-confirm run a batch and cannot be combined with "
            "--continuous, --url, --gen, --mark-applied, --mark-failed, "
            "or --reset-failed.")

    if plan:
        try:
            # B2: an omitted --limit sizes the batch to min(headroom,
            # eligible) inside stage_batch_plan — no silent LIMIT 1.
            payload = stage_batch_plan(
                min_score=effective_min_score,
                limit=limit,
                explicit_cap=daily_cap,
            )
        except PlanError as exc:
            _emit_json_error(exc.code, str(exc), exc.details)
        _emit_json(payload)
        return

    planned: Optional[dict] = None
    if i_confirm is not None:
        try:
            # Selection flags stay None when absent — the planned batch
            # defines the run (size and threshold), so only an explicit
            # flag can disagree with it (B3: min_score rides in raw; None
            # inherits the staged plan's threshold).
            planned = verify_confirm(
                i_confirm,
                explicit_cap=daily_cap,
                min_score=min_score,
                limit=limit,
            )
        except PlanError as exc:
            _emit_json_error(exc.code, str(exc), exc.details)

    from kestrel.config import check_tier, PROFILE_PATH as _profile_path
    from kestrel.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from kestrel.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from kestrel.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from kestrel.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 0: the droid engine needs its credential before anything starts
    if engine_name == "droid" and not os.environ.get("FACTORY_API_KEY"):
        console.print(
            "[red]FACTORY_API_KEY is not set.[/red] The droid engine authenticates with "
            "FACTORY_API_KEY — add it to ~/.kestrel/.env or export it."
        )
        raise typer.Exit(code=1)

    # Check 1: Tier 3 required (an engine CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]kestrel init[/bold] to create your profile first."
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
                "Run [bold]kestrel run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from kestrel.apply.launcher import gen_prompt, manual_command
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        generated = gen_prompt(target, min_score=effective_min_score, model=model, engine=engine_name)
        if not generated:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote prompt to:[/green] {generated}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(f"  {manual_command(engine_name, generated, model)}")
        return

    from kestrel.apply.launcher import main as apply_main

    if planned is not None:
        # Gate 2: the token burns only now — after every check that could
        # refuse the run — so a broken environment never costs a re-plan.
        consume_staged_plan()
        effective_limit = len(planned["urls"])
        allowed_urls: Optional[set[str]] = set(planned["urls"])
        daily_cap = planned["cap"]
    else:
        effective_limit = limit if limit is not None else (0 if continuous else 1)
        allowed_urls = None

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
        # B3: the confirmed run filters at the STAGED threshold — the
        # queue predicate and the plan's selection can never disagree.
        min_score=planned["min_score"] if planned else effective_min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        engine=engine_name,
        daily_cap=daily_cap,
        allowed_urls=allowed_urls,
    )


@app.command()
def status(
    json_output: bool = typer.Option(
        False, "--json", help="Emit the raw pipeline-stats dict as JSON instead of a Rich table."
    ),
) -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from kestrel.database import get_stats

    if json_output:
        try:
            stats = get_stats()
        except Exception as exc:
            _emit_json_error("database_error", f"{type(exc).__name__}: {exc}")
        # Emitted outside the except so a serialization bug is not
        # mislabeled as a database failure.
        _emit_json(stats)
        return

    stats = get_stats()

    console.print("\n[bold]Kestrel Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row(_scored_label(stats["score_provenance"]), str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["pending_score"]))
    # S2: visible accounting for errored scores (a failed rescore keeps
    # its previous score, so it counts in `scored` too — by design).
    summary.add_row("Score failures", str(stats["score_failed"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Captcha blocked", str(stats["captcha"]))
    # S4: the other terminal apply statuses, so every row is accounted.
    summary.add_row("Manual-marked (ATS)", str(stats["manual_marked"]))
    summary.add_row("Expired postings", str(stats["expired"]))
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

    from kestrel.view import open_dashboard

    open_dashboard()


@app.command()
def portal(
    port: int = typer.Option(8765, "--port", "-p", help="Port to bind on 127.0.0.1."),
) -> None:
    """Launch the localhost web portal (requires the [portal] extra)."""
    try:
        import uvicorn

        from kestrel.portal import create_app
        from kestrel.portal.auth import token_path
    except ImportError as e:
        console.print(f"[red]Portal dependencies missing:[/red] {e}")
        console.print(
            "Install with: [bold]pip install 'kestrel[portal]'[/bold]"
        )
        raise typer.Exit(code=1)

    app = create_app()

    console.print("\n[bold green]Kestrel portal[/bold green]")
    console.print(f"  Local URL: http://127.0.0.1:{port}/")
    # F-9: print the path auth.py actually uses — under an KESTREL_DIR
    # override the hardcoded ~/.kestrel path was wrong.
    console.print(f"  Token saved at {token_path()} (0600).")
    console.print(
        "[dim]  First visit asks for the token in the portal's login form —"
        " it is never sent in the URL, so it cannot leak into logs or browser"
        " history.[/dim]\n"
    )
    console.print("[dim]  Ctrl+C to stop. The portal serves only 127.0.0.1.[/dim]\n")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def _doctor_checks() -> list[dict]:
    """Collect doctor checks as presentation-free data.

    Each check is {name, status, note, fix}: status is one of
    ok|warn|fail|optional, and fix is a machine-readable remediation hint
    ({code, command}) or None. Both the Rich renderer and the --json
    contract consume this, so the two surfaces cannot drift apart.
    """

    # Agent-native onboarding is the primary route (PR #81/#82); the
    # wizard stays as the interactive alternative. Both doctor hints and
    # the machine-readable fix commands lead with the agent route.
    AGENT_HINT = ("run 'kestrel agent start --mode agent' — your agent conducts "
                  "the onboarding interview (no provider needed)")
    WIZARD_HINT = "run 'kestrel init' — guided wizard for interactive setups"

    import os
    import shutil

    from kestrel.config import (
        PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path,
    )

    checks: list[dict] = []

    def _check(name: str, state: str, note: str, fix: dict | None = None) -> None:
        checks.append({"name": name, "status": state, "note": note, "fix": fix})

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        _check("profile.json", "ok", str(PROFILE_PATH))
    else:
        _check("profile.json", "fail",
               f"Missing — {AGENT_HINT}, or {WIZARD_HINT}",
               {"code": "run_init", "command": "kestrel agent start --mode agent"})

    # Resume
    if RESUME_PATH.exists():
        _check("resume.txt", "ok", str(RESUME_PATH))
    elif RESUME_PDF_PATH.exists():
        _check("resume.txt", "warn", "Only PDF found — plain-text needed for AI stages",
               {"code": "add_plain_text_resume", "command": None})
    else:
        _check("resume.txt", "fail",
               f"Missing — {AGENT_HINT} to add your resume, or {WIZARD_HINT}",
               {"code": "run_init", "command": "kestrel agent start --mode agent"})

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        _check("searches.yaml", "ok", str(SEARCH_CONFIG_PATH))
    else:
        _check("searches.yaml", "warn",
               f"Will use example config — {AGENT_HINT}, or {WIZARD_HINT}",
               {"code": "run_init", "command": "kestrel agent start --mode agent"})

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        _check("python-jobspy", "ok", "Job board scraping available")
    except ImportError:
        pip_line = ("pip install --no-deps python-jobspy && pip install pydantic "
                    "tls-client requests markdownify regex")
        _check("python-jobspy", "warn", pip_line, {"code": "install_jobspy", "command": pip_line})

    # Composio (optional discovery source — web3.career). composio_state()
    # is the presentation-free source; doctor_row() only maps its states to
    # legacy Rich marks, which _MARKS now owns.
    from kestrel.discovery.composio import composio_state
    state, note = composio_state()
    if state == "warn":
        _check("Composio discovery", state, note, {"code": "fix_composio_discovery", "command": None})
    elif state == "optional":
        _check("Composio discovery", state, note, {"code": "set_composio_api_key", "command": None})
    else:
        _check("Composio discovery", state, note)

    # --- Tier 2 checks ---
    from kestrel.llm import describe_provider

    try:
        info = describe_provider()
        _check("LLM provider", "ok",
               f"{info['provider']}: {info['base_url']} (model {info['model']}, key {info['key_hint']})")
    except RuntimeError as exc:
        _check("LLM provider", "fail", str(exc),
               {"code": "configure_llm_provider", "command": "kestrel agent start --mode agent"})

    # --- Tier 3 checks ---
    from kestrel import engine_auth

    # Claude Code CLI (default engine) — install AND sign-in state
    claude = engine_auth.claude_status()
    if claude.state == engine_auth.SIGNED_IN:
        method = f", sign-in method: {claude.auth_method}" if claude.auth_method else ""
        _check("Claude Code CLI", "ok", f"{claude.binary} (signed in{method})")
    elif claude.state == engine_auth.SIGNED_OUT:
        note = f"Installed at {claude.binary} but not signed in — run `claude auth login` (needed for auto-apply)"
        if claude.error:
            note += f" [{claude.error}]"
        _check("Claude Code CLI", "warn", note, {"code": "claude_cli_login", "command": "claude auth login"})
    else:
        _check("Claude Code CLI", "fail",
               "Install from https://claude.ai/code (needed for auto-apply)",
               {"code": "install_claude_cli", "command": None})

    # Factory Droid CLI (optional second engine) — install + key state
    droid = engine_auth.droid_status()
    if droid.state == engine_auth.SIGNED_IN:
        _check("Factory Droid CLI", "ok", f"{droid.binary} (FACTORY_API_KEY set)")
    elif droid.state == engine_auth.SIGNED_OUT:
        _check("Factory Droid CLI", "warn",
               f"Installed at {droid.binary}; set FACTORY_API_KEY in .env to use it",
               {"code": "set_factory_api_key", "command": None})
    else:
        _check("Factory Droid CLI", "warn",
               "Install from https://docs.factory.ai/cli (only needed for --engine droid)",
               {"code": "install_droid_cli", "command": None})

    # Chrome
    try:
        chrome_path = get_chrome_path()
        _check("Chrome/Chromium", "ok", chrome_path)
    except FileNotFoundError:
        _check("Chrome/Chromium", "fail",
               "Install Chrome or set CHROME_PATH env var (needed for auto-apply)",
               {"code": "install_chrome", "command": None})

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        _check("Node.js (npx)", "ok", npx_bin)
    else:
        _check("Node.js (npx)", "fail",
               "Install Node.js 18+ from nodejs.org (needed for auto-apply)",
               {"code": "install_node", "command": None})

    # CapSolver (optional)
    if os.environ.get("CAPSOLVER_API_KEY"):
        _check("CapSolver API key", "ok", "CAPTCHA solving enabled")
    else:
        _check("CapSolver API key", "optional",
               "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving",
               {"code": "set_capsolver_api_key", "command": None})

    # FACTORY_API_KEY (optional, droid engine)
    if os.environ.get("FACTORY_API_KEY"):
        _check("FACTORY_API_KEY", "ok", "Droid engine auth available")
    else:
        _check("FACTORY_API_KEY", "optional",
               "Set FACTORY_API_KEY in .env to use --engine droid",
               {"code": "set_factory_api_key", "command": None})

    return checks


@app.command()
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the Rich report."
    ),
) -> None:
    """Check your setup and diagnose missing requirements."""
    from kestrel.config import load_env, get_tier, TIER_LABELS

    load_env()

    if json_output:
        try:
            checks = _doctor_checks()
        except Exception as exc:
            _emit_json_error("doctor_error", f"{type(exc).__name__}: {exc}")
        tier = get_tier()
        _emit_json({
            "ok": not any(c["status"] == "fail" for c in checks),
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "checks": checks,
        })
        return

    checks = _doctor_checks()

    # --- Render results ---
    console.print()
    console.print("[bold]Kestrel Doctor[/bold]\n")

    col_w = max(len(c["name"]) for c in checks) + 2
    for check in checks:
        pad = " " * (col_w - len(check["name"]))
        console.print(f"  {check['name']}{pad}{_MARKS[check['status']]}  [dim]{check['note']}[/dim]")

    console.print()

    # Tier summary
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs an LLM provider — "
                      "run 'kestrel agent start --mode agent', or 'kestrel init' for the guided wizard)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code or Droid CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code or Droid CLI + Chrome + Node.js)[/dim]")

    console.print()


# ---------------------------------------------------------------------------
# jobs sub-app: deep filtering over the store (spec art_we4AUxEy Layer 2)
# ---------------------------------------------------------------------------

jobs_app = typer.Typer(name="jobs", help="Filter and inspect the jobs store.")
app.add_typer(jobs_app, name="jobs")

# The spec's sort surface. The builder's full mapping lives in
# database._FILTER_SORT_COLUMNS; the CLI validates against this tuple so
# an unknown --sort is a typed usage error, not a traceback.
_FILTER_SORT_KEYS = ("score", "date", "salary", "discovered")

# The explicit filterable-dimension contract for --json output (spec
# Layer 2): identity columns plus every dimension a predicate reads,
# with the derived `stale` flag — consumers see why a row matched.
_JSON_JOB_FIELDS = (
    "url", "title", "location", "site", "discovered_at", "date_posted",
    "salary_min", "salary_max", "salary_currency", "salary_interval",
    "seniority", "fit_score", "stale",
    # Spec Layer 3: rerank state and the newest rescore movement.
    "rerank_marked_at", "prev_score", "score_delta", "rescored_at",
)


def _parse_csv_list(raw: str | None) -> list[str] | None:
    """Split a comma-separated flag value into tokens ('' → None)."""
    if raw is None:
        return None
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    return tokens or None


def _parse_posted_within(raw: str) -> int:
    """Parse --posted-within: '7d'/'30d'/'90d' or a plain int of days.

    Anything else is a typed CLI usage error (spec F5-consistent
    hardening), not a traceback.
    """
    text = raw.strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = int(text)
    except ValueError:
        raise typer.BadParameter(
            f"--posted-within must be a duration like '14d' or a plain number of "
            f"days, got {raw!r}."
        ) from None
    if days <= 0:
        raise typer.BadParameter("--posted-within must be a positive number of days.")
    return days


def _parse_rescored_since(raw: str) -> str:
    """Parse --rescored-since into an ISO cutoff: '30m', '1h', '2d', or a
    plain number of minutes. Anything else is a typed CLI usage error.
    """
    from datetime import datetime, timedelta, timezone

    text = raw.strip().lower()
    unit_seconds = {"m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in unit_seconds:
        value, seconds = text[:-1], unit_seconds[text[-1]]
    else:
        value, seconds = text, 60
    try:
        amount = int(value)
    except ValueError:
        raise typer.BadParameter(
            f"--rescored-since must be a duration like '30m', '1h', or '2d' "
            f"(or a plain number of minutes), got {raw!r}."
        ) from None
    if amount <= 0:
        raise typer.BadParameter("--rescored-since must be a positive duration.")
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=amount * seconds)
    return cutoff.isoformat()


def _predicates_from_flags(
    *, posted_within: Optional[str], location: Optional[str],
    remote: Optional[bool], seniority: Optional[str], min_salary: Optional[float],
    currency: Optional[str], min_score: Optional[int], max_score: Optional[int],
    site: Optional[str], keyword: Optional[str], exclude_keyword: Optional[str],
    stale_only: bool,
):
    """Translate the shared filter flags into a FilterPredicates set.

    Both `jobs filter` and `jobs rerank` target the same query builder,
    so both translate flags through this one helper — the two surfaces
    cannot drift on token validation or currency resolution.
    """
    from kestrel.database import VALID_SENIORITY_TIERS, FilterPredicates

    seniority_list = _parse_csv_list(seniority)
    if seniority_list:
        unknown = sorted(
            t.lower() for t in seniority_list
            if t.lower() not in VALID_SENIORITY_TIERS
        )
        if unknown:
            raise typer.BadParameter(
                f"Unknown seniority tier(s) {', '.join(unknown)} — valid tiers: "
                f"{', '.join(VALID_SENIORITY_TIERS)}."
            )
    if min_salary is None and currency is not None:
        raise typer.BadParameter("--currency only matters together with --min-salary.")

    currency_value = currency or (
        _profile_currency() if min_salary is not None else None)

    return FilterPredicates(
        posted_within_days=_parse_posted_within(posted_within) if posted_within else None,
        location=location.strip() if location else None,
        remote=remote,
        seniority=seniority_list,
        min_salary=min_salary,
        currency=currency_value,
        min_score=min_score,
        max_score=max_score,
        sites=_parse_csv_list(site),
        keywords=_parse_csv_list(keyword),
        exclude_keywords=_parse_csv_list(exclude_keyword),
        stale_only=stale_only,
    )


def _profile_currency() -> str | None:
    """The profile's compensation currency, or None when unset.

    No profile yet (pre-onboarding filter runs) simply means no currency
    default — the salary predicate then matches any currency.
    """
    from kestrel.config import load_profile

    try:
        compensation = load_profile().get("compensation", {})
    except FileNotFoundError:
        return None
    currency = compensation.get("salary_currency")
    if currency is None or not str(currency).strip():
        return None
    return str(currency).strip()


def _current_resume_hash() -> str:
    """sha256 of resume.txt's text, '' when no resume is on disk.

    An empty hash differs from any stored scored_resume_hash, so with no
    resume to compare against every existing score reports stale.
    """
    from kestrel import config
    from kestrel.scoring.scorer import read_resume_text, resume_sha256

    text = read_resume_text(config.RESUME_PATH)
    if text is None:
        return ""
    return resume_sha256(text)


@jobs_app.command("filter")
def jobs_filter(
    posted_within: Optional[str] = typer.Option(
        None, "--posted-within",
        help="date_posted within N days — '7d'/'30d'/'90d' or a plain int.",
    ),
    location: Optional[str] = typer.Option(
        None, "--location", help="Location substring, case-insensitive."),
    # Boolean PAIR syntax (--remote/--on-site) — the two names are
    # aliases that set True/False respectively. Comma-separated names
    # would make --on-site a second set-true alias of --remote (audit H1).
    remote: Optional[bool] = typer.Option(
        None, "--remote/--on-site",
        help="Restrict to remote (is_remote=1) or on-site (is_remote=0) rows.",
    ),
    seniority: Optional[str] = typer.Option(
        None, "--seniority",
        help="Comma-separated any-of tiers: intern,junior,mid,senior,staff,principal,lead,manager.",
    ),
    min_salary: Optional[float] = typer.Option(
        None, "--min-salary",
        help="Minimum annualized pay — a range reaching this counts. Without "
             "a resolvable currency (no --currency and no profile "
             "compensation.salary_currency), rows in ANY currency match — "
             "currencies are not compared and results may mix currencies.",
    ),
    currency: Optional[str] = typer.Option(
        None, "--currency",
        help="Currency for --min-salary matching (default: the profile's compensation.salary_currency).",
    ),
    min_score: Optional[int] = typer.Option(
        None, "--min-score", help="Minimum fit_score."),
    max_score: Optional[int] = typer.Option(
        None, "--max-score", help="Maximum fit_score."),
    site: Optional[str] = typer.Option(
        None, "--site", help="Comma-separated any-of source boards, case-insensitive."),
    keyword: Optional[str] = typer.Option(
        None, "--keyword",
        help="Comma-separated any-of substrings over title + full_description.",
    ),
    exclude_keyword: Optional[str] = typer.Option(
        None, "--exclude-keyword",
        help="Comma-separated substrings that must not appear in title + full_description.",
    ),
    stale_only: bool = typer.Option(
        False, "--stale-only",
        help="Only jobs whose score is stale: scored before the current resume.txt (or never bound).",
    ),
    rescored_since: Optional[str] = typer.Option(
        None, "--rescored-since",
        help="Only jobs re-scored within a window — '30m', '1h', '2d', or a plain "
             "number of minutes. JSON output reports each job's prev_score and "
             "score_delta (spec Layer 3).",
    ),
    sort: str = typer.Option(
        "discovered", "--sort",
        help="Sort key: score|date|salary|discovered (descending by default).",
    ),
    ascending: bool = typer.Option(False, "--asc", help="Sort ascending."),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Maximum rows to return."),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit one pure-JSON document (takes precedence over --urls).",
    ),
    urls: bool = typer.Option(
        False, "--urls", help="Print one URL per line, for piping."),
) -> None:
    """Query the store with composable predicates (all AND together).

    Read-only. Zero predicates lists everything (honoring --sort/--limit).
    """
    _bootstrap()

    from kestrel.database import filter_jobs

    if sort not in _FILTER_SORT_KEYS:
        raise typer.BadParameter(
            f"Unknown sort key {sort!r} — valid keys: {', '.join(_FILTER_SORT_KEYS)}."
        )
    if limit is not None and limit <= 0:
        raise typer.BadParameter("--limit must be a positive integer.")

    predicates = _predicates_from_flags(
        posted_within=posted_within, location=location, remote=remote,
        seniority=seniority, min_salary=min_salary, currency=currency,
        min_score=min_score, max_score=max_score, site=site, keyword=keyword,
        exclude_keyword=exclude_keyword, stale_only=stale_only,
    )
    predicates.rescored_since = (
        _parse_rescored_since(rescored_since) if rescored_since else None)
    predicates.sort = sort
    predicates.ascending = ascending
    predicates.limit = limit
    jobs = filter_jobs(predicates, current_resume_hash=_current_resume_hash())

    if json_output:
        _emit_json({
            "count": len(jobs),
            "jobs": [{field: job[field] for field in _JSON_JOB_FIELDS}
                     for job in jobs],
        })
        return
    if urls:
        for job in jobs:
            typer.echo(job["url"])
        return

    table = Table(title=f"{len(jobs)} matching jobs")
    for column in ("URL", "Title", "Location", "Remote", "Seniority",
                   "Salary", "Score", "Posted", "Stale", "Rescored"):
        table.add_column(column)
    for job in jobs:
        remote_label = {1: "remote", 0: "on-site"}.get(job["is_remote"], "—")
        rescored = (
            f"{job['prev_score']}→{job['fit_score']}"
            if job["rescored_at"] and job["prev_score"] is not None
            and job["fit_score"] is not None else ""
        )
        table.add_row(
            job["url"] or "—",
            job["title"] or "—",
            job["location"] or "—",
            remote_label,
            job["seniority"] or "—",
            job["salary"] or "—",
            "—" if job["fit_score"] is None else str(job["fit_score"]),
            (job["date_posted"] or "")[:10] or "—",
            "yes" if job["stale"] else "",
            rescored,
        )
    console.print(table)




@jobs_app.command("rerank")
def jobs_rerank(
    posted_within: Optional[str] = typer.Option(
        None, "--posted-within",
        help="date_posted within N days — '7d'/'30d'/'90d' or a plain int.",
    ),
    location: Optional[str] = typer.Option(
        None, "--location", help="Location substring, case-insensitive."),
    remote: Optional[bool] = typer.Option(
        None, "--remote/--on-site",
        help="Restrict to remote (is_remote=1) or on-site (is_remote=0) rows.",
    ),
    seniority: Optional[str] = typer.Option(
        None, "--seniority",
        help="Comma-separated any-of tiers: intern,junior,mid,senior,staff,principal,lead,manager.",
    ),
    min_salary: Optional[float] = typer.Option(
        None, "--min-salary",
        help="Minimum annualized pay — a range reaching this counts."),
    currency: Optional[str] = typer.Option(
        None, "--currency",
        help="Currency for --min-salary matching (default: the profile's compensation.salary_currency).",
    ),
    min_score: Optional[int] = typer.Option(
        None, "--min-score", help="Minimum fit_score."),
    max_score: Optional[int] = typer.Option(
        None, "--max-score", help="Maximum fit_score."),
    site: Optional[str] = typer.Option(
        None, "--site", help="Comma-separated any-of source boards, case-insensitive."),
    keyword: Optional[str] = typer.Option(
        None, "--keyword",
        help="Comma-separated any-of substrings over title + full_description.",
    ),
    exclude_keyword: Optional[str] = typer.Option(
        None, "--exclude-keyword",
        help="Comma-separated substrings that must not appear in title + full_description.",
    ),
    stale_only: bool = typer.Option(
        False, "--stale-only",
        help="Target only jobs whose score is stale: scored before the current resume.txt (or never bound).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the target set and count; commit nothing."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit one pure-JSON document (takes precedence over --urls)."),
    urls: bool = typer.Option(
        False, "--urls", help="Print one URL per line (the target set), for piping."),
) -> None:
    """Mark a filtered set for re-judgment by the host score loop.

    The flags match `jobs filter` (they share one query builder). With
    --dry-run this is read-only: the target set and count print, nothing
    commits. Without it, each target's rerank_marked_at is stamped —
    `agent job-next --stage score` then serves exactly these before the
    pending queue, and each re-scored job leaves the set — and nothing
    else changes: plan eligibility and staged plan tokens are untouched
    (staleness is advisory, spec art_we4AUxEy Layer 3). Zero predicates
    targets every scoreable job in the store. Dismissed rows are
    excluded from the target set — dismissal is board-level curation —
    and reported separately in --json as skipped_dismissed.
    """
    _bootstrap()

    from kestrel.database import filter_jobs, get_connection, mark_rerank

    predicates = _predicates_from_flags(
        posted_within=posted_within, location=location, remote=remote,
        seniority=seniority, min_salary=min_salary, currency=currency,
        min_score=min_score, max_score=max_score, site=site, keyword=keyword,
        exclude_keyword=exclude_keyword, stale_only=stale_only,
    )
    jobs = filter_jobs(predicates, current_resume_hash=_current_resume_hash())
    # Only described rows can be re-judged — an undescribed job needs
    # enrich first — and dismissal is board-level curation: a dismissed
    # row never re-enters the scoring loop through rerank (audit
    # Finding 3). Both skips are reported, not silent.
    targets = [job for job in jobs
               if job["full_description"] and not job["dismissed_at"]]
    skipped_description = sum(1 for job in jobs if not job["full_description"])
    skipped_dismissed = len(jobs) - len(targets) - skipped_description

    marked = 0
    if not dry_run:
        from datetime import datetime, timezone

        conn = get_connection()
        marked = mark_rerank(
            conn, [job["url"] for job in targets],
            datetime.now(timezone.utc).isoformat())
        # mark_rerank's contract: callers commit. Without this commit
        # the UPDATE rolls back when the CLI process exits — the mark
        # silently lost while the command still reports `marked: N`
        # (audit Finding 1, the Blocker).
        conn.commit()

    if json_output:
        _emit_json({
            "dry_run": dry_run,
            "count": len(targets),
            "marked": marked,
            "skipped": skipped_description + skipped_dismissed,
            "skipped_dismissed": skipped_dismissed,
            "jobs": [{field: job[field] for field in _JSON_JOB_FIELDS}
                     for job in targets],
        })
        return
    if urls:
        for job in targets:
            typer.echo(job["url"])
        return

    verb = "would be marked" if dry_run else "marked"
    table = Table(title=(f"{len(targets)} job(s) {verb} for re-scoring "
                         f"({skipped_description} skipped: no captured "
                         f"description; {skipped_dismissed} dismissed)"))
    for column in ("URL", "Title", "Score", "Stale"):
        table.add_column(column)
    for job in targets:
        table.add_row(
            job["url"] or "—",
            job["title"] or "—",
            "—" if job["fit_score"] is None else str(job["fit_score"]),
            "yes" if job["stale"] else "",
        )
    console.print(table)
    if not dry_run and marked:
        console.print(
            "[dim]`agent job-next --stage score` now serves these first — "
            "re-score with --rescore; each re-judged job leaves the set.[/dim]")

if __name__ == "__main__":
    app()
