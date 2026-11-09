# SPDX-License-Identifier: AGPL-3.0-only
"""Factory Droid engine adapter.

Shells out to `droid exec` per job, reading the prompt from a file.

UNVERIFIED SURFACE — read before changing anything here: the `droid` CLI was
not available in the build sandbox (no binary, no FACTORY_API_KEY — see the
spike findings doc), so this adapter implements the documented Droid surface
from the Factory research pass:

    droid exec -f <prompt_file> --auto medium --output-format json \
        --model <model> --enabled-tools <tools> --cwd <worker_dir>

Assumptions concentrated in this file (each marked UNVERIFIED), to be
confirmed or corrected by a real `droid exec` spike in one place:

  1. Command/flag shape and value syntax (comma-separated --enabled-tools).
  2. MCP tool naming matches the raw MCP tool names the prompt already uses
     (browser_navigate, ...), i.e. no mcp__<server>__ prefix.
  3. JSON event shapes are Claude-like (assistant/result), with two tolerated
     alternates (top-level tool_call, result carrying "text"); unknown JSON
     shapes fall back to raw text so nothing crashes and RESULT scanning
     still works.
  4. FACTORY_API_KEY is the auth credential, passed to the child env only.
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

# UNVERIFIED (assumption 2): allowlist of the MCP tools the apply prompt
# actually drives. --enabled-tools is an ALLOWLIST (the Claude path used a
# --disallowedTools DENYLIST), so naming exactly these keeps the destructive
# gmail tools (draft/modify/delete/batch...) out of the agent's scope.
DROID_ALLOWED_TOOLS = ",".join([
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_fill_form",
    "browser_file_upload",
    "browser_take_screenshot",
    "browser_wait_for",
    "browser_evaluate",
    "browser_tabs",
    "send_email",
    "search_emails",
    "read_email",
])


class DroidEngine:
    """Runs one job application through the Factory Droid CLI.

    Droid reads a single global ~/.factory/mcp.json (no per-invocation
    --mcp-config), and the Playwright MCP args bake in the per-worker CDP
    port — which is why Droid runs are serialized to one worker this release
    (enforced in launcher.main). setup() merges this run's servers into that
    file, preserving the user's own entries.
    """

    name = "droid"
    prompt_delivery = "file"

    def __init__(self, factory_dir: Path | None = None) -> None:
        # factory_dir override exists for tests; KESTREL_FACTORY_DIR is a
        # user-facing relocation knob in our own namespace (the legacy
        # APPLYPILOT_FACTORY_DIR stays readable — config.env_with_legacy).
        env_dir = config.env_with_legacy(
            "KESTREL_FACTORY_DIR", "APPLYPILOT_FACTORY_DIR")
        self.factory_dir = Path(factory_dir or env_dir or (Path.home() / ".factory"))

    # ------------------------------------------------------------------
    # setup: global mcp.json merge (mode 0600)
    # ------------------------------------------------------------------

    def setup(self, worker_id: int, cdp_port: int) -> None:
        mcp_path = self.factory_dir / "mcp.json"
        existing: dict = {}
        if mcp_path.exists():
            try:
                loaded = json.loads(mcp_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(
                    f"Existing {mcp_path} is not valid JSON ({exc}) — fix or remove it so "
                    "the droid engine can merge its MCP servers; refusing to clobber it."
                ) from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(
                    f"Existing {mcp_path} does not contain a JSON object; "
                    "refusing to overwrite it."
                )
            existing = loaded

        servers_raw = existing.get("mcpServers") or {}
        if not isinstance(servers_raw, dict):
            raise RuntimeError(
                f"Existing {mcp_path} has a non-object 'mcpServers' section; "
                "refusing to overwrite it."
            )

        servers = dict(servers_raw)
        ours = make_mcp_config(cdp_port)["mcpServers"]
        servers["playwright"] = ours["playwright"]
        servers["gmail"] = ours["gmail"]

        merged = dict(existing)
        merged["mcpServers"] = servers

        self.factory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_CREAT with 0o600 avoids a world-readable window on create; the
        # chmod tightens pre-existing looser files as well.
        fd = os.open(mcp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
            fh.write("\n")
        os.chmod(mcp_path, 0o600)
        self._mcp_path = mcp_path

    # ------------------------------------------------------------------
    # command / env / process
    # ------------------------------------------------------------------

    def build_command(self, prompt_path: Path | None, model: str, cwd: Path) -> list[str]:
        if prompt_path is None:
            raise RuntimeError("droid engine reads the prompt from a file (-f); got none")
        # model is passed through verbatim — including custom:<name> ids the
        # user defined in ~/.factory/config.json (the route to any
        # OpenAI-compatible endpoint, e.g. Gemini, without a local key).
        return [
            "droid", "exec",
            "-f", str(prompt_path),
            "--auto", "medium",
            "--output-format", "json",
            "--model", model,
            "--enabled-tools", DROID_ALLOWED_TOOLS,
            "--cwd", str(cwd),
        ]

    def child_env(self) -> dict:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # FACTORY_API_KEY reaches the child through its environment only —
        # never logged, never embedded in prompts.
        if not env.get("FACTORY_API_KEY"):
            raise RuntimeError(
                "FACTORY_API_KEY is not set — the droid engine authenticates with it "
                "(add FACTORY_API_KEY to ~/.kestrel/.env or export it)."
            )
        return env

    def spawn(self, cmd: list[str], env: dict, cwd: Path) -> subprocess.Popen:
        # stdin is DEVNULL: the prompt arrives via -f, and no stdin contract
        # exists for droid — an accidental stdin read can never hang the run.
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
        )

    # ------------------------------------------------------------------
    # stream parsing
    # ------------------------------------------------------------------

    def parse_stream(self, line: str) -> list[StreamEvent] | None:
        """Normalize one Droid JSON event line.

        Assumption 3 applies: Claude-like shapes are primary; a top-level
        tool_call and a result carrying "text" are tolerated alternates;
        anything else (including non-dict JSON and plain text) falls back to
        raw text so the RESULT scan still sees it.
        """
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(msg, dict):
            return None

        msg_type = msg.get("type")
        if msg_type == "assistant":
            events: list[StreamEvent] = []
            for block in msg.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    events.append(StreamEvent(kind="text", text=block.get("text", "")))
                elif bt == "tool_use":
                    events.append(self._tool_event(block.get("name", ""), block.get("input") or {}))
            return events
        if msg_type == "tool_call":  # tolerated alternate (UNVERIFIED)
            return [self._tool_event(msg.get("name", ""), msg.get("input") or {})]
        if msg_type == "result":
            text = msg.get("result") or msg.get("text") or ""
            return [StreamEvent(kind="usage", stats=self._stats(msg), text=text)]
        return None  # unrecognized shape → raw text

    def _tool_event(self, raw_name: str, tool_input: dict) -> StreamEvent:
        # Tolerant prefix strip: works whether or not Droid namespaces tools
        # with the mcp__<server>__ convention (assumption 2).
        name = (
            raw_name
            .replace("mcp__playwright__", "")
            .replace("mcp__gmail__", "gmail:")
        )
        return StreamEvent(kind="action", action_desc=describe_tool(name, tool_input))

    def _stats(self, msg: dict) -> dict:
        """Normalize usage/cost into the Claude parser's stats keys.

        UNVERIFIED: Droid's usage field names are documented to differ from
        Claude's; read the Claude names first, then camelCase/alternate
        spellings, defaulting to 0. The real spike pins this mapping.
        """
        usage = msg.get("usage") or {}

        def pick(source: dict, *names: str, default: int | float = 0):
            for field_name in names:
                value = source.get(field_name)
                if value is not None:
                    return value
            return default

        return {
            "input_tokens": pick(usage, "input_tokens", "inputTokens"),
            "output_tokens": pick(usage, "output_tokens", "outputTokens"),
            "cache_read": pick(usage, "cache_read_input_tokens", "cacheReadInputTokens"),
            "cache_create": pick(usage, "cache_creation_input_tokens", "cacheCreationInputTokens"),
            "cost_usd": pick(msg, "total_cost_usd", "cost_usd"),
            "turns": pick(msg, "num_turns", "turns"),
        }

    def extract_result(self, assistant_text: str) -> ApplyResult:
        return scan_result(assistant_text)
