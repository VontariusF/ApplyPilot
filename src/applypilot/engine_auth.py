"""Engine sign-in gate: not_installed / signed_out / signed_in.

The old gate checked only that an engine binary exists (``shutil.which``) —
an installed-but-unsigned-in Claude Code looked identical to a ready one,
and tier detection, doctor, and the portal all trusted that signal. This
module wraps the subprocess-friendly auth surface each CLI actually
exposes (verified against the CLI references, 2026-11-04):

- Claude Code: ``claude auth status`` prints JSON (``authMethod``) and
  exits 0 when signed in, 1 when not; ``claude auth login`` starts the
  browser OAuth flow.
- Factory Droid: no status subcommand exists, so the gate is
  ``FACTORY_API_KEY`` presence — its documented auth credential.

Stdlib-only on purpose: config.py (imported by everything) can use it
without import cycles.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

# The three gate states — the tri-state every consumer switches on.
NOT_INSTALLED = "not_installed"
SIGNED_OUT = "signed_out"
SIGNED_IN = "signed_in"

# `claude auth status` is a quick local check; a hung CLI must not wedge
# tier detection or the portal, so every status call is bounded.
STATUS_TIMEOUT_SECONDS = 10.0

# `claude auth login` blocks until the browser flow completes; the portal
# gives a spawned login this grace beat to surface instant-death failures.
LOGIN_GRACE_SECONDS = 0.5


@dataclass(frozen=True)
class EngineStatus:
    """One engine's gate state plus what its auth surface reported."""

    engine: str  # "claude" | "droid"
    state: str  # NOT_INSTALLED | SIGNED_OUT | SIGNED_IN
    auth_method: str | None  # Claude: JSON authMethod; Droid: "api_key" when keyed
    binary: str | None  # resolved CLI path, when installed
    error: str | None = None  # transport-level failure (timeout, spawn error)


def _parse_auth_method(stdout: str) -> str | None:
    """Extract ``authMethod`` from `claude auth status` output.

    Scans line by line for the first JSON object carrying the key — the
    CLI may print non-JSON lines (banners, update notices) around the
    payload. A signed-in exit code without parseable JSON still counts
    as signed in; the method is then simply unknown.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            method = payload.get("authMethod")
            if method:
                return str(method)
    return None


def claude_status(timeout: float = STATUS_TIMEOUT_SECONDS) -> EngineStatus:
    """Run `claude auth status` and classify the result.

    Exit 0 = signed in, 1 = not signed in, binary missing = not
    installed. A timeout or spawn failure reports signed_out with the
    error attached — a flaky auth check must never read as ready.
    """
    binary = shutil.which("claude")
    if binary is None:
        return EngineStatus("claude", NOT_INSTALLED, None, None)
    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return EngineStatus(
            "claude", SIGNED_OUT, None, binary,
            error=f"`claude auth status` did not finish within {timeout:g}s",
        )
    except OSError as exc:
        return EngineStatus(
            "claude", SIGNED_OUT, None, binary,
            error=f"could not run `claude auth status`: {exc}",
        )
    auth_method = _parse_auth_method(proc.stdout or "")
    if proc.returncode == 0:
        return EngineStatus("claude", SIGNED_IN, auth_method, binary)
    return EngineStatus("claude", SIGNED_OUT, auth_method, binary)


def droid_status() -> EngineStatus:
    """Droid's gate: binary present and ``FACTORY_API_KEY`` set.

    Droid has no status subcommand (checked against its CLI reference),
    so the documented API key is the only sign-in signal available.
    """
    binary = shutil.which("droid")
    if binary is None:
        return EngineStatus("droid", NOT_INSTALLED, None, None)
    if os.environ.get("FACTORY_API_KEY"):
        return EngineStatus("droid", SIGNED_IN, "api_key", binary)
    return EngineStatus("droid", SIGNED_OUT, None, binary)


def start_login() -> tuple[subprocess.Popen | None, str | None]:
    """Spawn `claude auth login`; the browser OAuth flow opens on the user's machine.

    Non-blocking on success: the flow completes in the browser, and the
    caller polls claude_status() for the flip to signed in. Returns
    (None, error) when the binary is missing or the spawn fails.
    """
    binary = shutil.which("claude")
    if binary is None:
        return None, "Claude Code is not installed — install it from https://claude.ai/code first."
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # outlive the portal request
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            [binary, "auth", "login"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except OSError as exc:
        return None, f"could not start `claude auth login`: {exc}"
    return proc, None


def login_instant_death(proc: subprocess.Popen, grace: float = LOGIN_GRACE_SECONDS) -> str | None:
    """Human message when a spawned login died within `grace` seconds.

    `claude auth login` blocks for the browser flow, so a process that
    has already exited by the grace deadline failed before sign-in could
    start. Returns None while the process is still running.
    """
    try:
        code = proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return None
    return f"`claude auth login` exited with code {code} before the sign-in flow could start."
