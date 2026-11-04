"""The ApplyPilot portal: FastAPI app factory, routes, SSE streams.

Single-user, localhost-only. The portal reads and writes the same SQLite
store and ~/.applypilot config the CLI uses; long jobs run as subprocesses
(runner.py), progress streams over SSE from a bounded JSONL event log
(apply runs) or a redacted log tail (pipeline runs).
"""

import asyncio
import json
import logging
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

from applypilot import config, database, engine_auth
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

templates = Jinja2Templates(directory=str(PORTAL_DIR / "templates"))


def _render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


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
                        limit: int = 50):
        stage = stage if stage in queries.STAGES else "scored"
        rows = database.get_jobs_by_stage(stage=stage, min_score=min_score,
                                          limit=min(max(limit, 1), 500))
        return _render(request, "jobs.html", jobs=rows, stage=stage,
                       min_score=min_score, limit=limit, stages=queries.STAGES,
                       total=len(rows))

    @app.get("/jobs/table", response_class=HTMLResponse)
    async def jobs_table(request: Request, stage: str = "scored", min_score: int = 7,
                         limit: int = 50):
        stage = stage if stage in queries.STAGES else "scored"
        rows = database.get_jobs_by_stage(stage=stage, min_score=min_score,
                                          limit=min(max(limit, 1), 500))
        return _render(request, "partials/jobs_table.html", jobs=rows, total=len(rows))

    @app.get("/jobs/detail", response_class=HTMLResponse)
    async def jobs_detail(request: Request, url: str):
        job = queries.job_detail(url)
        if job is None:
            return _render(request, "partials/job_missing.html", url=url, status_code=404)
        return _render(request, "partials/job_detail.html", job=job)

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
    async def engine_login():
        proc, error = engine_auth.start_login()
        if error:
            return RedirectResponse(f"/engine?login_error={quote(error)}", status_code=303)
        # `claude auth login` blocks for the browser flow, so a process
        # already dead within the grace beat died before sign-in started.
        death = await asyncio.to_thread(engine_auth.login_instant_death, proc)
        if death:
            return RedirectResponse(f"/engine?login_error={quote(death)}", status_code=303)
        return RedirectResponse(f"/engine?login=1&t0={time.time():.0f}", status_code=303)

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

def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


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
    }


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
