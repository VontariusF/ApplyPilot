# SPDX-License-Identifier: AGPL-3.0-only
"""Ask-the-agent panel for the Automation page (spec Automation UI).

A scoped assistant: it can start or stop one of the three presets, flip
one of the two master toggles, or report status — nothing else. Every
action routes through the same automation service functions the page's
buttons call (start_preset / stop_preset / update), so the chat adds no
privileged path, and status answers are composed from the store
(load / cap_status), never invented by the model.

The model's only job is intent selection: it replies with a JSON action
from a closed set; anything outside the set is refused. The transcript
is in-memory and bounded — the portal is a single-user local app, and
durable state (toggles, presets, runs) lives in the store, not here.
"""

import json
import logging
from threading import Lock

from kestrel.portal import automation, runner

log = logging.getLogger(__name__)

MAX_TRANSCRIPT = 20

# The panel speaks the same plain-language preset identities as the cards.
FALLBACK_ANSWER = (
    "I can start or stop Find jobs, Apply to matches, or Refresh scores, "
    "turn auto-apply or the daily schedule on or off, or tell you where "
    "things stand."
)

_TOGGLE_LABELS = {"auto_apply_enabled": "Auto-apply",
                  "schedule_enabled": "Daily schedule"}

# In-memory transcript (bounded): durable state lives in the store.
_TRANSCRIPT: list[dict] = []
_LOCK = Lock()

_PROMPT_HEAD = (
    "You are the assistant on the Automation page of Kestrel, a local "
    "job-search portal. Pick exactly ONE action for the user's request and "
    "answer with ONLY a JSON object, no other text. Choose from:\n"
    '{"action": "start", "preset": "find_jobs" | "apply_matches" | "refresh_scores"}\n'
    '{"action": "stop", "preset": "find_jobs" | "apply_matches" | "refresh_scores"}\n'
    '{"action": "toggle", "name": "auto_apply_enabled" | "schedule_enabled", '
    '"value": "on" | "off"}\n'
    '{"action": "status"}\n'
    '{"action": "unknown"}\n\n'
    "The presets: find_jobs searches your job sites and scores every "
    "listing against the resume; apply_matches applies to high-scoring "
    "matches; refresh_scores re-scores saved jobs. The toggles: "
    "auto_apply_enabled lets the portal apply automatically; "
    "schedule_enabled runs automation daily.\n"
)


class AssistantError(Exception):
    """The panel cannot answer (no provider, unusable reply, empty ask)."""


def status_text() -> str:
    """Plain-language status answer, composed from the store."""
    cfg = automation.load()
    cap = automation.cap_status(cfg)
    line = (f"auto-apply is {'on' if cfg['auto_apply_enabled'] else 'off'}, "
            f"capped at {cap['cap']}/day; {cap['used_today']} used today")
    if cap["paused"]:
        line += f" — paused: {cap['pause_reason']}"
    find = cfg["presets"]["find_jobs"]
    apply = cfg["presets"]["apply_matches"]
    return (
        f"{line}. Daily schedule is "
        f"{'on' if cfg['schedule_enabled'] else 'off'}. Find jobs is set "
        f"to the {find['results_level']} results level; Apply to matches "
        f"targets jobs scoring {apply['min_score']} or higher."
    )


def transcript() -> list[dict]:
    """The panel's recent turns (oldest first) — a copy."""
    with _LOCK:
        return list(_TRANSCRIPT)


def clear_transcript() -> None:
    """Drop the in-memory transcript (used by tests; restarts do the same)."""
    with _LOCK:
        _TRANSCRIPT.clear()


def handle_message(text: str) -> str:
    """One assistant turn: user text in, plain-language answer out.

    The turn is remembered in the in-memory transcript; the caller
    renders it. Raises AssistantError when the ask is empty, no LLM
    provider is configured, or the model's reply is unusable.
    """
    text = (text or "").strip()
    if not text:
        raise AssistantError("Ask the agent something first.")
    from kestrel import onboarding, llm

    if not onboarding.provider_ready():
        raise AssistantError(
            "No LLM provider is configured — add one on the Settings page "
            "first; the panel needs it to understand requests.")
    with _LOCK:
        _TRANSCRIPT.append({"role": "user", "text": text})
        del _TRANSCRIPT[:-MAX_TRANSCRIPT]
    answer = _perform(_decide_action(text))
    with _LOCK:
        _TRANSCRIPT.append({"role": "assistant", "text": answer})
        del _TRANSCRIPT[:-MAX_TRANSCRIPT]
    return answer


def _decide_action(text: str) -> dict:
    """The one LLM step: pick a JSON action from the closed set."""
    from kestrel import llm

    try:
        reply = llm.get_client().chat([{
            "role": "user",
            "content": f"{_PROMPT_HEAD}Current status: {status_text()}\n\n"
                       f"User message: {text}",
        }])
    except RuntimeError as exc:
        raise AssistantError(f"The agent could not answer: {exc}") from exc
    return _parse_action(reply)


def _parse_action(reply: str) -> dict:
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end <= start:
        raise AssistantError("The agent did not return an action.")
    try:
        parsed = json.loads(reply[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AssistantError("The agent's reply was not a valid action.") from exc
    if not isinstance(parsed, dict) or "action" not in parsed:
        raise AssistantError("The agent's reply was not a valid action.")
    return parsed


def _perform(action: dict) -> str:
    """Execute one validated action through the button-path services."""
    kind = action.get("action")
    try:
        if kind == "start":
            name = _preset_name(action)
            automation.start_preset(name)
            return f"Started {_preset_title(name).lower()}. {status_text()}"
        if kind == "stop":
            name = _preset_name(action)
            if automation.stop_preset(name):
                return f"Stopped {_preset_title(name).lower()}."
            return f"{_preset_title(name)} wasn't running."
        if kind == "toggle":
            return _perform_toggle(action)
        if kind == "status":
            return status_text()
    except runner.ParamError as exc:
        # Same failure surface as the buttons: the reason comes from the
        # service (e.g. the daily-cap pause), not from the model.
        return str(exc)
    return FALLBACK_ANSWER


def _perform_toggle(action: dict) -> str:
    name = action.get("name")
    if name not in _TOGGLE_LABELS:
        raise AssistantError("The agent picked an unknown toggle.")
    value = "on" if action.get("value") == "on" else "off"
    automation.update({name: value == "on"})
    return f"{_TOGGLE_LABELS[name]} is now {value}. {status_text()}"


def _preset_name(action: dict) -> str:
    name = action.get("preset")
    if name not in automation.PRESETS:
        raise AssistantError("The agent picked an unknown preset.")
    return name


def _preset_title(name: str) -> str:
    return automation.PRESETS[name]["title"]
