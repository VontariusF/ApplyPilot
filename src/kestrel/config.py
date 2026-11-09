# SPDX-License-Identifier: AGPL-3.0-only
"""Kestrel configuration: paths, platform detection, user data."""

import logging
import os
import platform
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from kestrel import engine_auth

_logger = logging.getLogger(__name__)


def env_with_legacy(new: str, legacy: str) -> str | None:
    """Read an env var under its Kestrel name, deferring to the legacy name.

    The 0.1.0 rename replaced the APPLYPILOT_* namespace with KESTREL_*. A
    non-empty new value always wins — even when it parses to a disabled
    value ("0"), so a deployment can explicitly undo a legacy opt-out —
    and the pre-rename name is only consulted when the new one is unset.
    That keeps existing deployments (a launchd unit exporting
    APPLYPILOT_DIR, for example) working across the upgrade with no
    user-side change.
    """
    value = os.environ.get(new)
    if value:
        return value
    return os.environ.get(legacy)


def _migrate_legacy_db(app_dir: Path) -> None:
    """Rename applypilot.db to kestrel.db inside a pre-rename data dir.

    The WAL trio is the database — journal_mode=WAL means committed
    transactions can still live in ``-wal`` until the next checkpoint —
    so any ``-wal``/``-shm`` siblings move with the main file. Renaming
    in place (same directory, atomic) leaves the SQLite state
    byte-identical; runs only while the new name does not exist yet.
    Assumes no legacy-version process still holds the database open.
    """
    db = app_dir / "kestrel.db"
    if not (app_dir / "applypilot.db").is_file() or db.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        source = app_dir / f"applypilot.db{suffix}"
        if source.exists():
            os.replace(source, app_dir / f"kestrel.db{suffix}")


def _migrate_legacy_app_dir(default: Path, legacy: Path) -> Path:
    """First-run migration: adopt the pre-rename data dir as the Kestrel one.

    Moves ~/.applypilot to ~/.kestrel wholesale when no Kestrel dir exists
    yet — a same-filesystem rename that carries file modes with it (the
    0600 portal token survives as-is) and keeps the SQLite database
    readable. Once a Kestrel dir exists it is used untouched and the
    legacy dir is never merged or deleted. Returns the directory to use.
    """
    if not legacy.is_dir() or default.exists():
        return default
    try:
        shutil.move(str(legacy), str(default))
    except OSError as exc:
        if default.exists():
            pass  # a concurrent process finished the same migration
        else:
            # Degrade to the pre-rename layout rather than fail startup:
            # the legacy dir is exactly what the old version read.
            _logger.warning(
                "Could not migrate %s to %s (%s); continuing with the "
                "legacy data directory in place.", legacy, default, exc)
            return legacy
    # WARNING is the only level rendered without logging configuration, and
    # the user must be told where their data went.
    _logger.warning(
        "Migrated data directory %s -> %s (one-time Kestrel rename).",
        legacy, default)
    return default


def _resolve_app_dir() -> Path:
    """Pick the data dir: KESTREL_DIR, then APPLYPILOT_DIR, then migration."""
    override = env_with_legacy("KESTREL_DIR", "APPLYPILOT_DIR")
    if override:
        return Path(override)
    home = Path.home()
    app_dir = _migrate_legacy_app_dir(home / ".kestrel", home / ".applypilot")
    _migrate_legacy_db(app_dir)
    return app_dir


# User data directory — all user-specific files live here. Bound once at
# import; every consumer reads the derived constants below as attributes.
APP_DIR = _resolve_app_dir()

# Core paths
DB_PATH = APP_DIR / "kestrel.db"
PROFILE_PATH = APP_DIR / "profile.json"
PROFILE_MD_PATH = APP_DIR / "profile.md"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"
# Onboarding interview state (conversation transcript, phase, claude session id)
ONBOARDING_STATE_PATH = APP_DIR / "onboarding-state.json"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    # S2: APP_DIR holds secret files (profile.json, .env, prompt logs).
    # Owner-only traversal keeps every other user off everything under it.
    if os.name == "posix":
        os.chmod(APP_DIR, 0o700)


