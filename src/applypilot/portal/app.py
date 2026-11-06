"""The ApplyPilot portal: FastAPI app factory, routes, SSE streams.

Single-user, localhost-only. The portal reads and writes the same SQLite
store and ~/.applypilot config the CLI uses; long jobs run as subprocesses
(runner.py), progress streams over SSE from a bounded JSONL event log
(apply runs) or a redacted log tail (pipeline runs).
"""

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sse_starlette.sse import EventSourceResponse

from applypilot import config, database, engine_auth, onboarding
from applypilot.apply.launcher import ManualMarkConflict, mark_job, reset_failed
from applypilot.llm import ModelListError, list_models
from applypilot.portal import auth, queries, runner
from applypilot.portal import automation as automation_mod
from applypilot.portal import assistant
from applypilot.portal import settings as settings_mod
from applypilot.portal import system_check as system_check_mod
from applypilot.scoring.scorer import run_scoring

PORTAL_DIR = Path(__file__).parent
STATIC_FILES = ("htmx.min.js", "ext-sse.js", "portal.css")

log = logging.getLogger(__name__)

# SSE tuning
POLL_SECONDS = 0.5
IDLE_POLLS_BEFORE_CLOSE = 4  # ~2s with no data after the run ends
# Board feed: tailor/apply subprocesses take minutes — stay connected
# through the quiet stretch (~10 min at POLL_SECONDS), then let the client
# reconnect.
BOARD_IDLE_POLLS_BEFORE_CLOSE = 1200
# Page-level streams (automation cards, jobs progress strip) are ended by
# the client's disconnect, not by idleness: a short idle close plus the
# browser's automatic reconnect opens downtime windows in which a
# scheduler-driven change (a cap pause) is recorded while no stream is
# listening — the reconnect's fresh baseline then swallows it and the page
# stays stale forever. A long bound keeps the page live; the first event
# on (re)connect re-syncs it.
PAGE_IDLE_POLLS_BEFORE_CLOSE = BOARD_IDLE_POLLS_BEFORE_CLOSE
BACKFILL_BYTES = 16 * 1024  # initial tail bound for long logs

# Agent sign-in gate: how long the cards poll `claude auth status` after a
# sign-in attempt before falling back to the terminal-command instructions.
LOGIN_TIMEOUT_SECONDS = 300.0

# Onboarding: resume uploads (PDF or text) are capped before they touch disk.
MAX_RESUME_UPLOAD_BYTES = 5 * 1024 * 1024

# Document preview (spec PR G): kinds served by /jobs/preview + /jobs/download,
# and a read cap so a pathologically large stored file cannot stall a render.
DOCUMENT_KINDS = ("resume", "cover")
MAX_PREVIEW_CHARS = 262_144

# The chat SSE stream polls slower than run streams — each tick runs
# `claude auth status` for the gate card, a real subprocess.
CHAT_POLL_SECONDS = 2.0
CHAT_IDLE_POLLS_BEFORE_CLOSE = 4

templates = Jinja2Templates(directory=str(PORTAL_DIR / "templates"))


