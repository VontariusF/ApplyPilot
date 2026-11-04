"""The ApplyPilot portal: FastAPI app factory, routes, SSE streams.

Single-user, localhost-only. The portal reads and writes the same SQLite
store and ~/.applypilot config the CLI uses; long jobs run as subprocesses
(runner.py), progress streams over SSE from a bounded JSONL event log
(apply runs) or a redacted log tail (pipeline runs).
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sse_starlette.sse import EventSourceResponse

from applypilot import config, database, engine_auth, onboarding
from applypilot.apply.launcher import mark_job, reset_failed
from applypilot.llm import ModelListError, list_models
from applypilot.portal import auth, queries, runner
from applypilot.portal import settings as settings_mod
from applypilot.scoring.scorer import run_scoring

PORTAL_DIR = Path(__file__).parent
STATIC_FILES = ("htmx.min.js", "ext-sse.js", "portal.css")

log = logging.getLogger(__name__)

# SSE tuning
POLL_SECONDS = 0.5
IDLE_POLLS_BEFORE_CLOSE = 4  # ~2s with no data after the run ends
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
    token = auth.get_or_create_token()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    templates.env.globals["params_summary"] = _params_summary

    # ------------------------------------------------------------------
    # Auth middleware: every route requires the token; non-localhost Host
    # headers are refused outright (DNS-rebinding defense).
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        if not auth.host_is_allowed(request.headers.get("host")):
            return PlainTextResponse("forbidden: portal is localhost-only", status_code=400)
        provided = auth.extract_token(
            request.cookies.get(auth.COOKIE_NAME),
            request.headers.get("x-portal-token")
            or _bearer(request.headers.get("authorization")),
            request.query_params.get("token"),
        )
        if not auth.token_matches(provided, app.state.token):
            # POST /login carries its token in the form body — pass it
            # through; the route validates it (401 on wrong/missing).
            if request.method == "POST" and request.url.path == "/login":
                return await call_next(request)
            return _unauthorized(request)
        response = await call_next(request)
        # First visit via ?token= — persist the cookie for subsequent requests.
        if request.query_params.get("token"):
            auth.set_token_cookie(response, app.state.token)
        return response

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
    async def jobs_page(request: Request, stage: str = "scored", min_score: int = 7,
                        limit: int = 50, site: str = "", location: str = "",
                        discovered_from: str = "", discovered_to: str = "",
                        apply_status: str = "", remote: str = "",
                        min_salary: str = ""):
        filters = _jobs_filters(stage, min_score, limit, site, location,
                                discovered_from, discovered_to, apply_status,
                                remote, min_salary)
        rows = queries.filtered_jobs(**filters)
        return _render(request, "jobs.html", jobs=rows, total=len(rows),
                       total_jobs=queries.count_jobs(), stages=queries.STAGES,
                       sites=queries.distinct_sites(),
                       statuses=queries.distinct_apply_statuses(), **filters)

    @app.get("/jobs/table", response_class=HTMLResponse)
    async def jobs_table(request: Request, stage: str = "scored", min_score: int = 7,
                         limit: int = 50, site: str = "", location: str = "",
                         discovered_from: str = "", discovered_to: str = "",
                         apply_status: str = "", remote: str = "",
                         min_salary: str = ""):
        filters = _jobs_filters(stage, min_score, limit, site, location,
                                discovered_from, discovered_to, apply_status,
                                remote, min_salary)
        rows = queries.filtered_jobs(**filters)
        return _render(request, "partials/jobs_table.html", jobs=rows,
                       total=len(rows), total_jobs=queries.count_jobs(), **filters)

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
        mark_job(url, status, reason)
        return RedirectResponse(f"/jobs/detail?url={url}", status_code=303)

    @app.post("/jobs/regenerate")
    async def jobs_regenerate(request: Request):
        form = await request.form()
        url = str(form.get("url", "")).strip()
        if not url:
            return PlainTextResponse("invalid regenerate request", status_code=400)
        include_cover = str(form.get("include_cover", "")).strip() in ("1", "on", "true")
        database.reset_job_tailoring(database.get_connection(), url, include_cover=include_cover)
        return RedirectResponse(f"/jobs/detail?url={url}", status_code=303)

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
        run_scoring(url=url)
        return RedirectResponse(f"/jobs/detail?url={url}", status_code=303)

    @app.post("/jobs/rescore-all")
    async def jobs_rescore_all():
        """Re-score every described job as a tracked subprocess."""
        runner.start_run("rescore", {})
        return RedirectResponse("/runs", status_code=303)

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
                       error=request.query_params.get("error"))

    @app.post("/runs/start")
    async def runs_start(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        kind = form.pop("kind", "")
        params = {k: v for k, v in form.items() if v}
        try:
            runner.start_run(kind, params)
        except runner.ParamError as e:
            return RedirectResponse(f"/runs?error={quote(str(e))}", status_code=303)
        return RedirectResponse("/runs", status_code=303)

    @app.post("/runs/cancel/{run_id}")
    async def runs_cancel(run_id: str):
        runner.cancel_run(run_id)
        return RedirectResponse("/runs", status_code=303)

    @app.get("/runs/{run_id}/log", response_class=HTMLResponse)
    async def run_log(request: Request, run_id: str):
        run = runner.get_run(run_id)
        if run is None:
            return PlainTextResponse("run not found", status_code=404)
        backfill = _read_log_tail(Path(run["log_path"]))
        return _render(request, "run_log.html", run=run,
                       backfill=escape(backfill),
                       redaction_note=settings_mod.describe_redaction())

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
    # Agent sign-in (onboarding step 1): the engine gate cards. Status
    # checks run per request; HTMX polls /engine/status every 2s until
    # the signed-in flip or the polling window expires.
    # ------------------------------------------------------------------
    @app.get("/engine", response_class=HTMLResponse)
    async def engine_page(request: Request, t0: float | None = None,
                          login: int = 0, login_error: str = ""):
        ctx = _engine_gate_context(t0, bool(login))
        return _render(request, "engine.html", login_error=login_error, **ctx)

    @app.get("/engine/status", response_class=HTMLResponse)
    async def engine_status(request: Request, t0: float | None = None, login: int = 0):
        ctx = _engine_gate_context(t0, bool(login))
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
                       **_onboarding_context(error=error, login_error=login_error,
                                             login=login, t0=t0))

    @app.get("/onboarding/chat", response_class=HTMLResponse)
    async def onboarding_chat(request: Request, error: str = ""):
        return _render(request, "partials/onboarding_surface.html",
                       **_onboarding_context(error=error))

    @app.get("/onboarding/stream")
    async def onboarding_stream():
        baseline = _chat_signature()  # snap at connect; pushes are change-only
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
                       **_onboarding_context(error=error))

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
                       **_onboarding_context(error=error))

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
                       **_onboarding_context(error=error))

    @app.post("/onboarding/confirm")
    async def onboarding_confirm(request: Request):
        error = ""
        try:
            await asyncio.to_thread(onboarding.confirm_profile)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **_onboarding_context(error=error))

    @app.post("/onboarding/find-jobs")
    async def onboarding_find_jobs():
        """Find-jobs-now handoff (spec journey step 5): kick off discovery.

        The confirm/done card offers this; the run goes through the same
        subprocess runner as the Runs page, so progress streams there too.
        """
        runner.start_run("pipeline", {})
        return RedirectResponse("/runs", status_code=303)

    @app.post("/onboarding/keep-editing")
    async def onboarding_keep_editing(request: Request):
        try:
            await asyncio.to_thread(onboarding.keep_editing)
        except onboarding.OnboardingError as exc:
            return _render(request, "partials/onboarding_surface.html",
                           **_onboarding_context(error=str(exc)))
        return _render(request, "partials/onboarding_surface.html",
                       **_onboarding_context())

    @app.post("/onboarding/redo")
    async def onboarding_redo(request: Request):
        error = ""
        try:
            await asyncio.to_thread(onboarding.redo_interview)
        except onboarding.OnboardingError as exc:
            error = str(exc)
        return _render(request, "partials/onboarding_surface.html",
                       **_onboarding_context(error=error))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0):
        return _render(request, "settings.html", fields=settings_mod.masked_view(),
                       saved=bool(saved), model_choices=_fetch_model_choices())

    @app.post("/settings")
    async def settings_save(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        settings_mod.apply_settings(form)
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

def _jobs_filters(stage: str, min_score: int, limit: int, site: str,
                  location: str, discovered_from: str, discovered_to: str,
                  apply_status: str, remote: str, min_salary: str) -> dict:
    """Validate + normalize the jobs-board query params into filtered_jobs kwargs.

    Unknown stages snap to "scored", the salary bound parses to a float
    (None = filter off, unparseable input ignored), and anything else out
    of range falls back to its no-op value — filter forms must never 422.
    """
    return {
        "stage": stage if stage in queries.STAGES else "scored",
        "min_score": max(min(min_score, 10), 0),
        "limit": min(max(limit, 1), 500),
        "site": site.strip(),
        "location": location.strip(),
        "discovered_from": discovered_from.strip(),
        "discovered_to": discovered_to.strip(),
        "apply_status": apply_status.strip(),
        "remote": remote.strip() if remote.strip() in queries.REMOTE_VALUES else "",
        "min_salary": _opt_float(min_salary),
    }


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


def _params_summary(params_json: str) -> str:
    """Compact human string of a run's stored params for the table."""
    try:
        params = json.loads(params_json)
    except (TypeError, json.JSONDecodeError):
        return params_json or ""
    if not params:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _active_stream_run() -> dict | None:
    """Latest running run (apply runs preferred for the workers panel)."""
    running = runner.running_runs()
    if not running:
        return None
    for run in running:
        if run["kind"] == "apply":
            return run
    return running[0]


