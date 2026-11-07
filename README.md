<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply (default engine) | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| Factory Droid CLI | Alternative auto-apply engine (`applypilot apply --engine droid`). Authenticates with `FACTORY_API_KEY`. Claude Code remains the default |
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |
| Composio API key | Extra discovery source: web3.career listings via the Composio v3.1 API. Set `COMPOSIO_API_KEY` and connect the `web3_career` toolkit (see [Composio discovery](#composio-discovery-web3career)) |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional), `COMPOSIO_API_KEY` (optional, enables the Composio discovery source).

### LLM providers

`LLM_PROVIDER` selects where scoring/tailoring calls go. When set, it is a
contract: a missing required variable is a startup error that names the
variable — never a silent fallthrough. Valid values:

| `LLM_PROVIDER` | Required env vars | Notes |
|----------------|-------------------|-------|
| `gemini` | `GEMINI_API_KEY` | Google Gemini via its OpenAI-compat endpoint (default model `gemini-2.0-flash`) |
| `openai` | `OPENAI_API_KEY` | Default model `gpt-4o-mini` |
| `custom` | `LLM_URL`, `LLM_MODEL` | Any endpoint speaking `POST {LLM_URL}/chat/completions` — OpenRouter, vLLM, llama.cpp server, Gemini's compat route. `LLM_API_KEY` optional for endpoints that need no bearer auth |
| `local` | `LLM_URL` | Ollama/llama.cpp on this machine (e.g. `http://localhost:11434/v1`) |

When `LLM_PROVIDER` is unset, the legacy detection order applies unchanged:
`GEMINI_API_KEY` → `OPENAI_API_KEY` → `LLM_URL`. `LLM_MODEL` overrides the
model name for any provider.

**Wire parameter caveat:** requests use `max_tokens`, which is accepted by
llama.cpp, Ollama, vLLM, and legacy OpenAI models. Newer OpenAI-branded
models (the `o*`/`gpt-5*` reasoning families) reject it in favor of
`max_completion_tokens` — if you target those, prefer a model id that still
accepts `max_tokens` on your endpoint.

### Composio discovery (web3.career)

An optional discovery source that pulls the newest web3.career listings into
the same store as the other boards, via the Composio v3.1 tool API
(`WEB3_CAREER_LIST_JOBS`). Enablement matches the other sources: set
`COMPOSIO_API_KEY` in `.env` (the wizard's optional Composio step writes it,
masked at input) and it runs as part of `applypilot run discover`.

| Env var | Meaning |
|---------|---------|
| `COMPOSIO_API_KEY` | Composio API key — enables the source. Never logged; `applypilot doctor` masks it to the last 4 |
| `COMPOSIO_TOOLKITS` | Comma list of toolkits (default `web3_career`) |
| `COMPOSIO_USER_ID` | Composio user namespace the ACTIVE connection lives under (default `applypilot-spike`) |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | Optional explicit connected-account id; otherwise Composio resolves your connection by user id + toolkit |

Behavior notes (from live-verified behavior, not Composio's docs):

- **Connection first:** the toolkit needs an ACTIVE connection (web3.career
  API key inside Composio — see their dashboard). ApplyPilot never creates or
  recreates connections; a missing one surfaces as a degraded-source error in
  the discovery summary.
- **Apply links are verbatim:** the provider's terms require each job's
  `apply_url` to be used unchanged — no tracking parameters, no rewrites.
  It is also the dedupe key (when descriptions are disabled the provider
  nulls ids and company names).
- **No pagination:** the provider serves the newest 100 listings per search
  (`limit` 1–100). Repeat runs dedupe on the apply URL.
- **Search parameters** live in the optional `composio:` block of
  `searches.yaml` (see `searches.example.yaml`): `tag`, `limit`, `country`,
  `remote_only`, `include_description`. Invalid values come back as
  source errors, not crashes.
- The `[composio]` extra (`pip install -e ".[composio]"`) ships the
  `composio-core` SDK for one-off account management; the discovery source
  itself uses plain HTTP and the base install is unaffected either way.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

### Privacy & data flow

**Your resume text is sent to the configured LLM provider on every AI call.** Scoring, tailoring, and cover-letter generation each transmit the full tailored resume text (plus your profile data: contact info, work authorization, compensation) to the provider selected in `.env` — Gemini, OpenAI, or a custom endpoint. Job descriptions are also sent during AI-powered enrichment. There is no redaction of this content; that is inherent to how the pipeline works.

To opt down:
- Use **discovery-only mode** (skip AI scoring/tailoring entirely) — nothing leaves your machine except job listings fetched from public boards.
- Point `LLM_URL` at a **local endpoint** (Ollama, llama.cpp) — your resume and profile stay on your machine, though job descriptions still come from the web.
- If you use a hosted provider, pick one whose data-retention terms you accept; avoid free tiers with training-on-inputs policies if that matters to you.

**Credential handling:** `profile.json` and `.env` are created with owner-only file permissions (0600) and tightened automatically if found looser. The job-site password is never embedded in agent prompts or written to log files — the apply agent is told to use the browser profile's saved credentials instead. Log files under `~/.applypilot/logs/` produced by versions before 0.3.1 may still contain passwords; scrub or delete them when upgrading.

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Pulls the newest web3.career listings via Composio (optional, `COMPOSIO_API_KEY`). Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

**The browser is a dedicated automation profile, not your daily-driver Chrome.** On first run, each worker clones your real Chrome profile as a starting point — but credential stores are excluded from the clone (saved passwords, session cookies, and autofill data do not carry over). Workers therefore start logged out of job sites; if a site requires an authenticated session, log into the worker profile manually once (`~/.applypilot/chrome-workers/worker-0`) and it will be reused on subsequent runs. Driving any browser profile with an autonomous agent is a real blast radius: the agent operates with broad browser permissions on whatever sessions that profile holds, so keep the automation profile free of accounts where a stray submission or action would hurt, and review `applypilot apply` logs.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

#### Engines

The auto-apply agent is an engine behind a small adapter interface; pick it per run:

```bash
applypilot apply --engine claude   # default: Claude Code CLI
applypilot apply --engine droid    # Factory Droid CLI (needs FACTORY_API_KEY)
export AUTOAPPLY_ENGINE=droid      # or set it as the default in ~/.applypilot/.env
```

`--model` is passed through **verbatim** to whichever engine is selected — Claude model ids for Claude, Droid model ids for Droid. Droid's `custom:<name>` models (defined in `~/.factory/config.json` with their own base URL and API key) are how Gemini models run without a machine-local Gemini key: Droid holds that credential, ApplyPilot only ever sees the model name.

Droid specifics:

- **Auth:** the child process gets `FACTORY_API_KEY` in its environment only — never in prompts or logs. `applypilot apply --engine droid` exits with a readable error if it is unset.
- **MCP config:** Droid reads one global `~/.factory/mcp.json` (unlike Claude's per-invocation `--mcp-config`). ApplyPilot merges its `playwright` + `gmail` servers into that file (preserving any user-added servers, writing mode `0600`) and bakes the per-worker CDP port into the Playwright args. Because that port is global per machine-config, **Droid runs are serialized to one worker** — `--workers N` is honored for Claude and clamped to 1 for Droid in this release.
- **Permissions:** Droid runs with `--auto medium` and an explicit `--enabled-tools` allowlist covering only the Playwright and Gmail MCP servers.

> **Note:** the Droid adapter is written against Factory's documented CLI surface (`droid exec`, `--output-format json`, its JSON event shapes). It has not yet been exercised against a real `droid` binary in CI; if Factory's actual event format or tool naming differs, the adapter's normalization layer (`src/applypilot/apply/droid.py`) is the single place to update.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset failed jobs for retry (captcha jobs are skipped)
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

#### Apply statuses

Jobs carry an `apply_status` value shown verbatim as a badge on the jobs board:

- `applied` — the application was submitted.
- `failed` — the application itself went wrong (form issue, engine error, crashed worker).
- `captcha` — the board blocked the submission with a CAPTCHA. **This is terminal by decision:** the application never happened, the job is never auto-retried, and `--reset-failed` deliberately skips it. It renders its own badge and is counted separately from failures in `applypilot status` (`captcha` vs `apply_errors`).
- `manual` — the site requires manual application (unsolvable CAPTCHA class).
- `in_progress` — a worker currently holds the job.

**Historical CAPTCHA rows predate the distinct status:** they remain `apply_status='failed'` with `apply_error='captcha'` (no data migration is shipped). If a manual cleanup is ever wanted, identify them with:

```sql
SELECT * FROM jobs
WHERE apply_status = 'failed' AND apply_error = 'captcha'
  AND (apply_task_id IS NULL OR apply_task_id != 'manual-failed');
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --engine droid         # Auto-apply with the Factory Droid engine
applypilot apply --model custom:gemini-flash  # Model id passed verbatim to the engine
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
applypilot portal                       # Launch the localhost web portal
```

---

## Onboarding by Agent (the `applypilot agent` protocol)

Any shell-capable coding agent can drive onboarding through a pure-JSON
protocol: every verb prints ONE JSON document (the authoritative "next"
payload) or an `{"error": {"code", "message", "details"}}` envelope with a
nonzero exit. The usual journey:

```
applypilot agent start                 # opens intake
applypilot agent resume --file resume.txt
applypilot agent start                 # opens the interview
applypilot agent next --reveal         # question + the resume text, redacted by default
applypilot agent answer --question <id> --choice <value> | --free-text <text> | --skip
applypilot agent review --set email=you@example.com
applypilot agent confirm               # writes profile.json / profile.md / searches.yaml
```

### Host mode — zero LLM calls

When neither Claude Code is signed in nor an LLM provider is configured,
`agent start` no longer refuses: the interview opens on the **host**
backend, meaning the driving agent IS the intelligence. The service layer
makes zero LLM or Claude calls for the entire journey — no kickoff turn, no
draft extraction, no per-answer acknowledgements. The driver reads the
resume itself (`agent next --reveal`) and answers every question with
`agent answer`; review and confirm are pure local computation.

Contract in host mode:

- `next` reports `"backend": "host"` and `needs.provider_setup: true` —
  the flag keeps its name but is **informational**: it means there is no
  backend for free-form chat, and it blocks nothing.
- `send` (free-form chat) stays backend-only: it exits nonzero with code
  `provider_setup` and points the driver back to `agent answer / resume /
  review`. There is no canned echo.
- Start-over (`agent redo`) is a pure local reset.
- Prefills are a backend (extraction) feature: none in host mode.

When a backend IS configured, nothing changes: claude is preferred, then
the LLM provider, and the portal keeps its readiness gate — human flows
never select the host backend.

---

## Local Web Portal

A single-user web cockpit over the same store and config the CLI uses. The
CLI stays the engine; the portal never replaces it.

```
pip install 'applypilot[portal]'
applypilot portal                      # serves http://127.0.0.1:8765
applypilot portal --port 9000
```

**Pages**

| Page | What it does |
|------|--------------|
| Dashboard | Stage counters, score distribution, recent apply outcomes, live worker states via SSE while a run is active |
| Jobs | Filter by stage/score, inspect full description, score reasoning, tailored documents; manual mark-applied / mark-failed |
| Runs | Start pipeline and apply runs, watch a live (redacted) log tail, cancel — the run's whole process group is killed, then Chrome cleanup |
| Settings | Edit the `.env` surface (LLM provider triple, API keys, apply model/engine defaults) — reads are masked to the last 4 characters |

**Security model**

- Binds to `127.0.0.1` only — never exposed to your network.
- A token is generated at first launch and stored at `~/.applypilot/portal_token`
  (mode 0600). Every request requires it: open the URL the CLI prints
  (`http://127.0.0.1:8765/?token=…`) once, and the browser keeps it as an
  HttpOnly `SameSite=Strict` cookie. Non-localhost `Host` headers are refused
  (DNS-rebinding defense).
- The token check can be opted out for kiosk-style local runs by setting
  `APPLIPILOT_PORTAL_DISABLE_AUTH=1` (truthy: `1`/`true`/`yes`) in the
  portal's environment — the name matches the pre-existing launchd
  deployments that already set it. This relaxes only the token check: the
  portal still refuses non-localhost `Host` headers, so an opted-out portal
  remains unreachable from other machines as long as the bind stays on
  `127.0.0.1`. Like `APPLYPILOT_DIR`, it is read from the portal process's
  environment (the store's `.env` is loaded into that environment at
  startup); it is deliberately not a Settings-page field.
- Secrets are never rendered back to the page — settings reads are masked to
  the last four characters, and the run log viewer redacts configured API
  keys, tokens, and passwords before display.
- Pipeline/apply runs execute as their own subprocesses (the package's own
  entry points), so cancel is a process-group kill and a portal restart never
  orphans your jobs silently — dead runs are detected at startup and marked.

**Engine note:** the Runs page grows an engine selector when engine selection
(Droid adapter) lands; on the current release the apply engine is Claude Code.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
