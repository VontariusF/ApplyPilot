# SPDX-License-Identifier: AGPL-3.0-only
"""Engine-adapter interface for the auto-apply stage.

Both engines (Claude Code, Factory Droid) drive the same per-worker Chrome
over CDP through the same Playwright/Gmail MCP servers, read the same
prompt, and report outcomes through the same RESULT:* text contract. What
differs is the agent binary, its permission model, its prompt delivery, and
its output format — each adapter normalizes its engine onto the interface
below so `launcher.run_job` stays engine-agnostic.

Adapters:
  kestrel.apply.claude  — Claude Code CLI (the reference implementation,
                             extracted verbatim from the original run_job)
  kestrel.apply.droid   — Factory Droid CLI (written against documented
                             Droid behavior; the CLI was unavailable in the
                             build sandbox, so every assumption is flagged
                             and concentrated in that one file)
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from kestrel import config

# Engine registry order; "claude" is the default and the proven path.
ENGINE_CHOICES = ("claude", "droid")


# ---------------------------------------------------------------------------
# Normalized stream / result types
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    """One normalized event from an engine's stdout stream.

    kind:
      "text"   — assistant text; appended to the transcript buffer
      "action" — a tool call; action_desc feeds the dashboard/log feed
      "usage"  — final usage/cost report; stats uses the Claude parser's keys
                 (input_tokens, output_tokens, cache_read, cache_create,
                 cost_usd, turns)
      "ignore" — recognized engine event with nothing to report
    """

    kind: str
    text: str = ""
    action_desc: str = ""
    stats: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyResult:
    """Outcome parsed from an engine's final assistant text.

    status is the engine-agnostic vocabulary the launcher persists:
    applied | expired | captcha | login_issue | failed:<reason> |
    failed:no_result_line. Both adapters share the exact same scan, so
    identical assistant text yields an identical ApplyResult.
    """

    status: str


class ApplyEngine(Protocol):
    """Interface every auto-apply engine implements."""

    # Registry name ("claude" | "droid"); recorded in the jobs.agent_id column.
    name: str
    # How the prompt reaches the process: piped to stdin (claude) or written
    # to a file referenced on the command line (droid).
    prompt_delivery: str  # "stdin" | "file"

    def setup(self, worker_id: int, cdp_port: int) -> None:
        """Per-run engine environment preparation (MCP config files, auth checks)."""
        ...

    def build_command(self, prompt_path: Path | None, model: str, cwd: Path) -> list[str]:
        """Build the full command line. model is passed through verbatim."""
        ...

    def child_env(self) -> dict:
        """Environment for the child process (auth vars flow only here)."""
        ...

    def spawn(self, cmd: list[str], env: dict, cwd: Path) -> subprocess.Popen:
        """Start the engine process."""
        ...

    def parse_stream(self, line: str) -> list[StreamEvent] | None:
        """Normalize one stdout line.

        Returns a list of events in order, [] when the engine emitted a
        recognized-but-uninteresting event, or None when the line is raw
        text that should be kept in the transcript for RESULT scanning.
        """
        ...

    def extract_result(self, assistant_text: str) -> ApplyResult:
        """Scan the concatenated assistant text for the RESULT:* contract."""
        ...


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

def make_mcp_config(cdp_port: int) -> dict:
    """Build the MCP server config shared by both engines.

    Playwright drives the worker's Chrome over CDP; the gmail server gives
    the agent its email tools. Identical to the original launcher._make_mcp_config.
    """
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


def describe_tool(name: str, tool_input: dict) -> str:
    """Human summary of a tool call for the dashboard/log feed.

    Shared by both engines; adapters strip their own tool-name prefixes
    before calling. Preserves the original Claude parser's heuristics and
    truncation exactly.
    """
    if "url" in tool_input:
        return f"{name} {tool_input['url'][:60]}"
    if "ref" in tool_input:
        return f"{name} {tool_input.get('element', tool_input.get('text', ''))}"[:50]
    if "fields" in tool_input:
        return f"{name} ({len(tool_input['fields'])} fields)"
    if "paths" in tool_input:
        return f"{name} upload"
    return name


_RESULT_MARKERS = ("APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE")
# A RESULT:FAILED:<reason> whose reason is one of these is promoted to the
# positive status (same behavior as the original inline parser).
_PROMOTABLE_REASONS = {"captcha", "expired", "login_issue"}


def _clean_reason(reason: str) -> str:
    return re.sub(r'[*`"]+$', '', reason).strip()


def scan_result(assistant_text: str) -> ApplyResult:
    """Shared RESULT:* contract scan — identical for every engine.

    Preserves the original Claude parser's precedence: positive markers are
    checked first across the whole text (in the fixed marker order), then
    RESULT:FAILED lines in line order. Text with no marker yields
    failed:no_result_line.
    """
    for marker in _RESULT_MARKERS:
        if f"RESULT:{marker}" in assistant_text:
            return ApplyResult(status=marker.lower())

    if "RESULT:FAILED" in assistant_text:
        for out_line in assistant_text.split("\n"):
            if "RESULT:FAILED" in out_line:
                if ":" in out_line[out_line.index("FAILED") + 6:]:
                    reason = _clean_reason(out_line.split("RESULT:FAILED:")[-1].strip())
                    if reason in _PROMOTABLE_REASONS:
                        return ApplyResult(status=reason)
                    return ApplyResult(status=f"failed:{reason}")
                return ApplyResult(status="failed:unknown")

    return ApplyResult(status="failed:no_result_line")


def resolve_engine_name(flag_value: str | None = None) -> str:
    """Resolve the selected engine: --engine flag > AUTOAPPLY_ENGINE env >
    config default.

    Raises ValueError with a readable message for unknown names so callers
    (CLI, portal) can exit cleanly instead of failing mid-run.
    """
    name = flag_value or os.environ.get("AUTOAPPLY_ENGINE") or config.DEFAULTS["apply_engine"]
    name = str(name).strip().lower()
    if name not in ENGINE_CHOICES:
        raise ValueError(f"Unknown engine '{name}'. Valid engines: {', '.join(ENGINE_CHOICES)}")
    return name


def make_engine(name: str) -> ApplyEngine:
    """Instantiate an adapter by name.

    Lazy imports keep the adapters' module-level graphs acyclic (they import
    this module for the shared pieces).
    """
    resolved = str(name).strip().lower()
    if resolved == "claude":
        from kestrel.apply.claude import ClaudeEngine
        return ClaudeEngine()
    if resolved == "droid":
        from kestrel.apply.droid import DroidEngine
        return DroidEngine()
    raise ValueError(f"Unknown engine '{name}'. Valid engines: {', '.join(ENGINE_CHOICES)}")