def _tighten_file(path: Path, mode: int = 0o600) -> None:
    """Restrict a secret file to owner-only access (S2). POSIX only.

    Only rewrites the mode when it is actually looser, so a user who chose
    a stricter mode keeps it.
    """
    if os.name == "posix" and path.exists() and path.stat().st_mode & 0o077:
        os.chmod(path, mode)


def _ensure_secret_parent(path: Path) -> None:
    """Create path's missing parent directories, each owner-only (S2).

    A fresh install could crash the first secret write (portal token,
    .env) because the app dir did not exist yet; only directories this
    call creates are tightened to 0700 — pre-existing ones keep their
    modes (ensure_dirs still owns ~/.kestrel's mode).
    """
    missing: list[Path] = []
    probe = path.parent
    while not probe.exists() and probe != probe.parent:  # `!= parent` = filesystem root
        missing.append(probe)
        probe = probe.parent
    for d in reversed(missing):
        d.mkdir()
        if os.name == "posix":
            os.chmod(d, 0o700)


def write_secret_text(path: Path, text: str) -> None:
    """Write a secret file (.env, profile.json) owner-only (S2).

    Missing parent directories are created first, owner-only, so a fresh
    install's first secret write cannot crash on a nonexistent app dir.
    os.open with mode 0o600 avoids a world-readable window between file
    creation and a later chmod. The explicit chmod at the end also tightens
    files that already existed with looser modes (rewrites). chmod is a
    no-op on Windows, which has no POSIX permission bits.
    """
    _ensure_secret_parent(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    _tighten_file(path)


def write_secret_text_atomic(path: Path, text: str) -> None:
    """write_secret_text with no torn-read window (audit F1).

    The in-place truncate-and-write of write_secret_text is invisible to a
    concurrent reader except mid-write, when it sees a truncated or
    half-written document — how a torn onboarding-state.json once reset a
    live interview. This writes a sibling temp file, fsyncs it, then
    os.replace()s it over the target: a reader sees the whole old file or
    the whole new one, never a partial one. Same 0600 semantics
    (mkstemp creates 0600; the replace carries it onto the target; the
    explicit tighten still upgrades files a previous version left loose).
    """
    _ensure_secret_parent(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
    _tighten_file(path)


def load_profile() -> dict:
    """Load user profile from ~/.kestrel/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `kestrel init` first."
        )
    # S2: tighten secrets written by older versions with default modes.
    _tighten_file(PROFILE_PATH)
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.kestrel/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def history_enabled() -> bool:
    """Document history toggle (spec D1): version every generated document.

    Reads HISTORY_ENABLED from the environment (loaded from ~/.kestrel/.env
    by load_env, managed on the portal settings page). Unset defaults to
    enabled; the off values restore the legacy overwrite-in-place behavior.
    """
    raw = os.environ.get("HISTORY_ENABLED", "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "off", "no")


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    # The tailor/cover stage-attempts cap (V3): the one number every
    # consumer reads — the queue predicates in database.STAGE_CONDITIONS,
    # the exhausted-bucket counts, and the host verbs' stage_exhausted
    # refusals. Never hardcode a literal at a call site.
    "max_tailor_attempts": 5,
    "max_cover_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    # B4: an in_progress lock older than this is treated as stale (crashed
    # worker) and may be reclaimed / cleared by reset_failed.
    "apply_stale_lock_minutes": 60,
    "viewport": "1280x900",
    "apply_engine": "claude",
}


def quote_env_value(value: str) -> str:
    """Quote a value for .env storage the way python-dotenv's own writer
    (dotenv.main.rewrite) does: single quotes, with backslash and
    single-quote escaped.

    Unquoted values round-trip wrongly through the parser when they hold
    ' #', quotes, or trailing backslashes (sweep F11): a key saved as
    `abc #123` reads back as `abc`. The escape set matches dotenv's
    single-quoted-value decoder exactly, so anything written here reads
    back identically through load_dotenv().
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def load_env():
    """Load environment variables from ~/.kestrel/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        # S2: tighten secrets written by older versions with default modes.
        _tighten_file(ENV_PATH)
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


def default_apply_model() -> str:
    """The apply engine's model when a run names none (P1-2).

    Reads AUTOAPPLY_MODEL — the Settings page's "Default apply model" —
    at call time so live saves apply; an unset or empty value falls back
    to 'haiku'. Callers run after load_env(): the portal pushes saved
    values into os.environ on save (settings._refresh_process_env) and
    start_run copies that environment into spawned children, while direct
    CLI runs load the store's .env in _bootstrap().
    """
    return os.environ.get("AUTOAPPLY_MODEL") or "haiku"


# Secret env keys whose values must never reach disk surfaces (prompt logs,
# the portal log viewer). S2/F1: the apply prompt embeds the CapSolver key
# verbatim, and provider keys can enter prompt text through custom-endpoint
# diagnostics -- so redaction boundaries need the actual configured values.
# Kept here so every consumer (apply path, portal) reads one list.
SECRET_ENV_KEYS = (
    "CAPSOLVER_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "FACTORY_API_KEY",
)


def collect_secret_values() -> list[str]:
    """Values of the known secret env keys, for redaction boundaries.

    Reads both the live environment and the .env file: a running process
    may hold stale or missing values for keys the file has since acquired.
    Empty values are skipped; duplicates across sources collapse.
    """
    seen: set[str] = set()
    values: list[str] = []
    sources: list[dict[str, str]] = [dict(os.environ)]
    if ENV_PATH.exists():
        from dotenv import dotenv_values

        file_values = dotenv_values(ENV_PATH)
        if file_values:
            sources.append(file_values)
    for source in sources:
        for key, val in source.items():
            if key in SECRET_ENV_KEYS and val and val not in seen:
                seen.add(val)
                values.append(val)
    return values


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


# Env vars that signal an LLM is configured under the legacy (LLM_PROVIDER
# unset) resolution order. Kept here so get_tier/check_tier and the wizard
# stay in sync with llm.py's legacy fallback.
_LLM_ENV_KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")


def _llm_configured() -> bool:
    """True when an LLM endpoint is configured.

    With an explicit LLM_PROVIDER of custom/local, only the endpoint URL
    counts — never a silent fallthrough to whatever other keys happen to be
    present (llm.py raises the readable startup error instead).
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if provider in ("custom", "local"):
        return bool(os.environ.get("LLM_URL"))
    return any(os.environ.get(k) for k in _LLM_ENV_KEYS)


def _engine_gap_messages() -> list[str]:
    """Human-readable reasons the auto-apply engine is not ready (tier 3).

    Distinguishes the two states the old binary-presence check blurred:
    an installed-but-unsigned-in CLI, and an installed Droid whose
    FACTORY_API_KEY is missing. Pure so tests can pin the messages.
    """
    claude = engine_auth.claude_status()
    droid = engine_auth.droid_status()
    if claude.state == engine_auth.NOT_INSTALLED and droid.state == engine_auth.NOT_INSTALLED:
        return [
            "An auto-apply engine CLI — Claude Code ([bold]https://claude.ai/code[/bold], "
            "default) or Factory Droid ([bold]https://docs.factory.ai/cli[/bold])"
        ]
    gaps: list[str] = []
    if claude.state == engine_auth.SIGNED_OUT:
        msg = "Claude Code sign-in — the CLI is installed but not signed in; run [bold]claude auth login[/bold]"
        if claude.error:
            msg += f" ({claude.error})"
        gaps.append(msg)
    if droid.state == engine_auth.SIGNED_OUT:
        gaps.append("FACTORY_API_KEY — the Droid CLI is installed; add the key to ~/.kestrel/.env to use it")
    return gaps


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + a signed-in engine (Claude Code
                                    authenticated via `claude auth status`,
                                    or Droid with FACTORY_API_KEY) + Chrome
    """
    load_env()

    if not _llm_configured():
        return 1

    engine_ready = (
        engine_auth.claude_status().state == engine_auth.SIGNED_IN
        or engine_auth.droid_status().state == engine_auth.SIGNED_IN
    )
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if engine_ready and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not _llm_configured():
        missing.append("LLM configuration — run [bold]kestrel init[/bold] or set LLM_PROVIDER / GEMINI_API_KEY")
    if required >= 3:
        missing.extend(_engine_gap_messages())
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
