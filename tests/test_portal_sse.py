"""SSE and event-log tests: ordered JSONL events, rotation bound, redaction.

Spec D acceptance: events appended to JSONL appear on the stream in order;
the bounded file rotates rather than growing unboundedly; log/stream output
carries no configured secrets and no raw HTML.
"""

import asyncio
import json

from kestrel import config
from kestrel.apply import dashboard, eventlog
from kestrel.database import get_connection
from kestrel.portal.app import _stream_log_lines, _stream_events


def collect(agen):
    async def run():
        return [e async for e in agen]
    return asyncio.run(run())


def test_dashboard_hook_writes_jsonl(portal_env, monkeypatch):
    log = portal_env / "runs" / "hook.jsonl"
    monkeypatch.setenv("KESTREL_EVENT_LOG", str(log))
    eventlog.reset_writer_cache()

    dashboard.init_worker(0)
    dashboard.update_state(0, status="applying", job_title="Pwidget Engineer",
                           company="ExampleCorp", score=9)
    dashboard.add_event("[dim]12:00:00[/dim] [W0] Starting: Pwidget Engineer @ ExampleCorp")

    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    kinds = [entry["kind"] for entry in lines]
    assert kinds[0] == "state"  # init emits the starting snapshot
    state = next(e for e in lines if e["kind"] == "state" and e.get("status") == "applying")
    assert state["job_title"] == "Pwidget Engineer"
    assert state["company"] == "ExampleCorp"
    assert state["score"] == 9
    event = next(e for e in lines if e["kind"] == "event")
    assert event["msg"] == "12:00:00 [W0] Starting: Pwidget Engineer @ ExampleCorp"  # markup stripped


def test_no_jsonl_without_env(portal_env):
    dashboard.add_event("quiet run")
    # No writer configured -> nothing written anywhere; just no crash.


def test_event_log_rotates_at_bound(portal_env):
    log = portal_env / "runs" / "rot.jsonl"
    writer = eventlog.EventLogWriter(log, max_bytes=1000)
    for i in range(100):
        writer.write({"kind": "event", "msg": "x" * 40})
    rotated = log.with_suffix(".jsonl.1")
    assert rotated.exists(), "rotation did not happen"
    assert log.stat().st_size < 1200
    # Exactly one generation is kept: rewriting .1 never accumulates files.
    assert list(log.parent.glob("rot.jsonl*")) == [log, rotated]


def test_stream_events_in_order(portal_env, monkeypatch):
    log = portal_env / "runs" / "sse.jsonl"
    writer = eventlog.EventLogWriter(log)
    writer.emit_state({"status": "applying", "job_title": "Job A"}, 0)
    writer.emit_event("first message")
    writer.emit_event("second message")

    events = collect(_stream_events(log, lambda: {"status": "completed"}))
    kinds = [e["event"] for e in events]
    # initial empty panels, then the absorbed data, then close
    assert kinds[0] == "state"
    assert kinds[1] == "event"
    assert "end" in kinds
    event_payloads = [e["data"] for e in events if e["event"] == "event"]
    joined = "\n".join(event_payloads)
    assert "first message" in joined and "second message" in joined
    state_payloads = [e["data"] for e in events if e["event"] == "state"]
    assert "Job A" in "\n".join(state_payloads)
    # Order within the feed: first before second.
    assert joined.index("first message") < joined.index("second message")


def test_stream_events_skips_malformed_lines(portal_env):
    log = portal_env / "runs" / "broken.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"kind": "event", "msg": "good"}\nnot-json-at-all\n', encoding="utf-8")
    events = collect(_stream_events(log, lambda: {"status": "completed"}))
    joined = "\n".join(e["data"] for e in events if e["event"] == "event")
    assert "good" in joined
    assert "not-json-at-all" not in joined


def insert_run(run_id: str, log_path, event_path: str | None, status: str = "completed") -> None:
    from kestrel.portal import runner

    runner.init_registry()
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO runs (id, kind, params, status, pid, pgid, started_at, log_path, event_path) "
        "VALUES (?, 'pipeline', '{}', ?, 123, 123, '2026-01-01', ?, ?)",
        (run_id, status, str(log_path), event_path),
    )
    conn.commit()


def test_log_stream_redacts_secrets_and_escapes_html(portal_env):
    log = portal_env / "runs" / "leaky.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    config.ENV_PATH.write_text("GEMINI_API_KEY=gemini-live-key-abc\n", encoding="utf-8")
    log.write_text(
        "error: request used key gemini-live-key-abc\n"
        "attempted <script>alert('xss')</script> injection\n",
        encoding="utf-8",
    )
    insert_run("sse1", log, None)

    events = collect(_stream_log_lines(log, lambda: {"status": "completed"}))
    datas = [e["data"] for e in events if e["event"] == "log"]
    joined = "\n".join(datas)
    assert "gemini-live-key-abc" not in joined
    assert "[REDACTED]" in joined
    assert "<script>" not in joined          # escaped for innerHTML swaps
    assert "&lt;script&gt;" in joined