def _engine_gate_context(t0: float | None, login_started: bool) -> dict:
    """Shared context for the agent sign-in cards (page render and polls).

    `t0` is the epoch the current polling window started; it rides through
    every poll URL so the timeout is judged against one consistent start.
    """
    claude = engine_auth.claude_status()
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


def _onboarding_context(error: str = "", login_error: str = "", login: int = 0,
                        t0: float | None = None) -> dict:
    """Shared context for the onboarding chat partial (page, posts, SSE).

    `login_error`/`login`/`t0` are the engine-login outcome params — the
    same contract every gate page parses. SSE re-renders omit them: the
    waiting hint then survives only until the signature changes, and the
    only in-wait signature change is the sign-in flip itself, which swaps
    the gate card for the ready card anyway.
    """
    state = onboarding.load_state()
    ready = onboarding.readiness()
    in_review = state is not None and state.phase == onboarding.REVIEW
    profile_md = ""
    if in_review and state.profile_draft:
        profile_md = onboarding.render_profile_md(
            state.profile_draft, state.profile_draft.get("_applypilot_search", {}))
    login_process = engine_auth.active_login()
    return {
        "state": state,
        "ready": ready,
        "provider_ready": onboarding.provider_ready(),
        "missing": onboarding.missing_review_fields(state.profile_draft) if in_review else [],
        "profile_md": profile_md,
        "error": error,
        "login_error": login_error,
        "login_started": bool(login) and not ready["can_start_claude"],
        "login_url": login_process.oauth_url() if login_process is not None else None,
    }


