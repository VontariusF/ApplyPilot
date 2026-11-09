"""Bounded JSONL event log for apply runs.

The portal's SSE endpoint tails these files; the CLI dashboard writes them
as a side effect of update_state/add_event when KESTREL_EVENT_LOG points
at a path. Opt-in via environment: a plain CLI run emits no JSONL.

The file is bounded — when it exceeds max_bytes it rotates to ``<name>.1``
(keeping exactly one previous generation), so a long continuous run can
never grow the file unboundedly.
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from kestrel import config

# One live file plus one rotated generation stays under ~2x this bound.
MAX_EVENT_LOG_BYTES = 256 * 1024

# Rich markup tags ([dim], [bold red], ...) are console styling; strip them so
# structured events carry the message text, not terminal directives.
_RICH_TAG = re.compile(r"\[/?(?:[a-z_]+(?: [a-z_]+)*|/?#[0-9a-fA-F]{3,8})\]")

_env_lock = threading.Lock()
_writer_by_path: dict[str, "EventLogWriter"] = {}


def strip_rich_markup(msg: str) -> str:
    """Remove Rich styling tags from a dashboard message."""
    return _RICH_TAG.sub("", msg)


def event_log_path_from_env() -> Path | None:
    """The JSONL path configured via KESTREL_EVENT_LOG, if any.

    The pre-rename APPLYPILOT_EVENT_LOG stays readable
    (config.env_with_legacy) so a legacy deployment's unit file keeps
    tailing across the upgrade.
    """
    raw = (config.env_with_legacy(
        "KESTREL_EVENT_LOG", "APPLYPILOT_EVENT_LOG") or "").strip()
    return Path(raw) if raw else None


def get_writer(path: Path | str | None = None) -> "EventLogWriter | None":
    """Return a shared writer for the configured event log path.

    Writers are cached per path so every caller shares one lock and one
    rotation counter. Returns None when neither an explicit path nor the
    KESTREL_EVENT_LOG environment variable provides one.
    """
    if path is None:
        path = event_log_path_from_env()
    if path is None:
        return None
    key = str(path)
    with _env_lock:
        writer = _writer_by_path.get(key)
        if writer is None:
            writer = EventLogWriter(Path(key))
            _writer_by_path[key] = writer
        return writer


def reset_writer_cache() -> None:
    """Drop cached writers (tests repoint the env var between cases)."""
    with _env_lock:
        _writer_by_path.clear()


class EventLogWriter:
    """Append-only JSONL event log with a hard size bound."""

    def __init__(self, path: Path, max_bytes: int = MAX_EVENT_LOG_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> None:
        """Append one event as a JSON line, rotating first if needed."""
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with self._lock:
            self._rotate_if_oversize(len(line))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)

    def emit_state(self, state: dict, worker_id: int) -> None:
        """Write a full worker-state snapshot (portal worker panels)."""
        payload = {"kind": "state", "worker_id": worker_id, **state}
        self.write(payload)

    def emit_event(self, msg: str) -> None:
        """Write a dashboard event line, stripped of Rich markup."""
        self.write({"kind": "event", "msg": strip_rich_markup(msg)})

    def _rotate_if_oversize(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + incoming <= self.max_bytes:
                return
            rotated = self.path.with_suffix(self.path.suffix + ".1")
            rotated.unlink(missing_ok=True)  # keep exactly one generation
            self.path.rename(rotated)
        except OSError:
            # Rotation is best-effort; appending is safe either way.
            pass