def test_stream_route_serves_events_end_to_end(portal_client, portal_env):
    """Full-stack: registry row -> /runs/{id}/stream emits the JSONL events."""
    client, _ = portal_client
    event_file = portal_env / "runs" / "route.jsonl"
    writer = eventlog.EventLogWriter(event_file)
    writer.emit_state({"status": "applying", "job_title": "Route Job"}, 0)
    writer.emit_event("route event one")
    insert_run("sse2", portal_env / "runs" / "route.log", str(event_file),
               status="completed")

    with client.stream("GET", "/runs/sse2/stream") as response:
        assert response.status_code == 200
        body = b"".join(response.iter_bytes())
    text = body.decode("utf-8")
    assert "event: state" in text
    assert "Route Job" in text
    assert "route event one" in text
    assert "event: end" in text


def test_stream_events_recovers_after_rotation(portal_env):
    """S3: a reader mid-stream keeps consuming after the log rotates.

    The reader's offset points near the old file's bound; once the live
    file restarts from empty, seeking there used to read past EOF
    forever and strand the panel for the rest of the run.
    """
    log = portal_env / "runs" / "rotsse.jsonl"
    writer = eventlog.EventLogWriter(log)
    for i in range(10):
        writer.emit_event(f"pre-rotation message {i}")
    old_size = log.stat().st_size

    polls: list[int] = []

    def fake_run():
        polls.append(1)
        if len(polls) == 2:
            # Rotate mid-stream and write into the fresh generation, so
            # the reader's offset now sits past the new file's end.
            eventlog.EventLogWriter(log, max_bytes=1).emit_event("rotation trigger")
            writer.emit_event("post-rotation message")
            return {"status": "running"}
        if len(polls) > 2:
            return {"status": "completed"}
        return {"status": "running"}

    events = collect(_stream_events(log, fake_run))
    payloads = "\n".join(e["data"] for e in events if e["event"] == "event")
    assert "pre-rotation message 0" in payloads  # backfill intact
    assert "post-rotation message" in payloads
    # Sanity: the scenario really leaves the new file smaller than the
    # offset a pre-fix reader holds, i.e. it really strands.
    assert log.stat().st_size < old_size


def test_stream_log_lines_recovers_after_rotation(portal_env):
    """S3: the run-log stream recovers after rotation the same way."""
    log = portal_env / "runs" / "rotlog.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(f"pre-rotation line {i}" for i in range(10)) + "\n")
    old_size = log.stat().st_size

    polls: list[int] = []

    def fake_run():
        polls.append(1)
        if len(polls) == 2:
            rotated = log.with_suffix(".log.1")
            rotated.unlink(missing_ok=True)
            log.rename(rotated)
            log.write_text("post-rotation line\n")
            return {"status": "running"}
        if len(polls) > 2:
            return {"status": "completed"}
        return {"status": "running"}

    events = collect(_stream_log_lines(log, fake_run))
    datas = "\n".join(e["data"] for e in events if e["event"] == "log")
    assert "pre-rotation line 0" in datas
    assert "post-rotation line" in datas
    assert log.stat().st_size < old_size


def test_absorb_event_survives_malformed_worker_id():
    """Portal sweep P3-5: a non-numeric worker_id used to raise ValueError
    inside the SSE generator, killing the whole stream. It is skipped
    instead."""
    from kestrel.portal.app import _absorb_event

    workers, events = {}, []
    assert _absorb_event(
        json.dumps({"kind": "state", "worker_id": "not-a-number"}),
        workers, events) is False
    assert workers == {}
    assert events == []
    # Well-formed events still absorb.
    assert _absorb_event(
        json.dumps({"kind": "state", "worker_id": 1, "status": "applying"}),
        workers, events) is True
    assert workers[1]["status"] == "applying"


def test_read_log_tail_raw_keeps_jsonl_parseable(portal_env, monkeypatch):
    """Portal sweep P3-5: the events backfill parses raw lines — a
    configured secret rewritten inside a JSONL line (display redaction)
    used to break the parse and silently drop the backfilled event."""
    from kestrel.portal import settings as settings_mod
    from kestrel.portal.app import _read_log_tail

    log = portal_env / "runs" / "ev.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    secret = "sk-supersecret-value-1234"
    line = json.dumps({"kind": "event", "msg": f"key={secret}"}) + "\n"
    log.write_text(line, encoding="utf-8")
    monkeypatch.setattr(settings_mod, "collect_secret_values",
                        lambda: [secret])

    raw = _read_log_tail(log, redact=False)
    assert secret in raw
    json.loads(raw.splitlines()[0])  # still parseable

    redacted = _read_log_tail(log, redact=True)
    assert secret not in redacted