def _chat_html() -> str:
    """Render the chat partial for the SSE stream (no request context)."""
    return templates.get_template("partials/onboarding_chat.html").render(
        **_onboarding_context())


def _chat_signature() -> tuple:
    """Cheap fingerprint of the chat surface inputs (state, gate, provider)."""
    state = onboarding.load_state()
    if state is None:
        file_sig = None
    else:
        try:
            mtime = os.path.getmtime(config.ONBOARDING_STATE_PATH)
        except OSError:
            mtime = None
        file_sig = (mtime, state.phase, len(state.messages))
    return (file_sig, onboarding.provider_ready(),
            engine_auth.claude_status().state)


async def _stream_chat(last: tuple) -> AsyncIterator[dict]:
    """SSE generator: the onboarding chat partial, re-pushed on any change.

    The signature watches both the state file (new messages, phase) and
    the gate (sign-in flip, provider config) so a signed-in flip swaps the
    gate card for the interview without a manual reload. The route snaps
    the baseline at connect time and the page renders the current surface
    server-side, so the stream only pushes on CHANGE — reconnects after
    the idle close stay swap-free (identical content would otherwise
    replace the DOM out from under an in-flight click). Idle-closes a few
    seconds after the last change; the next POST re-anchors the DOM.
    """
    idle = 0
    while idle < CHAT_IDLE_POLLS_BEFORE_CLOSE:
        await asyncio.sleep(CHAT_POLL_SECONDS)
        sig = _chat_signature()
        if sig != last:
            last, idle = sig, 0
            yield {"event": "chat", "data": _chat_html()}
        else:
            idle += 1
    yield {"event": "end", "data": "closed"}


def _read_log_tail(path: Path) -> str:
    """Redacted initial log backfill: last BACKFILL_BYTES.

    Missing file -> "". A byte-offset start can split a line; the partial
    leading fragment is dropped.
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
    return settings_mod.redact_for_render(text)


def _read_from(path: Path, offset: int) -> tuple[bytes, int]:
    with open(path, "rb") as f:
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
        for line in _read_log_tail(event_path).splitlines():
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
        workers[int(parsed.get("worker_id", 0))] = parsed
        return True
    if kind == "event":
        events.append(str(parsed.get("msg", "")))
        del events[:-50]  # keep the panel bounded
        return True
    return False
