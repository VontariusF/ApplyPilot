# SPDX-License-Identifier: AGPL-3.0-only
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
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

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


# The portal polls the Claude gate from several surfaces at once (the
# engine cards every 2s, the onboarding chat signature, onboarding
# readiness) — each poll used to spawn its own `claude auth status`
# subprocess. A short TTL shares one probe across concurrent callers;
# callers still run it in a worker thread (asyncio.to_thread), never on
# an event loop. Sign-in flips surface within TTL + one poll tick, well
# inside the gate's multi-minute polling window.
_STATUS_CACHE_TTL_SECONDS = 1.5

_status_lock = threading.Lock()
# (monotonic time taken, probe that produced it, its result). Keying on
# the probe identity makes test fakes (which monkeypatch claude_status)
# invalidate the cache automatically — a new fake is a new probe.
_status_cache: tuple[float, Callable[[], EngineStatus], EngineStatus] | None = None


def reset_status_cache() -> None:
    """Drop the cached Claude status (tests, and fresh-probe callers)."""
    global _status_cache
    with _status_lock:
        _status_cache = None


def claude_status_cached(ttl: float = _STATUS_CACHE_TTL_SECONDS) -> EngineStatus:
    """claude_status() behind a short TTL cache, safe to call concurrently.

    The lock is held across the probe so simultaneous callers SHARE one
    subprocess instead of each spawning their own — the probe holds the
    lock only for its (bounded) runtime, and a stale value outlives it by
    at most the TTL. A change of probe identity (test fakes, a patched
    function) is always a cache miss, so fakes take effect immediately.
    """
    global _status_cache
    probe = claude_status  # read through the module global: patch-aware
    with _status_lock:
        if _status_cache is not None:
            taken_at, cached_probe, cached = _status_cache
            if cached_probe is probe and time.monotonic() - taken_at < ttl:
                return cached
        value = probe()
        _status_cache = (time.monotonic(), probe, value)
        return value


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


# The CLI prints its sign-in URL somewhere in its output (banner text around
# it varies between releases); the capture is format-agnostic — first
# http(s) URL found wins, on either stream.
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


def extract_oauth_url(text: str) -> str | None:
    """First http(s) URL in captured login output, if any."""
    match = _URL_PATTERN.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(".,;)")


@dataclass
class LoginProcess:
    """A spawned `claude auth login` whose output is captured in the background.

    On a desktop the CLI opens the browser itself; on a headless portal the
    OAuth URL it prints is the only way to sign in, so stdout (with stderr
    merged in) is drained by a daemon thread instead of being discarded.
    `wait` delegates to the child, so callers keep treating this like the
    Popen it wraps.
    """

    proc: subprocess.Popen
    output: list[str] = field(default_factory=list)
    capture_error: str | None = None  # why the capture ended early, if it did

    def wait(self, timeout: float | None = None) -> int:
        """Delegate to the child process."""
        return self.proc.wait(timeout=timeout)

    def text(self) -> str:
        """Everything the CLI has printed so far."""
        return "".join(self.output)

    def oauth_url(self) -> str | None:
        return extract_oauth_url(self.text())


def _capture_output(login: LoginProcess) -> None:
    """Drain the login process's output into the capture (runs as a daemon).

    The thread must keep the pipe empty so the child never blocks on a
    full buffer. A capture failure is recorded, not raised — the login
    outcome is judged by the process exit and claude_status(), never by
    whether the capture survived.
    """
    stream = login.proc.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            login.output.append(line)
    except (OSError, ValueError) as exc:
        login.capture_error = f"output capture ended early: {exc}"
    finally:
        try:
            stream.close()
        except OSError as exc:
            login.capture_error = login.capture_error or f"could not close capture: {exc}"


# The most recent spawned login. The portal is single-user and one login
# runs at a time, so a plain module slot is the whole registry.
_active_login: LoginProcess | None = None


def active_login() -> LoginProcess | None:
    """The most recent spawned login, whose output may carry the OAuth URL."""
    return _active_login


def start_login() -> tuple[LoginProcess | None, str | None]:
    """Spawn `claude auth login`, capturing its output for the gate pages.

    Non-blocking on success: the flow completes in the browser, and the
    caller polls claude_status() for the flip to signed in. The OAuth URL
    the CLI prints is captured rather than discarded, so a headless portal
    can re-render it as a clickable sign-in link. Returns (None, error)
    when the binary is missing or the spawn fails.
    """
    global _active_login
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # a URL on stderr is still a sign-in link
            text=True,
            **popen_kwargs,
        )
    except OSError as exc:
        return None, f"could not start `claude auth login`: {exc}"
    login = LoginProcess(proc)
    threading.Thread(target=_capture_output, args=(login,),
                     name="claude-login-capture", daemon=True).start()
    _active_login = login
    return login, None


def login_instant_death(proc: LoginProcess | subprocess.Popen,
                        grace: float = LOGIN_GRACE_SECONDS) -> str | None:
    """Human message when a spawned login died within `grace` seconds.

    `claude auth login` blocks for the browser flow, so a process that
    has already exited by the grace deadline failed before sign-in could
    start. Returns None while the process is still running.
    """
    try:
        code = proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return None
    message = f"`claude auth login` exited with code {code} before the sign-in flow could start."
    # A CLI that died instantly usually printed why; the capture merged
    # stdout+stderr, so hand the tail along. Best-effort: the reader thread
    # may not have drained the pipe yet for a large burst of output.
    text = proc.text().strip() if callable(getattr(proc, "text", None)) else ""
    if text:
        message += f" It printed: {text[-200:]}"
    return message
