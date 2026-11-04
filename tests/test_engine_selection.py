"""Deliverable B tests: engine selection + Droid adapter.

Covers the acceptance tests from the spec:
  - adapter unit tests with a fake process: command shape, child env,
    event normalization (same update_state/add_event effects and stats keys
    as the Claude parser);
  - RESULT-contract parity: identical assistant text yields an identical
    ApplyResult from both adapters;
  - mcp.json merge/mode handling for Droid's global MCP file;
  - CLI: --engine droid --model custom:gemini-flash reaches the launcher
    verbatim, unknown engine exits with a readable error, doctor reports
    both engine binaries;
  - claude-path regression: the default engine is behaviorally unchanged
    (exact command, stdin delivery, dashboard updates, log files, statuses).

The Droid JSON event fixtures encode the DOCUMENTED-BEHAVIOR assumption
(the real droid CLI was unavailable — see the spike findings doc); the
assertions pin the adapter's normalization contract so the eventual spike
only has to update the fixtures.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import config
from applypilot.apply import dashboard, launcher
from applypilot.apply.claude import ClaudeEngine
from applypilot.apply.droid import DROID_ALLOWED_TOOLS, DroidEngine
from applypilot.apply.engines import make_engine, resolve_engine_name
from applypilot.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_dashboard():
    """Reset the dashboard's module-level state around each test."""
    dashboard._worker_states.clear()
    dashboard._events.clear()
    yield
    dashboard._worker_states.clear()
    dashboard._events.clear()


@pytest.fixture()
def profile_and_job():
    """Profile + tailored resume files + a job dict, all under the test APP_DIR."""
    config.ensure_dirs()
    profile = {
        "personal": {
            "full_name": "Test User",
            "preferred_name": "Test",
            "email": "test@example.com",
            "password": "hunter2-secret",
            "phone": "5551234567",
            "city": "Austin, TX",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Yes",
            "require_sponsorship": "No",
        },
        "compensation": {
            "salary_expectation": "120000",
            "salary_currency": "USD",
        },
    }
    config.PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")

    resume_pdf = config.TAILORED_DIR / "Job_Resume.pdf"
    resume_txt = config.TAILORED_DIR / "Job_Resume.txt"
    resume_pdf.write_bytes(b"%PDF-1.4 fake")
    resume_txt.write_text("Senior engineer with 10 years experience.", encoding="utf-8")

    return {
        "url": "https://jobs.example.com/listing/123",
        "title": "Senior Python Engineer",
        "site": "example",
        "application_url": "https://jobs.example.com/apply/123",
        "fit_score": 9,
        "tailored_resume_path": str(resume_txt),
    }


class FakeProc:
    """Scripted stand-in for the process returned by engine.spawn."""

    def __init__(self, lines, returncode=0, wait_exc=None):
        self.lines = lines
        self.returncode = returncode
        self.wait_exc = wait_exc
        self.pid = 4242
        self.cmd = None
        self.env = None
        self.cwd = None
        self.stdin_writes = []
        self.stdin_closed = False
        # run_job writes the prompt via proc.stdin and reads proc.stdout
        self.stdin = self
        self.stdout = iter(lines)

    def write(self, text):
        self.stdin_writes.append(text)

    def close(self):
        self.stdin_closed = True

    def wait(self, timeout=None):
        if self.wait_exc is not None:
            raise self.wait_exc
        return self.returncode

    def poll(self):
        return self.returncode


def spawn_recorder(proc):
    """Monkeypatch target: engine.spawn that records its args and returns proc."""
    def _spawn(self, cmd, env, cwd):
        proc.cmd = cmd
        proc.env = env
        proc.cwd = cwd
        return proc
    return _spawn


