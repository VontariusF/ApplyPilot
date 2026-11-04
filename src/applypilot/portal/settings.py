"""Settings page backend: read ~/.applypilot/.env masked, write it merged.

Reads never return a secret value — secrets are masked to their last four
characters. Writes preserve unknown lines and comments, and go through
config.write_secret_text so the file is 0600 (S2). Field semantics:

- secret field left blank  -> keep the existing value
- secret field + "clear"   -> remove the line
- any other field          -> set to the submitted value
"""

import os

from applypilot import config
from applypilot.security import REDACTED, redact_secret_values

# The .env surface the portal manages, in display order. kind: "secret"
# masks reads and keeps blanks as keep-existing; "text"/"choice" set
# verbatim. choices are exhaustive for select fields.
SECRET = "secret"
TEXT = "text"
CHOICE = "choice"

FIELDS: list[dict] = [
    {"key": "LLM_PROVIDER", "kind": CHOICE,
     "choices": ["gemini", "openai", "custom", "local"],
     "label": "LLM provider",
     "help": "Explicit provider selection. Unset = legacy auto-detect (Gemini key, OpenAI key, LLM_URL)."},
    {"key": "LLM_URL", "kind": TEXT,
     "label": "Custom endpoint base URL",
     "help": "OpenAI-compatible base URL for LLM_PROVIDER=custom/local (llama.cpp, Ollama /v1, vLLM, OpenRouter)."},
    {"key": "LLM_MODEL", "kind": TEXT, "datalist": True,
     "label": "Custom endpoint model",
     "help": "Model id the endpoint expects (e.g. qwen2.5:7b for Ollama). Pick from the list or type any id."},
    {"key": "LLM_API_KEY", "kind": SECRET,
     "label": "Custom endpoint API key",
     "help": "Bearer key for the custom endpoint. Optional: llama.cpp needs none."},
    {"key": "GEMINI_API_KEY", "kind": SECRET,
     "label": "Gemini API key", "help": "Google AI Studio key for the Gemini provider."},
    {"key": "OPENAI_API_KEY", "kind": SECRET,
     "label": "OpenAI API key", "help": "api.openai.com key for the OpenAI provider."},
    {"key": "FACTORY_API_KEY", "kind": SECRET,
     "label": "Factory API key",
     "help": "Authenticates the Droid engine (AUTOAPPLY_ENGINE=droid)."},
    {"key": "CAPSOLVER_API_KEY", "kind": SECRET,
     "label": "CapSolver API key", "help": "Optional CAPTCHA solving during auto-apply."},
    {"key": "AUTOAPPLY_MODEL", "kind": TEXT, "datalist": True,
     "label": "Default apply model",
     "help": "Model passed to the apply engine when a run does not name one. Pick from the list or type any id."},
    {"key": "AUTOAPPLY_ENGINE", "kind": CHOICE,
     "choices": ["", "claude", "droid"],
     "label": "Default apply engine",
     "help": "Default engine for apply runs (claude | droid). Runs launched from the Runs page inherit this."},
    {"key": "HISTORY_ENABLED", "kind": CHOICE,
     "choices": ["on", "off"],
     "label": "Document history",
     "help": "Keep every generated resume and cover letter as a new version (on) or overwrite in place (off). Default on."},
]

_SECRET_KEYS = [f["key"] for f in FIELDS if f["kind"] == SECRET]


def mask(value: str | None) -> str:
    """Mask a secret for display: last four characters only."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return f"••••{value[-4:]}"


def read_env_file() -> dict[str, str]:
    """Parse the current .env into a key/value dict (raw values, in-memory)."""
    path = config.ENV_PATH
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


def masked_view() -> list[dict]:
    """Field registry joined with the current (masked) values."""
    values = read_env_file()
    view = []
    for field in FIELDS:
        raw = values.get(field["key"], "")
        view.append({
            **field,
            "raw": raw,
            "masked": mask(raw) if field["kind"] == SECRET else raw,
            "is_secret": field["kind"] == SECRET,
        })
    return view


def _parse_form(form: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """Split the submitted form into values and cleared keys."""
    values: dict[str, str] = {}
    cleared: set[str] = set()
    for field in FIELDS:
        key = field["key"]
        submitted = (form.get(key) or "").strip()
        if key in form:
            values[key] = submitted
        if form.get(f"{key}__clear"):
            cleared.add(key)
    return values, cleared


def _effective_changes(
    values: dict[str, str], cleared: set[str]
) -> tuple[dict[str, str], set[str]]:
    """Resolve the form into keys to set and keys to drop.

    Semantics: a cleared key is removed; a submitted non-empty value is
    set; an empty submission removes text/choice keys but leaves secrets
    untouched (blank secret = keep existing).
    """
    sets: dict[str, str] = {}
    drops: set[str] = set(cleared)
    kinds = {f["key"]: f["kind"] for f in FIELDS}
    for key, val in values.items():
        if key in cleared:
            continue
        if val:
            sets[key] = val
        elif kinds.get(key, TEXT) != SECRET:
            drops.add(key)
    return sets, drops


def _merge_env_text(text: str, sets: dict[str, str], drops: set[str]) -> str:
    """Line-based merge: comments, blanks, and unknown keys survive in place."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        is_entry = stripped and not stripped.startswith("#") and "=" in stripped
        if is_entry:
            key = stripped.partition("=")[0].strip()
            if key in drops:
                continue
            if key in sets:
                out.append(f"{key}={sets[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, val in sets.items():
        if key not in seen:
            out.append(f"{key}={val}")
    return "\n".join(out).rstrip("\n") + "\n"


def apply_settings(form: dict[str, str]) -> dict[str, str]:
    """Merge a settings submission into the .env file at 0600.

    Preserves comments and unrecognized lines. Returns the resulting
    key/value map (raw, in-memory) for tests.
    """
    values, cleared = _parse_form(form)
    sets, drops = _effective_changes(values, cleared)
    if config.ENV_PATH.exists():
        text = config.ENV_PATH.read_text(encoding="utf-8")
    else:
        text = "# ApplyPilot configuration\n"
    config.write_secret_text(config.ENV_PATH, _merge_env_text(text, sets, drops))
    return read_env_file()


def collect_secret_values() -> list[str]:
    """Every configured secret value, for redaction boundaries.

    Combines the managed secret fields with anything in the live
    environment whose name looks like a key/token/password.
    """
    secrets_list: list[str] = []
    seen: set[str] = set()
    for source in (read_env_file(), dict(os.environ)):
        for key, val in source.items():
            if not val:
                continue
            if key in _SECRET_KEYS or any(
                marker in key.upper() for marker in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")
            ):
                if val not in seen:
                    seen.add(val)
                    secrets_list.append(val)
    return secrets_list


def redact_for_render(text: str) -> str:
    """The portal's redaction boundary: strip configured secrets and
    proxy credentials from anything destined for a browser."""
    text = redact_secret_values(text, collect_secret_values())
    return text


def describe_redaction() -> str:
    """Human note shown in the log viewer."""
    return f"Secrets and proxy credentials are replaced with {REDACTED} before rendering."