def _relative_age(iso: str | None) -> str:
    """Plain-language age of a stored timestamp ("3 days ago"), for cards.

    The board shows how old a listing is, not the raw discovered_at
    timestamp. Unparseable or missing values render empty (the card omits
    the line) — never a fabricated date.
    """
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = max(0.0, datetime.now(timezone.utc).timestamp() - then.timestamp())
    minute, hour, day = 60, 3600, 24 * 3600
    if seconds < minute:
        return "just now"
    if seconds < hour:
        n = int(seconds // minute)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if seconds < day:
        n = int(seconds // hour)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(seconds // day)
    if n < 14:
        return f"{n} day{'s' if n != 1 else ''} ago"
    if n < 60:
        weeks = n // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = max(1, n // 30)
    return f"{months} month{'s' if months != 1 else ''} ago"


def _render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    # Explicit ?theme=light|dark pins the CSS theme (render tests, screenshots);
    # unset follows the OS preference inside portal.css.
    theme = request.query_params.get("theme") or ""
    if theme not in ("light", "dark"):
        theme = ""
    return templates.TemplateResponse(request, name, ctx | {"theme": theme},
                                      status_code=status_code)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Build the portal app against the current config.APP_DIR store."""
    config.ensure_dirs()
    config.load_env()
    database.init_db()
    runner.init_registry()
    runner.sweep_orphans()
    automation_mod.start_scheduler()
    token = auth.get_or_create_token()
    auth.install_access_log_redaction()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    templates.env.globals["run_summary"] = automation_mod.run_summary
    templates.env.filters["age"] = _relative_age

    # ------------------------------------------------------------------
    # Auth middleware: every route requires the token; non-localhost Host
    # headers are refused outright (DNS-rebinding defense). The token is
    # accepted only from the cookie or a header — never the query string,
    # which access logs and browser history record verbatim (F9).
    # APPLIPILOT_PORTAL_DISABLE_AUTH (P2-4) opts the TOKEN check out — the
    # deployed Mac launchd unit runs that way. The host-check above runs
    # regardless: an opted-out portal must never become a LAN-reachable
    # unauthenticated store reader if the bind ever widens.
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        if not auth.host_is_allowed(request.headers.get("host")):
            return PlainTextResponse("forbidden: portal is localhost-only", status_code=400)
        if auth.auth_disabled():
            return await call_next(request)
        provided = auth.extract_token(
            request.cookies.get(auth.COOKIE_NAME),
            request.headers.get("x-portal-token")
            or _bearer(request.headers.get("authorization")),
        )
        if not auth.token_matches(provided, app.state.token):
            # POST /login carries its token in the form body — pass it
            # through; the route validates it (401 on wrong/missing).
            # POST /login/reveal is the login page's own Reveal-token
            # action: no token yet, guarded by the localhost-only host
            # check above, same as the login form.
            if request.method == "POST" and request.url.path in ("/login", "/login/reveal"):
                return await call_next(request)
            return _unauthorized(request)
        return await call_next(request)

    # ------------------------------------------------------------------
    # Auth: POST /login validates the submitted token and sets the cookie.
    # (GET /login unauthenticated is served by the middleware as the 401
    # login form, so it needs no route of its own.)
    # ------------------------------------------------------------------
    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        submitted = str(form.get("token", ""))
        if not auth.token_matches(submitted, app.state.token):
            return PlainTextResponse("invalid portal token", status_code=401)
        response = RedirectResponse("/", status_code=303)
        auth.set_token_cookie(response, app.state.token)
        return response

    @app.post("/login/reveal")
    async def login_reveal(request: Request):
        """Show the portal token on the login page itself.

        The login page must not point the user at a file path, and the
        portal is the one interface — so the token renders here on demand.
        Safe because the middleware's localhost-only host check runs
        before this route, and POST keeps the value out of URLs, browser
        history, and access logs (the same reasoning the F9 sweep applied
        to the login form itself).
        """
        return _render(request, "login.html", revealed_token=app.state.token)

    # ------------------------------------------------------------------
    # Static (whitelisted, auth-covered by the middleware — no mounts)
    # ------------------------------------------------------------------
    @app.get("/static/{name}")
    async def static_file(name: str):
        if name not in STATIC_FILES:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(PORTAL_DIR / "static" / name)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        # Fresh store: onboarding first (spec step 1 — the gate stops
        # redirecting only once profile.json exists).
        if not config.PROFILE_PATH.exists():
            return RedirectResponse("/onboarding", status_code=303)
        stats = database.get_stats()
        outcomes = queries.recent_outcomes()
        active = _active_stream_run()
        dist = stats["score_distribution"]
        max_count = max((count for _, count in dist), default=0)
        return _render(request, "dashboard.html", stats=stats, outcomes=outcomes,
                       active_run=active, max_count=max(max_count, 1))

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request):
        view = _board_view(request.query_params)
        return _render(request, "jobs.html",
                       sites=queries.distinct_sites(),
                       statuses=queries.distinct_apply_statuses(),
                       progress=_jobs_progress(),
                       **await _jobs_board_context_async(view))

    @app.get("/jobs/table", response_class=HTMLResponse)
    async def jobs_table(request: Request):
        view = _board_view(request.query_params)
        return _render(request, "partials/jobs_board.html",
                       **await _jobs_board_context_async(view))

    @app.get("/jobs/detail", response_class=HTMLResponse)
    async def jobs_detail(request: Request, url: str):
        job = queries.job_detail(url)
        if job is None:
            return _render(request, "partials/job_missing.html", url=url, status_code=404)
        # The store keeps one score_reasoning blob ("keywords\nreasoning",
        # scorer.py); the detail page presents the two parts separately.
        keywords, reasoning = queries.split_score_reasoning(job.get("score_reasoning"))
        return _render(request, "partials/job_detail.html", job=job,
                       keywords=keywords, reasoning=reasoning,
                       docs=_document_links(job))

    @app.post("/jobs/mark")
    async def jobs_mark(request: Request):
        form = await request.form()
        url = str(form.get("url", "")).strip()
        status = str(form.get("status", "")).strip()
        reason = str(form.get("reason", "") or "") or None
        if status not in ("applied", "failed") or not url:
            return PlainTextResponse("invalid mark request", status_code=400)
        try:
            mark_job(url, status, reason)
        except ManualMarkConflict as e:
            # Writer coordination (fix 5): a run owns this job. Say so instead
            # of silently overwriting the run's state.
            return PlainTextResponse(str(e), status_code=409)
        return RedirectResponse(f"/jobs/detail?url={quote(url, safe='')}",
                                status_code=303)

    @app.post("/jobs/regenerate")
    async def jobs_regenerate(request: Request):
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("invalid regenerate request", status_code=400)
        include_cover = str(form.get("include_cover", "")).strip() in ("1", "on", "true")
        # P2-4: an active apply run owns this job's documents — resetting
        # tailoring now would unlink the very PDF that run is about to
        # upload. Same refusal as /jobs/apply-now.
        if database.job_apply_in_progress(url):
            return PlainTextResponse(
                automation_mod.APPLY_IN_PROGRESS_MESSAGE, status_code=409)
        database.reset_job_tailoring(database.get_connection(), url, include_cover=include_cover)
        return RedirectResponse(f"/jobs/detail?url={quote(url, safe='')}",
                                status_code=303)

    @app.post("/jobs/tailor-now")
    async def jobs_tailor_now(request: Request):
        """Tailor now (spec Jobs panel): reset this job's tailoring state and
        immediately run a one-job tailor subprocess, tracked in the registry."""
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("invalid tailor-now request", status_code=400)
        include_cover = str(form.get("include_cover", "")).strip() in ("1", "on", "true")
        view = _board_form_view(form)
        hx = request.headers.get("HX-Request")
        try:
            automation_mod.start_single_job_tailor(url, include_cover=include_cover)
        except runner.ParamError as e:
            if hx:
                return _render(request, "partials/jobs_board.html",
                               toast_message=str(e), toast_kind="error",
                               **await _jobs_board_context_async(view))
            return RedirectResponse(f"/jobs?error={quote(str(e))}", status_code=303)
        if hx:
            # Stay on the board: the card shows a busy state and the SSE feed
            # refreshes it when documents are ready.
            return _render(request, "partials/jobs_board.html",
                           toast_message="Tailoring started — the card updates when documents are ready.",
                           toast_kind="info", **await _jobs_board_context_async(view))
        return RedirectResponse(f"/jobs/detail?url={quote(url, safe='')}",
                                status_code=303)

    @app.post("/jobs/apply-now")
    async def jobs_apply_now(request: Request):
        """Apply now (spec Jobs panel): start a one-job apply run for this url.

        The confirm modal lists the documents that will be submitted; this
        route refuses to start without a tailored resume — the card offers
        Tailor now instead. Never submits anything silently.
        """
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("invalid apply-now request", status_code=400)
        view = _board_form_view(form)
        hx = request.headers.get("HX-Request")
        job = queries.job_detail(url)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        # Writer coordination (fix 3): a run owns this job (apply_status is
        # in_progress until the worker records its result). Starting another
        # run would exit 'Queue empty' having done nothing — refuse honestly.
        if job.get("apply_status") == "in_progress":
            message = automation_mod.APPLY_IN_PROGRESS_MESSAGE
            if hx:
                return _render(request, "partials/jobs_board.html",
                               toast_message=message, toast_kind="error",
                               status_code=409,
                               **await _jobs_board_context_async(view))
            return PlainTextResponse(message, status_code=409)
        if not job.get("tailored_resume_path"):
            message = "This job has no tailored documents yet — run Tailor now first."
            if hx:
                return _render(request, "partials/jobs_board.html",
                               toast_message=message, toast_kind="error",
                               **await _jobs_board_context_async(view))
            return RedirectResponse(f"/jobs?error={quote(message)}", status_code=303)
        try:
            # F-2: manual starts acquire the active-run guard — a second
            # click must never spawn a concurrent run past the cap.
            runner.start_guarded("apply", {"url": url})
        except runner.ParamError as e:
            if hx:
                return _render(request, "partials/jobs_board.html",
                               toast_message=str(e), toast_kind="error",
                               **await _jobs_board_context_async(view))
            return RedirectResponse(f"/jobs?error={quote(str(e))}", status_code=303)
        if hx:
            return _render(request, "partials/jobs_board.html",
                           toast_message="Applying now — the card updates when the application finishes.",
                           toast_kind="info", **await _jobs_board_context_async(view))
        return RedirectResponse(f"/jobs/detail?url={quote(url, safe='')}",
                                status_code=303)

    @app.post("/jobs/dismiss")
    async def jobs_dismiss(request: Request):
        """Dismiss a job from the board (spec Jobs panel): a curation flag
        with a timestamp, never a pipeline stage. The card leaves the default
        view into the collapsed Dismissed section, which offers Undo."""
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("invalid dismiss request", status_code=400)
        if not database.set_job_dismissed(database.get_connection(), url,
                                          dismissed=True):
            return PlainTextResponse("job not found", status_code=404)
        if request.headers.get("HX-Request"):
            return _render(request, "partials/jobs_board.html",
                           toast_message="Job dismissed — find it in the Dismissed section below if that was a mistake.",
                           toast_kind="info",
                           **await _jobs_board_context_async(_board_form_view(form)))
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/undismiss")
    async def jobs_undismiss(request: Request):
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("invalid undismiss request", status_code=400)
        if not database.set_job_dismissed(database.get_connection(), url,
                                          dismissed=False):
            return PlainTextResponse("job not found", status_code=404)
        if request.headers.get("HX-Request"):
            return _render(request, "partials/jobs_board.html",
                           toast_message="Job restored to the board.",
                           toast_kind="info",
                           **await _jobs_board_context_async(_board_form_view(form)))
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/reset-failed")
    async def jobs_reset_failed():
        reset_failed()
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/rescore")
    async def jobs_rescore(request: Request):
        """Re-score one job inline (a single LLM call) and show the result.

        Scoring failures land in the job's score_error and render as a
        distinct failed state on the detail page.
        """
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("missing url", status_code=400)
        # The scoring call is a blocking LLM HTTP request (up to ~240s with
        # retries) — off the event loop, or every portal request stalls
        # behind it. Same pattern as the onboarding routes.
        result = await asyncio.to_thread(run_scoring, url=url) or {}
        if request.headers.get("HX-Request"):
            # Inline feedback (spec Jobs panel): the fit-assessment panel
            # itself re-renders with the fresh score instead of a silent
            # full-page redirect.
            job = queries.job_detail(url) or {}
            keywords, reasoning = queries.split_score_reasoning(job.get("score_reasoning"))
            # Portal sweep P3-3: a no-op rescore (no full description, or
            # every scoring call failed) used to render the same "just
            # rescored" state as a success. Say what happened instead.
            rescore_error = ""
            if result.get("errors") and not result.get("scored"):
                rescore_error = (
                    "Rescore did not score this job — it has no full description "
                    "to assess, or the scoring call failed. Details are in the "
                    "job's score error below.")
            return _render(request, "partials/fit_assessment.html", job=job,
                           keywords=keywords, reasoning=reasoning,
                           rescored=not rescore_error,
                           rescore_error=rescore_error)
        return RedirectResponse(f"/jobs/detail?url={quote(url, safe='')}",
                                status_code=303)

    @app.post("/jobs/rescore-all")
    async def jobs_rescore_all():
        """Re-score every described job as a tracked subprocess.

        Back to the board: the progress strip (same SSE partial pattern
        as the Automation cards) shows live counts — no dead-end
        redirect to /runs.
        """
        # F-2: guarded like every manual start; the refusal surfaces as an
        # error redirect instead of an unhandled 500.
        try:
            runner.start_guarded("rescore", {})
        except runner.ParamError as e:
            return RedirectResponse(f"/jobs?error={quote(str(e))}",
                                    status_code=303)
        return RedirectResponse("/jobs", status_code=303)

    # ------------------------------------------------------------------
    # Document preview / download / version history (spec PR G)
    # ------------------------------------------------------------------
    @app.get("/jobs/preview", response_class=HTMLResponse)
    async def jobs_preview(request: Request, url: str, doc: str = "resume"):
        """In-page preview of a job's generated document (spec PR G).

        Renders the stored text with preserved line breaks; download links
        cover the exact bytes (.txt and, when present, the PDF sibling).
        """
        job = queries.job_detail(url)
        if job is None:
            return _render(request, "partials/job_missing.html", url=url, status_code=404)
        if doc not in DOCUMENT_KINDS:
            return PlainTextResponse("unknown document kind", status_code=404)
        path = _document_path(job, doc)
        pdf_exists = path is not None and _pdf_sibling(path).is_file()
        if path is None or not path.is_file():
            return _render(request, "preview.html", job=job, doc=doc, missing=True,
                           text="", pdf_exists=False, status_code=404)
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_PREVIEW_CHARS]
        return _render(request, "preview.html", job=job, doc=doc, missing=False,
                       text=text, pdf_exists=pdf_exists, docs_path=str(path))

    @app.get("/jobs/download")
    async def jobs_download(url: str, doc: str = "resume", format: str = "txt"):
        """Stream the job's stored document as an attachment (localhost only).

        The path always comes from the job row, and _document_path confines
        it to the two document output dirs — no request-controlled paths.
        `format=pdf` serves the PDF sibling the pipeline generated, when one
        exists; there is never an on-demand conversion.
        """
        job = queries.job_detail(url)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        if doc not in DOCUMENT_KINDS:
            return PlainTextResponse("unknown document kind", status_code=404)
        path = _document_path(job, doc)
        if path is None or not path.is_file():
            return PlainTextResponse("document not found", status_code=404)
        if format == "pdf":
            path = _pdf_sibling(path)
            if not path.is_file():
                return PlainTextResponse("no PDF was generated for this document",
                                         status_code=404)
        return FileResponse(path, filename=path.name,
                            headers={"Cache-Control": "no-store"})

    @app.get("/jobs/versions", response_class=HTMLResponse)
    async def jobs_versions(request: Request, url: str):
        """Per-job version history page (spec PR G, D2)."""
        job = queries.job_detail(url)
        if job is None:
            return _render(request, "partials/job_missing.html", url=url, status_code=404)
        versions = database.get_document_versions(job_url=url)
        current = {
            database.KIND_RESUME: job.get("tailored_resume_path"),
            database.KIND_COVER_LETTER: job.get("cover_letter_path"),
        }
        return _render(request, "versions.html", job=job, versions=versions,
                       current=current,
                       snapshot_ids={
                           "resume": job.get("applied_resume_version_id"),
                           "cover_letter": job.get("applied_cover_version_id"),
                       })

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request):
        return _render(request, "runs.html", runs=runner.list_runs(),
                       error=request.query_params.get("error"),
                       notice=request.query_params.get("notice"),
                       scheduler_skip=automation_mod.load()["state"].get("scheduler_skip"))

    @app.post("/runs/start")
    async def runs_start(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        kind = form.pop("kind", "")
        # Optional landing page: the jobs board sends next=/jobs so its
        # progress strip takes over instead of a dead-end /runs redirect.
        next_ = form.pop("next", "")
        target = next_ if next_.startswith("/") and not next_.startswith("//") \
            else "/runs"
        params = {k: v for k, v in form.items() if v}
        try:
            # F-2: the manual-start guard — a double click must not spawn a
            # concurrent run that together blows past the daily cap.
            runner.start_guarded(kind, params)
        except runner.ParamError as e:
            if request.headers.get("HX-Request"):
                # In-context failure feedback on the board itself (the empty
                # state's Find-jobs button), not a dead-end /runs redirect.
                return _render(request, "partials/jobs_board.html",
                               toast_message=str(e), toast_kind="error",
                               **await _jobs_board_context_async(_board_form_view(form)))
            return RedirectResponse(f"{target}?error={quote(str(e))}",
                                    status_code=303)
        if request.headers.get("HX-Request") and target == "/runs":
            # In-context discovery (jobs board empty state): the board itself
            # shows the run — no dead-end redirect to /runs. The POST carries
            # no view params, so the default board view renders.
            return _render(request, "partials/jobs_board.html",
                           **await _jobs_board_context_async(_board_form_view(form)))
        return RedirectResponse(target, status_code=303)

    @app.post("/runs/cancel/{run_id}")
    async def runs_cancel(run_id: str):
        # Portal sweep P3-4: cancel_run is False for an already-terminal
        # run — surface the difference instead of redirecting silently
        # (the user cannot otherwise tell "cancelled" from "no-op").
        if runner.cancel_run(run_id):
            return RedirectResponse("/runs?notice=Run+cancelled.",
                                    status_code=303)
        return RedirectResponse(
            "/runs?error=That+run+was+not+active+%E2%80%94+nothing+to+cancel.",
            status_code=303)

    @app.post("/runs/retry/{run_id}")
    async def runs_retry(run_id: str):
        """Re-run a failed run with identical parameters (spec Automation)."""
        try:
            runner.retry_run(run_id)
        except runner.ParamError as e:
            return RedirectResponse(f"/runs?error={quote(str(e))}", status_code=303)
        return RedirectResponse("/runs?notice=Retry+started.", status_code=303)

    @app.get("/runs/{run_id}/log", response_class=HTMLResponse)
    async def run_log(request: Request, run_id: str):
        run = runner.get_run(run_id)
        if run is None:
            return PlainTextResponse("run not found", status_code=404)
        backfill = _read_log_tail(Path(run["log_path"]))
        return _render(request, "run_log.html", run=run,
                       backfill=escape(backfill),
                       redaction_note=settings_mod.describe_redaction())

    @app.get("/events/board")
    async def board_events():
        """SSE feed for the jobs board (spec Jobs panel).

        Emits `board-changed` whenever anything visible on the board may
        have changed: runs starting/stopping (a single-job tailor/apply run
        finishing means documents are ready) or the job store growing.
        Clients refetch the board on the event. One event is emitted at
        connect so a reconnecting client refetches the current board: a
        change recorded while the stream was down must not be swallowed by
        the reconnect's fresh baseline. Closes after a quiet window;
        the browser's EventSource reconnects automatically.
        """

        async def gen() -> AsyncIterator[dict]:
            last = await asyncio.to_thread(_board_snapshot)
            idle = 0
            yield {"event": "board-changed", "data": "changed"}
            while idle < BOARD_IDLE_POLLS_BEFORE_CLOSE:
                await asyncio.sleep(POLL_SECONDS)
                snapshot = await asyncio.to_thread(_board_snapshot)
                if snapshot != last:
                    last = snapshot
                    idle = 0
                    yield {"event": "board-changed", "data": "changed"}
                else:
                    idle += 1
            yield {"event": "end", "data": "closed"}

        # ping keeps the connection warm and — more importantly — makes the
        # response write on a fixed cadence, so a client that vanished
        # (page navigated away, test teardown) is detected when the write
        # fails and its generator is cancelled. Without it an abandoned
        # stream keeps polling the board DB every POLL_SECONDS for the
        # full quiet window (1200 polls ~ 10 minutes).
        return EventSourceResponse(gen(), ping=5)

    @app.get("/runs/{run_id}/logstream")
    async def run_logstream(run_id: str, backfill: int = 1):
        run = runner.get_run(run_id)
        if run is None:
            return PlainTextResponse("run not found", status_code=404)
        return EventSourceResponse(_stream_log_lines(
            Path(run["log_path"]),
            lambda: runner.get_run(run_id),
            include_backfill=bool(backfill),
        ))

    @app.get("/runs/{run_id}/stream")
    async def run_stream(run_id: str):
        run = runner.get_run(run_id)
        if run is None:
            return PlainTextResponse("run not found", status_code=404)
        if run["event_path"]:
            return EventSourceResponse(
                _stream_events(Path(run["event_path"]), lambda: runner.get_run(run_id))
            )
        # Pipeline runs stream their (redacted) console output instead.
        return EventSourceResponse(_stream_log_lines(
            Path(run["log_path"]), lambda: runner.get_run(run_id)
        ))

    # ------------------------------------------------------------------
    # Automation (spec Automation UI): three preset cards, the auto-apply
    # and schedule toggles, and a push channel for the cards. Every action
    # goes through automation.start_preset/stop_preset/update — the same
    # service functions the ask-the-agent panel uses.
    # ------------------------------------------------------------------
    @app.get("/automation", response_class=HTMLResponse)
    async def automation_page(request: Request):
        return _render(request, "automation.html",
                       **_automation_context(error=request.query_params.get("error", "")))

    @app.post("/automation/start")
    async def automation_start(request: Request):
        form = await request.form()
        try:
            automation_mod.start_preset(str(form.get("preset", "")))
        except runner.ParamError as e:
            return RedirectResponse(f"/automation?error={quote(str(e))}", status_code=303)
        return RedirectResponse("/automation", status_code=303)

    @app.post("/automation/stop")
    async def automation_stop(request: Request):
        form = await request.form()
        try:
            automation_mod.stop_preset(str(form.get("preset", "")))
        except runner.ParamError as e:
            return RedirectResponse(f"/automation?error={quote(str(e))}", status_code=303)
        return RedirectResponse("/automation", status_code=303)

    @app.post("/automation/toggle")
    async def automation_toggle(request: Request):
        """Flip a master toggle (auto-apply / daily schedule) on or off."""
        form = await request.form()
        name = str(form.get("name", ""))
        if name not in ("auto_apply_enabled", "schedule_enabled"):
            return RedirectResponse("/automation?error=Unknown+toggle",
                                    status_code=303)
        try:
            automation_mod.update({name: str(form.get("value", "")) == "on"})
        except runner.ParamError as e:
            return RedirectResponse(f"/automation?error={quote(str(e))}", status_code=303)
        return RedirectResponse("/automation", status_code=303)

    @app.post("/automation/adjust")
    async def automation_adjust(request: Request):
        """Save one preset's Adjust disclosure (selects/buttons only)."""
        form = await request.form()
        preset = str(form.get("preset", ""))
        try:
            updates = _preset_form_updates(preset, form)
            automation_mod.update({"presets": {preset: updates}})
        except (runner.ParamError, ValueError) as e:
            return RedirectResponse(f"/automation?error={quote(str(e))}", status_code=303)
        return RedirectResponse("/automation", status_code=303)

    @app.post("/automation/ask")
    async def automation_ask(request: Request):
        """One ask-the-agent turn (spec Automation UI).

        The turn runs in a thread like every other agent call; the answer
        lands in the in-memory transcript and the page re-renders it.
        """
        form = await request.form()
        text = str(form.get("text", ""))
        error = ""
        try:
            await asyncio.to_thread(assistant.handle_message, text)
        except assistant.AssistantError as e:
            error = str(e)
        return _render(request, "automation.html",
                       **_automation_context(error=error))

    @app.get("/automation/stream")
    async def automation_stream():
        """Push the automation cards partial when anything on them changes."""
        baseline = _automation_signature()
        return EventSourceResponse(_stream_cards(baseline))

    @app.get("/jobs/progress/stream")
    async def jobs_progress_stream():
        """Push the jobs-board progress strip when run state or count changes."""
        return EventSourceResponse(_stream_progress(_progress_signature()))

    # ------------------------------------------------------------------
    # Agent sign-in (onboarding step 1): the engine gate cards. Status
    # checks run per request; HTMX polls /engine/status every 2s until
    # the signed-in flip or the polling window expires.
    # ------------------------------------------------------------------
    @app.get("/engine", response_class=HTMLResponse)
    async def engine_page(request: Request, t0: float | None = None,
                          login: int = 0, login_error: str = ""):
        ctx = await _engine_gate_context(t0, bool(login))
        return _render(request, "engine.html", login_error=login_error, **ctx)

    @app.get("/engine/status", response_class=HTMLResponse)
    async def engine_status(request: Request, t0: float | None = None, login: int = 0):
        ctx = await _engine_gate_context(t0, bool(login))
        return _render(request, "partials/engine_gates.html", **ctx)

    @app.post("/engine/login")
    async def engine_login(request: Request):
        form = await request.form()
        dest = str(form.get("next", "") or "/engine")
        if dest not in ("/engine", "/onboarding"):  # allowlisted return paths
            dest = "/engine"
        proc, error = engine_auth.start_login()
        if error:
            return RedirectResponse(f"{dest}?login_error={quote(error)}", status_code=303)
        # `claude auth login` blocks for the browser flow, so a process
        # already dead within the grace beat died before sign-in started.
        death = await asyncio.to_thread(engine_auth.login_instant_death, proc)
        if death:
            return RedirectResponse(f"{dest}?login_error={quote(death)}", status_code=303)
        return RedirectResponse(f"{dest}?login=1&t0={time.time():.0f}", status_code=303)

    @app.post("/engine/droid-key")
    async def engine_droid_key(request: Request):
        """Droid's sign-in equivalent: one pasted Factory API key.

        Factory's CLI has no login command — the documented headless
        credential is ``FACTORY_API_KEY``. The key goes through the
        settings write path (0600 in ~/.applypilot/.env) whose live
        refresh flips ``droid_status()`` in this same process, so the
        gate turns ready without a restart. The value is never echoed
        back — not in redirects, bodies, or logs.
        """
        form = await request.form()
        dest = str(form.get("next", "") or "/engine")
        if dest not in ("/engine", "/onboarding"):  # allowlisted return paths
            dest = "/engine"
        key = str(form.get("factory_key", "") or "").strip()
        if not key:
            msg = "Paste your Factory API key — generate one from the Factory settings page."
            return RedirectResponse(f"{dest}?login_error={quote(msg)}", status_code=303)
        settings_mod.apply_settings({"FACTORY_API_KEY": key})
        return RedirectResponse(dest, status_code=303)

    # ------------------------------------------------------------------
    # Onboarding (spec deliverable C): one chat surface carries the
    # agent-account gate, the interview, and profile review. Agent turns
    # are blocking CLI/LLM calls, so every route awaits them in a thread.
    # The SSE stream re-renders the chat partial when the state file or
    # the gate flips — it is the cross-tab/poll freshness channel.
    # ------------------------------------------------------------------
    @app.get("/onboarding", response_class=HTMLResponse)
    async def onboarding_page(request: Request, error: str = "", login_error: str = "",
                              login: int = 0, t0: float | None = None):
        # Parses the engine-login outcome contract exactly as /engine does —
        # ignoring these redirect params is what made a failed sign-in look
        # like a dead button. t0 exists for that contract only; the
        # onboarding surface keeps no timeout state (the SSE channel flips
        # the gate the moment claude_status() does).
        return _render(request, "onboarding.html",
                       **await _onboarding_context(error=error, login_error=login_error,
                                             login=login, t0=t0))

    @app.get("/onboarding/chat", response_class=HTMLResponse)
    async def onboarding_chat(request: Request, error: str = ""):
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.get("/onboarding/stream")
    async def onboarding_stream():
        # Snap the signature at connect. The stream's FIRST event re-syncs
        # the page to the present — covering changes that landed while no
        # stream listened — later events are change-only.
        baseline = await _chat_signature()
        return EventSourceResponse(_stream_chat(baseline))

    @app.post("/onboarding/start")
    async def onboarding_start(request: Request):
        form = await request.form()
        mode = str(form.get("mode", "claude"))
        error = ""
        try:
            await asyncio.to_thread(onboarding.start_interview, mode)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/send")
    async def onboarding_send(request: Request):
        form = await request.form()
        text = str(form.get("text", ""))
        error = ""
        try:
            await asyncio.to_thread(onboarding.send_message, text)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/answer")
    async def onboarding_answer(request: Request):
        """One guided answer: record it, advance the agenda, ack via agent."""
        form = await request.form()
        error = ""
        try:
            await asyncio.to_thread(
                onboarding.record_answer,
                str(form.get("question_id", "")),
                str(form.get("value", "") or ""),
                str(form.get("free_text", "") or ""),
                str(form.get("skip", "")).strip() in ("1", "on", "true"),
            )
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/clear")
    async def onboarding_clear(request: Request):
        """Clear one resume-sourced chip: the extracted value leaves the
        draft, the field returns to the review screen as add-later."""
        form = await request.form()
        error = ""
        try:
            await asyncio.to_thread(onboarding.clear_prefill, str(form.get("question_id", "")))
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/agent-mode")
    async def onboarding_agent_mode(request: Request):
        """'Let the agent ask' ↔ back to guided buttons (session mode)."""
        form = await request.form()
        enabled = str(form.get("enabled", "")).strip() in ("1", "on", "true")
        error = ""
        try:
            await asyncio.to_thread(onboarding.set_agent_led, enabled)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/resume")
    async def onboarding_resume(request: Request):
        form = await request.form()
        upload = form.get("resume_file")
        pasted = str(form.get("resume_text", "") or "")
        error = ""
        try:
            if upload is not None and getattr(upload, "filename", ""):
                data = await upload.read()
                if len(data) > MAX_RESUME_UPLOAD_BYTES:
                    error = (f"That file is {len(data) // (1024 * 1024)} MB — resumes are "
                             f"capped at {MAX_RESUME_UPLOAD_BYTES // (1024 * 1024)} MB.")
                elif upload.filename.lower().endswith(".pdf"):
                    await asyncio.to_thread(
                        onboarding.add_resume, None, data, upload.filename)
                else:
                    # Text uploads (txt/md) are readable by the agent, like a paste.
                    await asyncio.to_thread(
                        onboarding.add_resume,
                        data.decode("utf-8", errors="replace"), None, upload.filename)
            elif pasted.strip():
                await asyncio.to_thread(
                    onboarding.add_resume, pasted, None, "pasted resume")
            else:
                error = "Choose a file or paste your resume text first."
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/review")
    async def onboarding_review(request: Request):
        """Save review-form edits: quiet corrections, no agent turn."""
        form = await request.form()
        updates = {key.removeprefix("answer_"): str(value)
                   for key, value in form.items() if key.startswith("answer_")}
        error = ""
        try:
            await asyncio.to_thread(onboarding.update_answers, updates)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/confirm")
    async def onboarding_confirm(request: Request):
        error = ""
        try:
            await asyncio.to_thread(onboarding.confirm_profile)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    @app.post("/onboarding/find-jobs")
    async def onboarding_find_jobs():
        """Find-jobs-now handoff (spec journey step 5): kick off discovery.

        Lands on the jobs board, where the live progress strip (same SSE
        partial pattern as the Automation cards) shows the run working —
        no dead-end redirect to /runs.
        """
        # F-2: guarded like every manual start; the refusal surfaces as an
        # error redirect on the board instead of an unhandled 500.
        try:
            runner.start_guarded("pipeline", {})
        except runner.ParamError as e:
            return RedirectResponse(f"/jobs?error={quote(str(e))}",
                                    status_code=303)
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/onboarding/keep-editing")
    async def onboarding_keep_editing(request: Request):
        try:
            await asyncio.to_thread(onboarding.keep_editing)
        except onboarding.OnboardingError as exc:
            return _render(request, "partials/onboarding_surface.html",
                           **await _onboarding_context(error=str(exc)))
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context())

    @app.post("/onboarding/redo")
    async def onboarding_redo(request: Request):
        error = ""
        try:
            await asyncio.to_thread(onboarding.redo_interview)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **await _onboarding_context(error=error))

    # ------------------------------------------------------------------
    # System Check — the zero-terminal diagnostic surface (replaces the
    # CLI doctor for portal users). The partial is the re-run target: the
    # Check-again button swaps this list in place, never a page redirect.
    # ------------------------------------------------------------------
    @app.get("/system-check", response_class=HTMLResponse)
    async def system_check_page(request: Request):
        groups = await asyncio.to_thread(system_check_mod.grouped_checks)
        return _render(request, "system_check.html", groups=groups,
                       state_labels=system_check_mod.STATE_LABELS)

    @app.get("/system-check/checks", response_class=HTMLResponse)
    async def system_check_checks(request: Request):
        groups = await asyncio.to_thread(system_check_mod.grouped_checks)
        return _render(request, "partials/system_checks.html", groups=groups,
                       state_labels=system_check_mod.STATE_LABELS)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0, error: str = ""):
        return _render(request, "settings.html", fields=settings_mod.masked_view(),
                       saved=bool(saved), error=error,
                       model_choices=_fetch_model_choices())

    @app.post("/settings")
    async def settings_save(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            settings_mod.apply_settings(form)
        except settings_mod.SettingsParamError as e:
            # In-page error (same pattern as ?saved=): a scripted POST with
            # an invalid CHOICE value used to surface as an unhandled 500.
            return RedirectResponse(f"/settings?error={quote(str(e))}",
                                    status_code=303)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/settings/models")
    async def settings_models():
        """Model ids for the settings datalists (Refresh control source).

        Auth-covered by the middleware like every route. A failed lookup
        returns 502 with an empty list — the browser keeps its existing
        options and the fields remain free text.
        """
        try:
            models = list_models()
        except (ModelListError, RuntimeError) as exc:
            return JSONResponse({"models": [], "error": str(exc)}, status_code=502)
        return JSONResponse({"models": models})

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Plain-language board chips (spec Jobs panel): each maps onto existing
# query surfaces — stage conditions OR'd together (queries._view_condition),
# plus the read-only apply-failed view for failed attempts. No new
# pipeline stages, STAGE_CONDITIONS untouched.
CHIP_STAGES = {
    # "New" is NOT the literal discovered∪pending_detail∪enriched union:
    # discovered's condition is 1=1 (every job is discovered), so the union
    # is degenerate. The semantic is "found, no score outcome yet".
    "new": "new",
    "scored": "scored",
    "ready": "tailored",
    "applied": "applied",
    "attention": "score_failed,apply_failed",
}
CHIP_LABELS = {
    "new": "New",
    "scored": "Scored",
    "ready": "Ready to apply",
    "applied": "Applied",
    "attention": "Needs attention",
}


def _jobs_filters(stage: str = "scored", min_score: str | int = 7,
                  limit: str | int = 50, site: str = "", location: str = "",
                  discovered_from: str = "", discovered_to: str = "",
                  apply_status: str = "", remote: str = "",
                  min_salary: str | float = "", sort: str = "best",
                  chip: str = "") -> dict:
    """Validate + normalize board params into filtered_jobs kwargs.

    Query strings and verb forms both arrive as strings, so numeric
    parsing happens here. Unknown stages snap to "scored", the salary
    bound parses to a float (None = filter off, unparseable input
    ignored), and anything else out of range falls back to its no-op
    value — filter forms must never 422. A chip (plain-language preset)
    ORs together existing stage conditions and takes precedence over the
    single-stage param.
    """
    stages = CHIP_STAGES.get(chip, "")
    score = _opt_int(str(min_score))
    return {
        # Chip views span score-less stages; an empty stage param keeps
        # the min-score bound (which only applies to scored/tailored/
        # applied) from hiding rows that have no score yet.
        "stage": "" if stages else (stage if stage in queries.STAGES else "scored"),
        "min_score": 7 if score is None else max(min(score, 10), 0),
        "limit": min(max(_opt_int(str(limit)) or 50, 1), 500),
        "site": site.strip(),
        "location": location.strip(),
        "discovered_from": discovered_from.strip(),
        "discovered_to": discovered_to.strip(),
        "apply_status": apply_status.strip(),
        "remote": remote.strip() if remote.strip() in queries.REMOTE_VALUES else "",
        "min_salary": _opt_float(str(min_salary)),
        "sort": sort if sort in queries.SORTS else "best",
        "stages": stages,
    }


def _board_snapshot() -> str:
    """Cheap fingerprint of board-relevant state: run lifecycle changes
    (a single-job tailor/apply finishing = documents ready) plus store size
    and the dismissed count (so a dismissal refreshes every open board)."""
    runs = [{"id": r["id"], "kind": r["kind"], "status": r["status"]}
            for r in runner.list_runs(limit=20)]
    return json.dumps({"runs": runs, "jobs": queries.count_jobs(),
                       "dismissed": queries.dismissed_count()}, sort_keys=True)


def _busy_verbs() -> dict[str, str]:
    """url -> verb ("tailor"/"apply") for running single-job runs.

    The board renders a busy state (spinner, no action buttons) on cards
    whose single-job run is still executing; the SSE board feed triggers a
    refresh when the run finishes, so the card flips back automatically.
    """
    busy: dict[str, str] = {}
    for run in runner.running_runs():
        if run.get("kind") not in ("tailor", "apply"):
            continue
        try:
            params = json.loads(run.get("params") or "{}")
        except json.JSONDecodeError:
            continue
        url = str(params.get("url", ""))
        if url:
            busy[url] = str(run["kind"])
    return busy


# Canonical board view params carried through verb POSTs so a dismiss/tailor
# response re-renders the same filtered view (not a reset board).
_BOARD_VIEW_KEYS = ("stage", "min_score", "limit", "site", "location",
                    "discovered_from", "discovered_to", "apply_status",
                    "remote", "min_salary", "sort", "chip", "page")

_FILTER_KEYS = ("stage", "min_score", "limit", "site", "location",
                "discovered_from", "discovered_to", "apply_status",
                "remote", "min_salary", "sort", "chip")


def _normalize_view(raw) -> dict[str, str]:
    """Board view params (query string or verb form) as normalized strings
    with the board defaults filled in."""
    view = {k: str(raw.get(k, "") or "") for k in _BOARD_VIEW_KEYS}
    if not view["stage"].strip():
        view["stage"] = "scored"
    if not view["sort"].strip():
        view["sort"] = "best"
    page = _opt_int(view["page"])
    view["page"] = str(page) if page and page > 0 else "1"
    return view


def _board_view(params) -> dict[str, str]:
    """Board view from a request query string."""
    return _normalize_view(params)


def _board_form_view(form) -> dict[str, str]:
    """Board view carried by a verb form's hidden fields."""
    return _normalize_view(form)


def _board_url(view: dict[str, str], **overrides: str) -> str:
    """A /jobs URL for this view with overrides applied (blank = drop)."""
    merged = {**view, **overrides}
    qs = urlencode({k: v for k, v in merged.items()
                    if str(v) != "" and str(v) != "None"})
    return f"/jobs?{qs}" if qs else "/jobs"


def _board_chip_links(view: dict[str, str]) -> list[dict]:
    """The five plain-language chips; each link toggles that chip on
    (or off when active) and resets to the first page."""
    links = []
    for key, label in CHIP_LABELS.items():
        active = view["chip"] == key
        links.append({
            "key": key,
            "label": label,
            "active": active,
            "href": _board_url(view, chip="" if active else key, page="1"),
        })
    return links


def _board_active_filters(view: dict[str, str]) -> list[dict]:
    """Dismissible chips, one per ACTIVE surviving filter, each linking to
    the same view with that filter removed. min_score sits at 7 by default
    (the board baseline), so it only reads as a chosen filter when the
    user changed it."""
    active: list[dict] = []

    def add(param: str, label: str) -> None:
        active.append({"label": label,
                       "href": _board_url(view, page="1", **{param: ""})})

    if view["site"].strip():
        add("site", f"Board: {view['site'].strip()}")
    if view["location"].strip():
        add("location", f"Location: {view['location'].strip()}")
    if view["apply_status"].strip():
        add("apply_status", f"Status: {view['apply_status'].strip()}")
    salary = _opt_float(view["min_salary"])
    if salary is not None:
        add("min_salary", f"Pay at least {salary:,.0f}+")
    fit = _opt_int(view["min_score"])
    if fit is not None and fit != 7:
        add("min_score", f"Match {fit}/10 or better")
    if view["remote"] == "yes":
        add("remote", "Remote only")
    elif view["remote"] == "no":
        add("remote", "Onsite or hybrid")
    if view["discovered_from"].strip():
        add("discovered_from", f"Found since {view['discovered_from'].strip()}")
    if view["discovered_to"].strip():
        add("discovered_to", f"Found until {view['discovered_to'].strip()}")
    return active


async def _jobs_board_context_async(view: dict[str, str]) -> dict:
    """Board context off the event loop (portal sweep P3-7).

    The build queries SQLite and does the salary filter/sort in Python
    over every matching row — blocking work that used to run inside the
    async routes on every board render and SSE refetch.
    """
    return await asyncio.to_thread(_jobs_board_context, view)


def _jobs_board_context(view: dict[str, str]) -> dict:
    """Context for the jobs-board partial: cards, chips, pagination,
    empty states. Takes the normalized string view."""
    filters = _jobs_filters(**{k: view[k] for k in _FILTER_KEYS})
    count_kwargs = {k: v for k, v in filters.items() if k not in ("limit", "sort")}
    total_matching = queries.count_filtered_jobs(**count_kwargs)
    page_size = filters["limit"]
    total_pages = max(1, math.ceil(total_matching / page_size))
    page = min(max(1, _opt_int(view["page"]) or 1), total_pages)
    rows = queries.filtered_jobs(**filters, offset=(page - 1) * page_size)
    pagination = {
        "page": page,
        "total_pages": total_pages,
        "total_matching": total_matching,
        "prev_url": _board_url(view, page=str(page - 1)) if page > 1 else "",
        "next_url": _board_url(view, page=str(page + 1)) if page < total_pages else "",
    }
    # Discovery-in-progress live strip (spec): the SSE board feed refreshes
    # it, so "found so far" ticks up while the run works.
    discovery_active = any(run.get("kind") == "pipeline"
                           for run in runner.running_runs())
    has_filters = bool(view["chip"]
                       or any(view[k].strip() for k in ("site", "location",
                                                        "apply_status",
                                                        "discovered_from",
                                                        "discovered_to"))
                       or filters["min_salary"] is not None
                       or (filters["min_score"] != 7)
                       or filters["remote"] != "")
    return {
        "jobs": rows,
        "total": len(rows),
        "total_matching": total_matching,
        "total_jobs": queries.count_jobs(),
        "pagination": pagination,
        "chip_links": _board_chip_links(view),
        "active_filters": _board_active_filters(view),
        "view_chip": view["chip"],
        "has_filters": has_filters,
        "discovery_active": discovery_active,
        "busy": _busy_verbs(),
        "dismissed_jobs": queries.dismissed_jobs(),
        "board_qs": urlencode({k: v for k, v in view.items() if v != ""}),
        **filters,
    }


def _opt_int(raw: str) -> int | None:
    """Parse a filter-form integer; blank/garbage means the filter is off."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _opt_float(raw: str) -> float | None:
    """Parse a filter-form float; blank/garbage means the filter is off."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


# ---------------------------------------------------------------------------
# Document preview helpers (spec PR G)
# ---------------------------------------------------------------------------

def _document_field(doc: str) -> str:
    """The jobs-table column holding the kind's current document path."""
    return "tailored_resume_path" if doc == "resume" else "cover_letter_path"


def _under_document_dirs(path: Path) -> bool:
    """True when path resolves inside one of the two document output dirs.

    The preview/download routes derive paths from the store's job row; this
    keeps a tampered row from turning them into an arbitrary-file read.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root) for root in (config.TAILORED_DIR, config.COVER_LETTER_DIR))


def _document_path(job: dict, doc: str) -> Path | None:
    """The job's stored document path for kind 'resume'|'cover', or None.

    Returns None when the job has no document, the kind is unknown, or the
    stored path escapes the document dirs (legacy rows are all inside them).
    """
    if doc not in DOCUMENT_KINDS:
        return None
    raw = job.get(_document_field(doc))
    if not raw:
        return None
    path = Path(raw)
    if not _under_document_dirs(path):
        return None
    return path


def _pdf_sibling(path: Path) -> Path:
    """The PDF sibling of a generated .txt document ({stem}.pdf)."""
    return path.with_suffix(".pdf")


def _document_links(job: dict) -> dict:
    """Per-kind document link context for the detail page.

    Keys are DOCUMENT_KINDS; each entry carries the contained path (None
    when the job has no usable document), whether the file is on disk, and
    whether its PDF sibling exists.
    """
    links: dict[str, dict] = {}
    for doc in DOCUMENT_KINDS:
        path = _document_path(job, doc)
        links[doc] = {
            "path": path,
            "exists": path is not None and path.is_file(),
            "pdf_exists": path is not None and _pdf_sibling(path).is_file(),
            "raw": job.get(_document_field(doc)),
        }
    return links


def _fetch_model_choices(timeout: float = 2.0) -> list[str]:
    """Best-effort model list for the settings datalists; [] when unreachable.

    A short timeout keeps page render snappy when a configured endpoint is
    down; the failure is logged, and the fields degrade to plain text.
    """
    try:
        return list_models(timeout=timeout)
    except (ModelListError, RuntimeError) as exc:
        log.warning("Settings page: model list unavailable: %s", exc)
        return []


def _unauthorized(request: Request) -> HTMLResponse:
    """The 401 body doubles as the token login form for browsers."""
    return _render(request, "login.html", status_code=401)


def _active_stream_run() -> dict | None:
    """Latest running run (apply runs preferred for the workers panel)."""
    running = runner.running_runs()
    if not running:
        return None
    for run in running:
        if run["kind"] == "apply":
            return run
    return running[0]


async def _engine_gate_context(t0: float | None, login_started: bool) -> dict:
    """Shared context for the agent sign-in cards (page render and polls).

    `t0` is the epoch the current polling window started; it rides through
    every poll URL so the timeout is judged against one consistent start.
    """
    # The Claude probe spawns `claude auth status` (10s timeout) — it must
    # never run on the event loop, where one hung CLI would stall the whole
    # portal. The TTL-cached probe shares one subprocess across the
    # engine page's 2s poll, the onboarding surfaces, and the chat stream
    # (P1-1); droid_status is a which()+env check and stays inline.
    claude = await asyncio.to_thread(engine_auth.claude_status_cached)
    droid = engine_auth.droid_status()
    login = engine_auth.active_login()
    now = time.time()
    window_start = t0 if t0 else now
    waiting = claude.state == engine_auth.SIGNED_OUT or droid.state == engine_auth.SIGNED_OUT
    timed_out = waiting and (now - window_start) >= LOGIN_TIMEOUT_SECONDS
    return {
        "claude": claude,
        "droid": droid,
        "t0": window_start,
        "now_ts": now,
        "login_started": login_started,
        "timed_out": timed_out,
        "polling": waiting and not timed_out,
        "login_timeout_min": int(LOGIN_TIMEOUT_SECONDS // 60),
        # The spawned `claude auth login`'s captured OAuth URL, when one has
        # appeared — the headless sign-in path.
        "login_url": login.oauth_url() if login is not None else None,
    }


async def _onboarding_context(error: str = "", login_error: str = "",
                              login: int = 0, t0: float | None = None) -> dict:
    """Shared context for the onboarding chat partial (page, posts, SSE).

    Async so the gate probe runs off the event loop (P1-1). `login_error`/
    `login`/`t0` are the engine-login outcome params — the
    same contract every gate page parses. SSE re-renders omit them: the
    waiting hint then survives only until the signature changes, and the
    only in-wait signature change is the sign-in flip itself, which swaps
    the gate card for the ready card anyway.
    """
    ready = await asyncio.to_thread(onboarding.readiness)
    state = onboarding.load_state()
    # Resume-first entry (spec art_ti3XJuNr): the first surface a ready
    # user sees opens intake — the resume upload — instead of jumping
    # straight into questions. Rendered surfaces are the flow's entry
    # points (page GET, POST re-renders, SSE), so the intake state is
    # opened here once; start_intake is idempotent and a live state in a
    # later phase resumes untouched. Locked-out users keep the gate
    # cards — nothing startable, nothing to upload into.
    if state is None and (ready["can_start_claude"] or ready["can_start_llm"]):
        state = onboarding.start_intake()
    in_review = state is not None and state.phase == onboarding.REVIEW
    login_process = engine_auth.active_login()
    in_interview = state is not None and state.phase == onboarding.INTERVIEW
    agent_led = in_interview and state.mode == onboarding.MODE_AGENT
    # wizard_question: the rendered question carries its present-tense
    # choices (salary_band's bands derive from the captured currency and
    # level); flow logic below keeps using current_question.
    question = onboarding.wizard_question(state) if in_interview and not agent_led else None
    # Salary-band level hint (agent-replies sweep P3): the bands assume
    # mid-level when experience is unrecorded — say so under the prompt.
    question_hint = ""
    if question is not None and question.id == "salary_band":
        question_hint = onboarding.salary_band_hint(
            onboarding.effective_answers(state))
    return {
        "state": state,
        # Render version for the SSE staleness guard (see onboarding_surface).
        "chat_seq": state.seq if state else 0,
        "ready": ready,
        "provider_ready": onboarding.provider_ready(),
        "missing": onboarding.missing_review_fields(state.profile_draft) if in_review else [],
        "review_fields": onboarding.review_fields(state) if in_review else [],
        "question": question,
        "question_hint": question_hint,
        # Resume-sourced chips (spec art_ti3XJuNr): what extraction already
        # answered, shown editable in the interview; and the gaps still
        # open, as needs-answer chips. Empty outside the guided interview.
        "prefilled_answers": onboarding.prefilled_answers(state) if in_interview and not agent_led else [],
        "remaining_gaps": onboarding.remaining_questions(state) if in_interview and not agent_led else [],
        "agent_led": agent_led,
        "error": error,
        "login_error": login_error,
        "login_started": bool(login) and not ready["can_start_claude"],
        "login_url": login_process.oauth_url() if login_process is not None else None,
    }


async def _chat_html() -> str:
    """Render the chat partial for the SSE stream (no request context)."""
    return templates.get_template("partials/onboarding_chat.html").render(
        **await _onboarding_context())


# ---------------------------------------------------------------------------
# Automation page helpers (spec Automation UI)
# ---------------------------------------------------------------------------

def _preset_form_updates(preset: str, form) -> dict:
    """The preset dict an Adjust form submits (validated by automation.update).

    Only the fields the UI actually offers; keep_going is a checkbox, so
    its absence means off.
    """
    if preset == "find_jobs":
        return {"results_level": str(form.get("results_level", ""))}
    if preset == "apply_matches":
        raw_score = str(form.get("min_score", "")).strip()
        raw_cap = str(form.get("daily_cap", "")).strip()
        try:
            min_score = int(raw_score) if raw_score else automation_mod.MIN_SCORE_CHOICES[0]
            daily_cap = int(raw_cap) if raw_cap else automation_mod.DAILY_CAP_CHOICES[1]
        except ValueError as e:
            raise runner.ParamError("score and cap must be numbers") from e
        return {
            "min_score": min_score,
            "daily_cap": daily_cap,
            "keep_going": str(form.get("keep_going", "")).strip() in ("1", "on", "true"),
        }
    if preset == "refresh_scores":
        return {}
    raise runner.ParamError(f"unknown preset {preset!r}")


def _automation_context(error: str = "") -> dict:
    """Shared context for the automation cards partial (page render, posts, SSE)."""
    cfg = automation_mod.load()
    status = automation_mod.cap_status(cfg)
    active = runner.running_runs()
    presets = []
    for name, meta in automation_mod.PRESETS.items():
        presets.append({
            "name": name,
            "kind": meta["kind"],
            "title": meta["title"],
            "description": meta["description"],
            "running": any(r["kind"] == meta["kind"] for r in active),
            "params": cfg["presets"][name],
        })
    return {
        "cfg": cfg,
        "status": status,
        "presets": presets,
        "error": error,
        "transcript": assistant.transcript(),
        "min_score_choices": automation_mod.MIN_SCORE_CHOICES,
        "daily_cap_choices": automation_mod.DAILY_CAP_CHOICES,
        "results_levels": automation_mod.RESULTS_LEVELS,
    }


def _automation_cards_html() -> str:
    """Render the cards partial for the SSE stream (no request context)."""
    return templates.get_template("partials/automation_cards.html").render(
        **_automation_context())


def _automation_signature() -> tuple:
    """Cheap fingerprint of everything the cards display (push on change)."""
    cfg = automation_mod.load()
    return (
        cfg["auto_apply_enabled"],
        cfg["schedule_enabled"],
        json.dumps(cfg["presets"], sort_keys=True),
        json.dumps(cfg["labels"], sort_keys=True),
        json.dumps(cfg["state"], sort_keys=True),
        json.dumps(automation_mod.cap_status(cfg), sort_keys=True),
        tuple(sorted(r["id"] for r in runner.running_runs())),
    )


async def _stream_cards(last: tuple) -> AsyncIterator[dict]:
    """SSE generator: the automation cards partial, re-pushed on any change.

    The first event carries the current cards: a page reconnecting after
    a downtime gap must re-sync to the present, because the reconnect's
    fresh baseline would otherwise swallow any change recorded while no
    stream was listening (a scheduler cap pause). Afterwards the stream
    pushes only changes, so a run start/end reaches every open Automation
    page without a reload.
    """
    yield {"event": "cards", "data": _automation_cards_html()}
    idle = 0
    while idle < PAGE_IDLE_POLLS_BEFORE_CLOSE:
        await asyncio.sleep(POLL_SECONDS)
        sig = _automation_signature()
        if sig != last:
            last, idle = sig, 0
            yield {"event": "cards", "data": _automation_cards_html()}
        else:
            idle += 1
    yield {"event": "end", "data": "closed"}


def _jobs_progress() -> dict | None:
    """Strip context for the jobs board: the active run's plain progress."""
    run = _active_stream_run()
    if run is None:
        return None
    return automation_mod.progress_for_run(run)


def _progress_signature() -> tuple:
    """Cheap fingerprint of the strip inputs (run identity + count)."""
    run = _active_stream_run()
    if run is None:
        return ("idle", None)
    return (run["id"], automation_mod.progress_for_run(run)["count"])


def _progress_html() -> str:
    """Render the jobs-board progress strip partial (no request context)."""
    return templates.get_template("partials/jobs_progress.html").render(
        progress=_jobs_progress())


async def _stream_progress(last: tuple) -> AsyncIterator[dict]:
    """SSE generator: the jobs-board strip, re-pushed on any change.

    Mirrors _stream_cards: pushes only changes, so a run start, count
    tick, or completion reaches every open jobs board without a reload.
    The first event re-syncs a reconnecting page (see _stream_cards).
    """
    yield {"event": "progress", "data": _progress_html()}
    idle = 0
    while idle < PAGE_IDLE_POLLS_BEFORE_CLOSE:
        await asyncio.sleep(POLL_SECONDS)
        sig = _progress_signature()
        if sig != last:
            last, idle = sig, 0
            yield {"event": "progress", "data": _progress_html()}
        else:
            idle += 1
    yield {"event": "end", "data": "closed"}


async def _chat_signature() -> tuple:
    """Cheap fingerprint of the chat surface inputs (state, gate, provider).

    Async: the gate probe may spawn a subprocess, so it runs in a worker
    thread (P1-1) — the SSE generators await this every poll tick.
    """
    state = onboarding.load_state()
    if state is None:
        file_sig = None
    else:
        try:
            mtime = os.path.getmtime(config.ONBOARDING_STATE_PATH)
        except OSError:
            mtime = None
        file_sig = (mtime, state.seq, state.phase, len(state.messages))
    claude_state = (await asyncio.to_thread(engine_auth.claude_status_cached)).state
    return (file_sig, onboarding.provider_ready(), claude_state)


async def _stream_chat(last: tuple) -> AsyncIterator[dict]:
    """SSE generator: the onboarding chat partial, re-pushed on any change.

    The signature watches both the state file (new messages, phase) and
    the gate (sign-in flip, provider config) so a signed-in flip swaps the
    gate card for the interview without a manual reload. The FIRST event
    re-syncs a reconnecting page (see _stream_cards): a reconnect's fresh
    baseline already contains whatever changed while no stream listened
    (the ~8s idle close), so change-only pushes would swallow it. The
    identical-content push this costs on a no-change connect is dropped
    by the page's data-chat-seq guard (same-seq pushes render identical
    state, and dropping them protects in-flight user input). Idle-closes
    a few seconds after the last change; the next POST re-anchors the DOM.
    """
    yield {"event": "chat", "data": await _chat_html()}  # initial sync (P2-2)
    idle = 0
    while idle < CHAT_IDLE_POLLS_BEFORE_CLOSE:
        await asyncio.sleep(CHAT_POLL_SECONDS)
        sig = await _chat_signature()
        if sig != last:
            last, idle = sig, 0
            yield {"event": "chat", "data": await _chat_html()}
        else:
            idle += 1
    yield {"event": "end", "data": "closed"}


def _read_log_tail(path: Path, redact: bool = True) -> str:
    """Initial log backfill: last BACKFILL_BYTES.

    Missing file -> "". A byte-offset start can split a line; the partial
    leading fragment is dropped. Redaction is a display concern (the
    partials escape); callers that parse the lines (the JSONL events
    backfill) pass redact=False — a configured secret rewritten inside a
    JSONL line breaks the parse and silently drops that event.
    """
    if not path.exists():
        return ""
    size = path.stat().st_size
    start = max(0, size - BACKFILL_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        text = f.read().decode("utf-8", errors="replace")
    if start > 0:
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1:]
    return settings_mod.redact_for_render(text) if redact else text


def _read_from(path: Path, offset: int) -> tuple[bytes, int]:
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        if offset > size:
            # S3: the event log rotated mid-stream — the live file
            # restarted from empty, so an offset near the old bound would
            # seek past EOF forever and strand the stream. Restart from 0.
            offset = 0
        f.seek(offset)
        data = f.read()
    return data, offset + len(data)


def _start_offset(path: Path) -> int:
    """Where a backfilled stream starts: skip all but the last BACKFILL_BYTES."""
    if not path.exists():
        return 0
    size = path.stat().st_size
    if size <= BACKFILL_BYTES:
        return 0
    with open(path, "rb") as f:
        f.seek(size - BACKFILL_BYTES)
        # Align to the next newline so no partial line is emitted.
        f.readline()
        return f.tell()


async def _stream_log_lines(path: Path, get_run, include_backfill: bool = True
                            ) -> AsyncIterator[dict]:
    """SSE generator: redacted, HTML-escaped log lines in order.

    Log lines are swapped into innerHTML by htmx, so escape() here is the
    injection boundary for run output.
    """
    redact = settings_mod.redact_for_render
    offset = _start_offset(path) if include_backfill else (
        path.stat().st_size if path.exists() else 0
    )
    if include_backfill and offset:
        for line in _read_log_tail(path).splitlines():
            yield {"event": "log", "data": escape(line)}
    idle = 0
    while idle < IDLE_POLLS_BEFORE_CLOSE:
        run = get_run()
        running = run is not None and run["status"] == "running"
        data = b""
        if path.exists():
            data, offset = await asyncio.to_thread(_read_from, path, offset)
        if data:
            idle = 0
            for line in data.decode("utf-8", errors="replace").splitlines():
                yield {"event": "log", "data": escape(redact(line))}
        else:
            idle = 0 if running else idle + 1
        await asyncio.sleep(POLL_SECONDS)
    yield {"event": "end", "data": "closed"}


async def _stream_events(event_path: Path, get_run) -> AsyncIterator[dict]:
    """SSE generator: structured JSONL events (worker states + events).

    Accumulates latest state per worker and the recent event list, and
    renders the HTML partials the dashboard panels swap in.
    """
    workers: dict[int, dict] = {}
    events: list[str] = []

    def workers_html() -> str:
        ordered = [workers[w] for w in sorted(workers)]
        return templates.get_template("partials/workers.html").render(workers=ordered)

    def events_html() -> str:
        return templates.get_template("partials/events.html").render(events=events)

    offset = _start_offset(event_path) if event_path.exists() else 0
    if offset:
        # Raw lines (portal sweep P3-5): redaction is for display, and a
        # redacted secret inside a JSONL line breaks the parse — the
        # backfilled event would be silently dropped. _absorb_event
        # tolerates junk; the partials escape on render.
        for line in _read_log_tail(event_path, redact=False).splitlines():
            _absorb_event(line, workers, events)
    yield {"event": "state", "data": workers_html()}
    yield {"event": "event", "data": events_html()}
    idle = 0
    while idle < IDLE_POLLS_BEFORE_CLOSE:
        run = get_run()
        running = run is not None and run["status"] == "running"
        data = b""
        if event_path.exists():
            data, offset = await asyncio.to_thread(_read_from, event_path, offset)
        if data:
            idle = 0
            changed = False
            for line in data.decode("utf-8", errors="replace").splitlines():
                if _absorb_event(line, workers, events):
                    changed = True
            if changed:
                yield {"event": "state", "data": workers_html()}
                yield {"event": "event", "data": events_html()}
        else:
            idle = 0 if running else idle + 1
        await asyncio.sleep(POLL_SECONDS)
    yield {"event": "end", "data": "closed"}


def _absorb_event(line: str, workers: dict[int, dict], events: list[str]) -> bool:
    """Fold one JSONL line into the SSE accumulator; True when it changed."""
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return False
    kind = parsed.get("kind")
    if kind == "state":
        try:
            worker = int(parsed.get("worker_id", 0))
        except (TypeError, ValueError):
            # Portal sweep P3-5: a malformed worker_id used to raise
            # ValueError and kill the whole SSE generator. Skip the line;
            # the stream stays up for well-formed events.
            return False
        workers[worker] = parsed
        return True
    if kind == "event":
        events.append(str(parsed.get("msg", "")))
        del events[:-50]  # keep the panel bounded
        return True
    return False