def seed_ready_job(resume_path: str = "/tmp/does-not-matter.pdf"):
    """Insert (or reset) one apply-ready job row."""
    from applypilot.database import get_connection, init_db

    init_db()
    conn = get_connection()
    # OR REPLACE + explicit apply-field resets: the session DB is shared
    # across tests, so each seed starts from a known, apply-ready state.
    conn.execute(
        "INSERT OR REPLACE INTO jobs (url, title, site, fit_score, tailored_resume_path, "
        "apply_status, apply_attempts, applied_at) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL)",
        ("https://jobs.example.com/listing/123", "Senior Python Engineer",
         "example", 9, resume_path),
    )
    conn.commit()


# Stream-json fixtures (Claude shapes; Droid's assumed shapes are the same).
CLAUDE_ASSISTANT_LINE = json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": "Navigating to the application form."},
        {"type": "tool_use", "name": "mcp__playwright__browser_navigate",
         "input": {"url": "https://jobs.example.com/apply/123"}},
    ]},
})
CLAUDE_RESULT_LINE = json.dumps({
    "type": "result",
    "usage": {"input_tokens": 100, "output_tokens": 50,
              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
    "total_cost_usd": 0.012,
    "num_turns": 4,
    "result": "RESULT:APPLIED",
})

# Independent transcription of the 15-tool gmail denylist the original
# run_job passed to --disallowedTools (regression baseline, not imported).
EXPECTED_DENYLIST = ",".join([
    "mcp__gmail__draft_email", "mcp__gmail__modify_email", "mcp__gmail__delete_email",
    "mcp__gmail__download_attachment", "mcp__gmail__batch_modify_emails",
    "mcp__gmail__batch_delete_emails", "mcp__gmail__create_label", "mcp__gmail__update_label",
    "mcp__gmail__delete_label", "mcp__gmail__get_or_create_label", "mcp__gmail__list_email_labels",
    "mcp__gmail__create_filter", "mcp__gmail__list_filters", "mcp__gmail__get_filter",
    "mcp__gmail__delete_filter",
])


# ---------------------------------------------------------------------------
# Engine resolution
# ---------------------------------------------------------------------------

def test_resolve_engine_defaults(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_ENGINE", raising=False)
    assert resolve_engine_name(None) == "claude"
    assert resolve_engine_name("DROID") == "droid"
    assert resolve_engine_name(" claude ") == "claude"


def test_resolve_engine_env_override(monkeypatch):
    monkeypatch.setenv("AUTOAPPLY_ENGINE", "droid")
    assert resolve_engine_name(None) == "droid"
    # The flag wins over the env var.
    assert resolve_engine_name("claude") == "claude"


def test_resolve_engine_unknown_raises(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_ENGINE", raising=False)
    with pytest.raises(ValueError, match="Unknown engine 'claude-code'"):
        resolve_engine_name("claude-code")
    with pytest.raises(ValueError, match="Valid engines: claude, droid"):
        make_engine("nope")


def test_default_engine_is_claude_with_default_config(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_ENGINE", raising=False)
    assert config.DEFAULTS["apply_engine"] == "claude"


# ---------------------------------------------------------------------------
# RESULT contract: shared scan + parity
# ---------------------------------------------------------------------------

PARITY_TEXTS = [
    "Done. RESULT:APPLIED",
    "RESULT:EXPIRED",
    "RESULT:CAPTCHA",
    "RESULT:LOGIN_ISSUE",
    "RESULT:FAILED:stuck",
    "RESULT:FAILED:captcha",
    "RESULT:FAILED:expired.",
    "RESULT:FAILED",
    "all work and no result markers",
    "RESULT:APPLIED\nRESULT:FAILED:later",  # positive marker wins
    "step 1 done\nRESULT:FAILED:cloudflare_blocked",
]


@pytest.mark.parametrize("text", PARITY_TEXTS)
def test_result_contract_parity(text):
    """Identical assistant text yields an identical ApplyResult from both adapters."""
    assert ClaudeEngine().extract_result(text) == DroidEngine().extract_result(text)


def test_scan_result_values():
    from applypilot.apply.engines import scan_result

    assert scan_result("RESULT:APPLIED").status == "applied"
    assert scan_result("RESULT:EXPIRED").status == "expired"
    assert scan_result("RESULT:CAPTCHA").status == "captcha"
    assert scan_result("RESULT:LOGIN_ISSUE").status == "login_issue"
    # FAILED reasons promote to their own status after cleanup...
    assert scan_result("RESULT:FAILED:captcha").status == "captcha"
    # ...but only after marker cleanup ("expired." is not promoted).
    assert scan_result("RESULT:FAILED:expired.").status == "failed:expired."
    assert scan_result("RESULT:FAILED:stuck").status == "failed:stuck"
    assert scan_result("RESULT:FAILED").status == "failed:unknown"
    assert scan_result("no marker here").status == "failed:no_result_line"


# ---------------------------------------------------------------------------
# Claude adapter (regression: behavior unchanged)
# ---------------------------------------------------------------------------

def test_claude_stats_dict_unchanged():
    """The stats dict keeps the exact keys the original parser produced."""
    events = ClaudeEngine().parse_stream(CLAUDE_RESULT_LINE)
    assert len(events) == 1
    assert events[0].kind == "usage"
    assert events[0].stats == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read": 10,
        "cache_create": 5,
        "cost_usd": 0.012,
        "turns": 4,
    }
    assert events[0].text == "RESULT:APPLIED"


def test_claude_parse_stream_events():
    events = ClaudeEngine().parse_stream(CLAUDE_ASSISTANT_LINE)
    assert [e.kind for e in events] == ["text", "action"]
    assert events[0].text == "Navigating to the application form."
    assert events[1].action_desc == "browser_navigate https://jobs.example.com/apply/123"


def test_claude_parse_stream_raw_and_ignored_lines():
    engine = ClaudeEngine()
    assert engine.parse_stream("not json at all") is None
    assert engine.parse_stream("[1, 2]") is None  # non-dict JSON: raw text, no crash
    assert engine.parse_stream(json.dumps({"type": "system", "subtype": "init"})) == []


def test_claude_run_job_regression(profile_and_job, clean_dashboard, monkeypatch):
    """The default (claude) path is behaviorally unchanged: exact command,
    stdin prompt delivery, per-worker MCP config, dashboard effects, logs."""
    job = profile_and_job
    dashboard.init_worker(7)

    proc = FakeProc([CLAUDE_ASSISTANT_LINE, CLAUDE_RESULT_LINE])
    monkeypatch.setattr(ClaudeEngine, "spawn", spawn_recorder(proc))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "x")

    # Capture the built prompt so the stdin payload can be compared exactly.
    captured = {}
    real_build = launcher.prompt_mod.build_prompt

    def spy_build(**kwargs):
        captured["prompt"] = real_build(**kwargs)
        return captured["prompt"]

    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", spy_build)

    status, _ = launcher.run_job(job, port=9229, worker_id=7, model="haiku", dry_run=True)

    assert status == "applied"

    # Command shape — exactly the historical list.
    assert proc.cmd == [
        "claude",
        "--model", "haiku",
        "-p",
        "--mcp-config", str(config.APP_DIR / ".mcp-apply-7.json"),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", EXPECTED_DENYLIST,
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    # Prompt piped through stdin, byte-identical to build_prompt's output;
    # no prompt file written for the stdin-delivery engine.
    assert proc.stdin_writes == [captured["prompt"]]
    assert proc.stdin_closed
    assert not list(config.LOG_DIR.glob("prompt_claude_*"))

    # Child env: Claude's own env markers stripped.
    assert "CLAUDECODE" not in proc.env
    assert "CLAUDE_CODE_ENTRYPOINT" not in proc.env

    # Per-worker MCP config carries the run's CDP port.
    mcp = json.loads((config.APP_DIR / ".mcp-apply-7.json").read_text(encoding="utf-8"))
    assert "--cdp-endpoint=http://localhost:9229" in mcp["mcpServers"]["playwright"]["args"]
    assert "gmail" in mcp["mcpServers"]

    # Dashboard effects: action counted, status/cost updated like before.
    # (The final status emit overwrites last_action — the tool action itself
    # is asserted in the worker-log check below.)
    state = dashboard.get_state(7)
    assert state.actions == 1
    assert state.last_action.startswith("APPLIED")
    assert state.status == "applied"
    assert state.total_cost == pytest.approx(0.012)

    # Logs: worker log header + streamed text + tool line; per-job log written.
    worker_log = (config.LOG_DIR / "worker-7.log").read_text(encoding="utf-8")
    assert "Navigating to the application form." in worker_log
    assert ">> browser_navigate" in worker_log
    job_logs = list(config.LOG_DIR.glob("claude_*_w7_*.txt"))
    assert len(job_logs) == 1
    assert "RESULT:APPLIED" in job_logs[0].read_text(encoding="utf-8")


def test_claude_run_job_failed_reason_flow(profile_and_job, clean_dashboard, monkeypatch):
    """A RESULT:FAILED:<reason> run keeps the failed:<reason> status and events."""
    job = profile_and_job
    dashboard.init_worker(3)

    result_line = json.dumps({
        "type": "result", "usage": {}, "total_cost_usd": 0,
        "num_turns": 1, "result": "RESULT:FAILED:stuck on submit",
    })
    proc = FakeProc([result_line])
    monkeypatch.setattr(ClaudeEngine, "spawn", spawn_recorder(proc))

    status, _ = launcher.run_job(job, port=9222, worker_id=3, model="haiku", dry_run=True)

    assert status == "failed:stuck on submit"
    state = dashboard.get_state(3)
    assert state.status == "failed"
    assert state.last_action.startswith("FAILED: stuck on submit")


def test_claude_run_job_timeout(profile_and_job, clean_dashboard, monkeypatch):
    """A hung child is reported as failed:timeout (hard-coded 300s until the
    apply-timeout remediation lands)."""
    job = profile_and_job
    dashboard.init_worker(5)

    proc = FakeProc([], wait_exc=subprocess.TimeoutExpired(cmd="claude", timeout=300))
    monkeypatch.setattr(ClaudeEngine, "spawn", spawn_recorder(proc))

    status, _ = launcher.run_job(job, port=9222, worker_id=5, model="haiku", dry_run=True)
    assert status == "failed:timeout"


def test_claude_run_job_signal_kill_is_skipped(profile_and_job, clean_dashboard, monkeypatch):
    """A negative return code (killed by signal — Ctrl+C skip) is 'skipped'."""
    job = profile_and_job
    dashboard.init_worker(6)

    proc = FakeProc([], returncode=-9)
    monkeypatch.setattr(ClaudeEngine, "spawn", spawn_recorder(proc))

    status, _ = launcher.run_job(job, port=9222, worker_id=6, model="haiku", dry_run=True)
    assert status == "skipped"


# ---------------------------------------------------------------------------
# Droid adapter
# ---------------------------------------------------------------------------

def test_droid_run_command_shape(profile_and_job, clean_dashboard, monkeypatch, tmp_path):
    """droid exec command: prompt file (-f), --auto medium, JSON output,
    model verbatim (custom:<name> included), single worker dir."""
    job = profile_and_job
    dashboard.init_worker(7)

    monkeypatch.setenv("APPLYPILOT_FACTORY_DIR", str(tmp_path / "factory"))
    monkeypatch.setenv("FACTORY_API_KEY", "fk-test-123")

    proc = FakeProc([CLAUDE_ASSISTANT_LINE, CLAUDE_RESULT_LINE])
    monkeypatch.setattr(DroidEngine, "spawn", spawn_recorder(proc))

    captured = {}
    real_build = launcher.prompt_mod.build_prompt

    def spy_build(**kwargs):
        captured["prompt"] = real_build(**kwargs)
        return captured["prompt"]

    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", spy_build)

    engine = make_engine("droid")
    status, _ = launcher.run_job(
        job, port=9222, worker_id=7, model="custom:gemini-flash", dry_run=True, engine=engine,
    )

    assert status == "applied"

    expected_worker_dir = config.APPLY_WORKER_DIR / "worker-7"
    prompt_file = proc.cmd[proc.cmd.index("-f") + 1]
    assert proc.cmd == [
        "droid", "exec",
        "-f", prompt_file,
        "--auto", "medium",
        "--output-format", "json",
        "--model", "custom:gemini-flash",
        "--enabled-tools", DROID_ALLOWED_TOOLS,
        "--cwd", str(expected_worker_dir),
    ]

    # The prompt reaches droid via the -f file, never stdin.
    assert Path(prompt_file).read_text(encoding="utf-8") == captured["prompt"]
    assert proc.stdin_writes == []

    # Credential flows to the child env only.
    assert proc.env["FACTORY_API_KEY"] == "fk-test-123"
    assert "CLAUDECODE" not in proc.env

    # Global factory mcp.json merged with the run's CDP port.
    factory_mcp = json.loads((tmp_path / "factory" / "mcp.json").read_text(encoding="utf-8"))
    assert "--cdp-endpoint=http://localhost:9222" in factory_mcp["mcpServers"]["playwright"]["args"]

    # Dashboard effects identical to the Claude parser's for equivalent events.
    state = dashboard.get_state(7)
    assert state.actions == 1
    assert state.status == "applied"
    assert state.total_cost == pytest.approx(0.012)

    # Per-job log uses the engine name prefix.
    assert list(config.LOG_DIR.glob("droid_*_w7_*.txt"))


def test_droid_child_env_requires_factory_key(profile_and_job, clean_dashboard, monkeypatch):
    job = profile_and_job
    dashboard.init_worker(2)

    monkeypatch.setenv("APPLYPILOT_FACTORY_DIR", "/tmp/ap-test-factory")
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)

    proc = FakeProc([])
    monkeypatch.setattr(DroidEngine, "spawn", spawn_recorder(proc))

    # A missing credential is a config error: it raises from run_job's
    # setup section (like build_prompt's missing-resume errors), before
    # any spawn — the CLI converts it to a readable exit.
    with pytest.raises(RuntimeError, match="FACTORY_API_KEY"):
        launcher.run_job(job, port=9222, worker_id=2, model="m", dry_run=True,
                         engine=make_engine("droid"))
    assert proc.cmd is None  # spawn never happened


def test_droid_mcp_json_merges_user_servers(tmp_path, monkeypatch):
    factory_dir = tmp_path / "factory"
    factory_dir.mkdir()
    (factory_dir / "mcp.json").write_text(json.dumps({
        "mcpServers": {"user-server": {"command": "uvx", "args": ["thing"]}},
        "customModels": {"gemini": {"baseUrl": "https://x"}},
    }), encoding="utf-8")
    os.chmod(factory_dir / "mcp.json", 0o644)

    engine = DroidEngine(factory_dir=factory_dir)
    engine.setup(worker_id=0, cdp_port=9225)

    merged = json.loads((factory_dir / "mcp.json").read_text(encoding="utf-8"))
    assert merged["mcpServers"]["user-server"] == {"command": "uvx", "args": ["thing"]}
    assert merged["customModels"] == {"gemini": {"baseUrl": "https://x"}}
    assert "--cdp-endpoint=http://localhost:9225" in merged["mcpServers"]["playwright"]["args"]
    assert "gmail" in merged["mcpServers"]
    assert stat.S_IMODE((factory_dir / "mcp.json").stat().st_mode) == 0o600


def test_droid_mcp_json_created_with_mode_0600(tmp_path):
    engine = DroidEngine(factory_dir=tmp_path / "fresh-factory")
    engine.setup(worker_id=3, cdp_port=9222)
    assert stat.S_IMODE((tmp_path / "fresh-factory" / "mcp.json").stat().st_mode) == 0o600


def test_droid_mcp_json_corrupt_file_refuses(tmp_path):
    factory_dir = tmp_path / "factory"
    factory_dir.mkdir()
    (factory_dir / "mcp.json").write_text("{not json", encoding="utf-8")

    engine = DroidEngine(factory_dir=factory_dir)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        engine.setup(worker_id=0, cdp_port=9222)


def test_droid_parse_stream_tool_call_alternate():
    line = json.dumps({"type": "tool_call", "name": "browser_navigate", "input": {"url": "https://x"}})
    events = DroidEngine().parse_stream(line)
    assert len(events) == 1
    assert events[0].kind == "action"
    assert events[0].action_desc == "browser_navigate https://x"


def test_droid_parse_stream_result_text_alternate():
    line = json.dumps({
        "type": "result", "text": "RESULT:EXPIRED",
        "usage": {"inputTokens": 7},
    })
    events = DroidEngine().parse_stream(line)
    assert events[0].kind == "usage"
    assert events[0].text == "RESULT:EXPIRED"
    assert events[0].stats["input_tokens"] == 7
    assert set(events[0].stats) == {
        "input_tokens", "output_tokens", "cache_read", "cache_create", "cost_usd", "turns",
    }


def test_droid_parse_stream_unknown_falls_back_to_raw():
    engine = DroidEngine()
    assert engine.parse_stream('{"type": "something-else", "payload": 1}') is None
    assert engine.parse_stream("plain text output line") is None
    assert engine.parse_stream("[1, 2]") is None


def test_droid_parse_stream_matches_claude_normalization():
    """Equivalent (assumed) event shapes normalize to the same events and
    stats keys as the Claude parser."""
    droid_events = DroidEngine().parse_stream(CLAUDE_RESULT_LINE)
    claude_events = ClaudeEngine().parse_stream(CLAUDE_RESULT_LINE)
    assert [(e.kind, e.text) for e in droid_events] == [(e.kind, e.text) for e in claude_events]
    assert droid_events[0].stats == claude_events[0].stats


# ---------------------------------------------------------------------------
# CLI: engine selection surfaces
# ---------------------------------------------------------------------------

def _no_tier(*args, **kwargs):
    return None


def test_apply_engine_droid_reaches_launcher(profile_and_job, monkeypatch):
    seed_ready_job()
    monkeypatch.setenv("FACTORY_API_KEY", "fk-test-123")
    monkeypatch.setattr("applypilot.config.check_tier", _no_tier)

    captured = {}

    def fake_main(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("applypilot.apply.launcher.main", fake_main)

    result = runner.invoke(
        app, ["apply", "--engine", "droid", "--model", "custom:gemini-flash", "--limit", "1"],
    )
    assert result.exit_code == 0, result.output
    assert captured["engine"] == "droid"
    assert captured["model"] == "custom:gemini-flash"
    # Default model path is untouched: --model passes through verbatim.
    assert "custom:gemini-flash" in result.output


def test_apply_default_engine_is_claude(profile_and_job, monkeypatch):
    seed_ready_job()
    monkeypatch.delenv("AUTOAPPLY_ENGINE", raising=False)
    monkeypatch.setattr("applypilot.config.check_tier", _no_tier)

    captured = {}

    def fake_main(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("applypilot.apply.launcher.main", fake_main)

    result = runner.invoke(app, ["apply", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert captured["engine"] == "claude"
    assert "claude" in result.output


def test_apply_engine_env_override(profile_and_job, monkeypatch):
    seed_ready_job()
    monkeypatch.setenv("AUTOAPPLY_ENGINE", "droid")
    monkeypatch.setenv("FACTORY_API_KEY", "fk-test-123")
    monkeypatch.setattr("applypilot.config.check_tier", _no_tier)

    captured = {}

    def fake_main(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("applypilot.apply.launcher.main", fake_main)

    result = runner.invoke(app, ["apply", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert captured["engine"] == "droid"


def test_apply_unknown_engine_readable_error(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_ENGINE", raising=False)
    result = runner.invoke(app, ["apply", "--engine", "bogus"])
    assert result.exit_code == 1
    assert "Unknown engine 'bogus'" in result.output
    assert "Valid engines: claude, droid" in result.output


def test_apply_droid_without_factory_key_exits_readable(profile_and_job, monkeypatch):
    seed_ready_job()
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    result = runner.invoke(app, ["apply", "--engine", "droid", "--limit", "1"])
    assert result.exit_code == 1
    assert "FACTORY_API_KEY" in result.output


def test_gen_prompt_prints_engine_specific_commands(profile_and_job, monkeypatch, tmp_path):
    seed_ready_job(resume_path=profile_and_job["tailored_resume_path"])
    monkeypatch.setenv("APPLYPILOT_FACTORY_DIR", str(tmp_path / "factory"))
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)  # --gen works without a key

    from applypilot.apply.launcher import gen_prompt, manual_command

    prompt_file = gen_prompt(
        "https://jobs.example.com/listing/123", min_score=7, model="haiku", engine="claude",
    )
    mcp_path = config.APP_DIR / ".mcp-apply-0.json"
    claude_cmd = manual_command("claude", prompt_file, model="haiku")
    assert claude_cmd == (
        f"claude --model haiku -p --mcp-config {mcp_path} "
        f"--permission-mode bypassPermissions < {prompt_file}"
    )

    droid_prompt = gen_prompt(
        "https://jobs.example.com/listing/123", min_score=7, model="custom:gemini-flash", engine="droid",
    )
    droid_cmd = manual_command("droid", droid_prompt, model="custom:gemini-flash")
    assert droid_cmd.startswith("droid exec -f ")
    assert "--auto medium" in droid_cmd
    assert "--model custom:gemini-flash" in droid_cmd


def test_doctor_reports_both_binaries(monkeypatch):
    # Claude present, droid absent -> both lines appear, droid as optional warn.
    monkeypatch.setattr(
        "shutil.which",
        lambda name: {"/usr/bin/claude": None, "claude": "/usr/bin/claude"}.get(name),
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Claude Code CLI" in result.output
    assert "Factory Droid CLI" in result.output
    assert "--engine droid" in result.output


def test_doctor_reports_droid_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "/usr/bin/droid" in result.output
    assert "Claude Code CLI" in result.output


# ---------------------------------------------------------------------------
# acquire_job single-URL mode: NULL apply_status rows are acquirable
# ---------------------------------------------------------------------------

def test_acquire_single_url_null_status_is_acquirable():
    """Regression: the --gen single-URL query excluded never-attempted
    (apply_status NULL) jobs — the exact state a freshly tailored job has,
    and the state --gen most needs to handle."""
    seed_ready_job()
    job = launcher.acquire_job(
        target_url="https://jobs.example.com/listing/123", min_score=7, worker_id=0,
    )
    assert job is not None
    assert job["url"] == "https://jobs.example.com/listing/123"


def test_acquire_single_url_in_progress_stays_locked():
    seed_ready_job()
    from applypilot.database import get_connection

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress' WHERE url = ?",
        ("https://jobs.example.com/listing/123",),
    )
    conn.commit()

    assert launcher.acquire_job(
        target_url="https://jobs.example.com/listing/123", min_score=7, worker_id=0,
    ) is None


# ---------------------------------------------------------------------------
# Serialization: Droid multi-worker runs are clamped to one worker
# ---------------------------------------------------------------------------

def test_droid_workers_clamped_to_one(monkeypatch, clean_dashboard):
    calls = []
    monkeypatch.setattr(launcher, "worker_loop", lambda **kwargs: calls.append(kwargs) or (0, 0))

    launcher.main(limit=2, workers=3, engine="droid")

    assert len(calls) == 1
    assert calls[0]["worker_id"] == 0
    assert calls[0]["engine"] == "droid"


def test_claude_workers_not_clamped(monkeypatch, clean_dashboard):
    calls = []
    monkeypatch.setattr(launcher, "worker_loop", lambda **kwargs: calls.append(kwargs) or (0, 0))

    launcher.main(limit=2, workers=3, engine="claude")

    assert len(calls) == 3


def test_main_unknown_engine_raises_readable(monkeypatch, clean_dashboard):
    monkeypatch.delenv("AUTOAPPLY_ENGINE", raising=False)
    with pytest.raises(ValueError, match="Unknown engine"):
        launcher.main(limit=1, workers=1, engine="nope")
