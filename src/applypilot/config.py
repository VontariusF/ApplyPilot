"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))

# Core paths
DB_PATH = APP_DIR / "applypilot.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

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
    modes (ensure_dirs still owns ~/.applypilot's mode).
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


def load_profile() -> dict:
    """Load user profile from ~/.applypilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    # S2: tighten secrets written by older versions with default modes.
    _tighten_file(PROFILE_PATH)
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.applypilot/searches.yaml."""
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
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    # B4: an in_progress lock older than this is treated as stale (crashed
    # worker) and may be reclaimed / cleared by reset_failed.
    "apply_stale_lock_minutes": 60,
    "viewport": "1280x900",
    "apply_engine": "claude",
}


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        # S2: tighten secrets written by older versions with default modes.
        _tighten_file(ENV_PATH)
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


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


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code or Droid CLI + Chrome
    """
    load_env()

    if not _llm_configured():
        return 1

    has_claude = shutil.which("claude") is not None
    has_droid = shutil.which("droid") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if (has_claude or has_droid) and has_chrome:
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
        missing.append("LLM configuration — run [bold]applypilot init[/bold] or set LLM_PROVIDER / GEMINI_API_KEY")
    if required >= 3:
        if not shutil.which("claude") and not shutil.which("droid"):
            missing.append(
                "An auto-apply engine CLI — Claude Code ([bold]https://claude.ai/code[/bold], "
                "default) or Factory Droid ([bold]https://docs.factory.ai/cli[/bold])"
            )
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
