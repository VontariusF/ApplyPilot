# Contributing to Kestrel

Thank you for your interest in contributing to Kestrel — an AGPL-3.0 fork of
[Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot). This
guide covers development setup, the gates every change must pass, and the
ground rules for what never enters the repository.

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Git

### Clone and Install

```bash
git clone https://github.com/Vontarius-Ventures/kestrel.git
cd kestrel
pip install -e ".[dev]"
playwright install chromium
```

This installs Kestrel in editable mode with the development dependencies
(pytest, ruff, and the portal stack) and downloads the Chromium browser
binary for Playwright.

### Verify the installation

```bash
kestrel --version
kestrel doctor --json
```

## Must-pass gates

These must pass before you open a PR — CI runs them, and a red gate blocks
the merge:

```bash
ruff check .                 # lint (src + tests)
pytest tests -m "not e2e"    # unit suite (non-e2e)
```

For portal or onboarding changes, also run the browser e2e suite
(`pytest tests/e2e`); Chromium must be installed once per machine
(`playwright install chromium`) — without it the e2e tests silently SKIP,
and a skipped gate is no gate.

## How to contribute

1. Check existing [issues](https://github.com/Vontarius-Ventures/kestrel/issues)
   to avoid duplicating work; for new features, open an issue first to
   discuss the approach.
2. Fork the repo and create a feature branch from `main`.
3. Write code with type hints and docstrings, and add tests for new
   functionality — bug fixes need a regression test.
4. Run the must-pass gates above.
5. Submit a PR with a clear description of what changed and why.

### Adding Workday employers and career sites

Workday employer portals live in `src/kestrel/config/employers.yaml`; direct
career sites in `src/kestrel/config/sites.yaml`. Find the portal URL and
Workday instance/tenant (or the site's CSS selectors for job listings), add
an entry following the existing format, and verify with
`kestrel run --stage discover` against that employer or site before
submitting.

## Code style

- **Type hints** on all function signatures.
- **Docstrings** (Google style) on all public functions and classes.
- **snake_case** for functions and variables, **PascalCase** for classes.
- **Imports** sorted by Ruff (isort-compatible); **line length** 120
  characters.
- Auto-fix what Ruff can fix: `ruff check src/ --fix`.

## Ground rules — never commit

These are hard rules, enforced by review; violations are removed on sight:

- **Keys, tokens, or credentials of any kind** — API keys, OAuth tokens,
  session cookies, password files.
- **`.env*.local` and `*.local` files** — local-only configuration and
  secrets stay on your machine. `.env.example` (placeholders only) is the
  template that ships.
- **Personal machine details** — home-directory paths, hostnames, IPs,
  usernames, deployed-service configs (e.g., launchd plists).
- **Local databases** — SQLite files and their journals.
- **Live resume/profile data** — `profile.json` and `resume.txt` are
  git-ignored; commit only the `profile.example.json` / example fixtures.

## Project structure

```
kestrel/
├── src/kestrel/          # Main package
│   ├── cli.py            # CLI entry points
│   ├── pipeline.py       # Stage orchestration (tailor, cover letters, PDF)
│   ├── discovery/        # Stage 1: job discovery scrapers
│   ├── enrichment/       # Stage 2: description extraction
│   ├── scoring/          # Stage 3: AI scoring
│   ├── apply/            # Stage 6: browser automation (Claude/Droid engines)
│   ├── portal/           # Localhost web portal
│   ├── wizard/           # Onboarding wizard
│   ├── agent_protocol.py # Host-driven agent protocol (JSON verbs)
│   ├── database.py       # SQLite store
│   └── config/           # Bundled YAML configs (employers, sites, searches)
├── tests/                # Test suite (unit + e2e)
└── pyproject.toml        # Package configuration
```

## License

By contributing to Kestrel, you agree that your contributions will be
licensed under the [GNU Affero General Public License v3.0](LICENSE).
Kestrel retains upstream ApplyPilot attribution per [NOTICE](NOTICE).
