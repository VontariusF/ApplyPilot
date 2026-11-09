"""Claude Code engine adapter — the reference implementation.

Extracted verbatim from the original launcher.run_job command-build and
stream-parse blocks so the default path is behaviorally unchanged: same
command shape, same stdin prompt delivery, same per-worker MCP config, same
event normalization and stats dict. tests/test_engine_selection.py pins all
of it as a regression guarantee.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from kestrel import config
from kestrel.apply.engines import (
    ApplyResult,
    StreamEvent,
    describe_tool,
    make_mcp_config,
    scan_result,
)

# The 15 destructive gmail tools the original run_job denied by name.
_GMAIL_DENYLIST = (
    "mcp__gmail__draft_email,mcp__gmail__modify_email,"
    "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
    "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
    "mcp__gmail__create_label,mcp__gmail__update_label,"
    "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
    "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
    "mcp__gmail__list_filters,mcp__gmail__get_filter,"
    "mcp__gmail__delete_filter"
)


class ClaudeEngine:
    """Runs one job application through the Claude Code CLI."""

    name = "claude"
    prompt_delivery = "stdin"

    def __init__(self) -> None:
        self._mcp_config_path: Path | None = None

    def setup(self, worker_id: int, cdp_port: int) -> None:
        """Write the per-worker MCP config (exactly as run_job always did)."""
        path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
        path.write_text(json.dumps(make_mcp_config(cdp_port)), encoding="utf-8")
        self._mcp_config_path = path

    def build_command(self, prompt_path: Path | None, model: str, cwd: Path) -> list[str]:
        """The original command: prompt arrives on stdin, so prompt_path is unused."""
        return [
            "claude",
            "--model", model,
            "-p",
            "--mcp-config", str(self._mcp_config_path),
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--disallowedTools", _GMAIL_DENYLIST,
            "--output-format", "stream-json",
            "--verbose", "-",
        ]

    def child_env(self) -> dict:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        return env

    def spawn(self, cmd: list[str], env: dict, cwd: Path) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
        )

    def parse_stream(self, line: str) -> list[StreamEvent] | None:
        """Normalize one Claude Code stream-json line (original parser semantics)."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None  # raw line — kept in the transcript
        if not isinstance(msg, dict):
            # Non-dict JSON (e.g. "[1,2]") would have crashed the original
            # parser with AttributeError; treat it as raw text instead.
            return None

        msg_type = msg.get("type")
        if msg_type == "assistant":
            events: list[StreamEvent] = []
            for block in msg.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "text":
                    events.append(StreamEvent(kind="text", text=block["text"]))
                elif bt == "tool_use":
                    name = (
                        block.get("name", "")
                        .replace("mcp__playwright__", "")
                        .replace("mcp__gmail__", "gmail:")
                    )
                    desc = describe_tool(name, block.get("input", {}))
                    events.append(StreamEvent(kind="action", action_desc=desc))
            return events
        if msg_type == "result":
            usage = msg.get("usage", {})
            stats = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_create": usage.get("cache_creation_input_tokens", 0),
                "cost_usd": msg.get("total_cost_usd", 0),
                "turns": msg.get("num_turns", 0),
            }
            return [StreamEvent(kind="usage", stats=stats, text=msg.get("result", ""))]
        return []  # recognized engine event, nothing to report

    def extract_result(self, assistant_text: str) -> ApplyResult:
        return scan_result(assistant_text)
