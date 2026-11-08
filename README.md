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

### Scoring by Agent (host-driven pipeline, T1)

The same protocol now covers the scoring stage: with no LLM provider key
anywhere, a driving agent IS the scoring intelligence. It reads each job
from the queue, judges fit against the resume it onboarded with, and
persists the score through the same write path the LLM stage uses — one
UPDATE, stamped `scored_by: 'host'` for provenance (provider-written
scores carry the provider name; pre-provenance rows stay NULL).

```
applypilot agent job-next --stage score   # next pending job + rubric + progress
applypilot agent job-next --stage score --reveal   # resume text + resume_facts
applypilot agent score --url <url> --score 8 --keywords "python, sql" \
    --reasoning "Direct experience across most requirements."
applypilot agent score --url <url> --score 9 --reasoning "..." --rescore   # overwrite
```

The loop: `job-next` until its `job` is `null` (`progress.unscored == 0`).
`agent score` returns the next pending payload, so one call per job
drives the whole queue.

- The `job-next` payload carries the exact 1-10 rubric text the LLM path
  scores with, a description excerpt capped at the same 6000 chars the
  LLM sees, and progress counts — including `pending_detail`, the count
  of jobs that never captured a description (they cannot be scored until
  an enrich pass captures one).
- Validation matches the LLM path: scores outside 1-10 or empty
  reasoning are refused with an `invalid_score` envelope; scoring an
  already-scored job without `--rescore` returns `already_scored`.
- Redaction is the PR #58 pattern: no resume text or `resume_facts` in a
  default payload — `--reveal` opts in per call.
- These verbs never touch the LLM tier gate: they run on a store with
  zero provider configuration.

### Enrich, Tailor, and Cover by Agent (host-driven pipeline, T2)

The same protocol now covers the three remaining LLM seams: description
capture when the HTML tier misses (the `extract_with_llm` fallback),
resume tailoring, and cover letters. In every case the driving agent
supplies the text and the CLI runs the same local validation and
persistence the LLM path uses — the LLM judge that normally reviews
tailor output is skipped in host mode (validation is lenient, exactly
like a portal run).

```
applypilot agent job-next --stage enrich            # stranded jobs (no description yet)
applypilot agent enrich --url <url> \
    --full-description "..." [--application-url "..."]
applypilot agent job-next --stage tailor            # scored jobs missing materials
applypilot agent tailor --url <url> --data '<resume JSON>'
applypilot agent job-next --stage cover             # tailored jobs missing letters
applypilot agent cover --url <url> --text "..."
```

- Each stage's `job-next` serves that stage's queue (enrich: pending
  detail jobs; tailor: scored jobs with a description and no tailored
  resume; cover: tailored jobs with no cover letter) and carries the
  same shape as score: job block with a 6000-char description excerpt,
  progress counts for the whole pipeline, and redaction by default.
  `--reveal` adds the resume text and `resume_facts`; the tailor stage
  also adds the profile slice the assembler uses (skills boundary,
  experience, education — never personal fields) and a `data_template`
  skeleton for the `--data` JSON.
- `agent enrich` writes the description capture through the same
  persistence path the scraper's fallback tier uses (same stamps, same
  `pending_detail` -> score-queue transition). It refuses an unknown job
  (`unknown_job`), an empty description (`invalid_description`), and a
  recapture (`already_enriched`).
- `agent tailor` runs the local validator then the assembler. A failed
  validation is a retryable `invalid_resume_data` envelope carrying the
  validator's errors as `avoid_notes` — fix them and call again;
  nothing is persisted on failure. Lenient mode skips banned-filler
  checks but never fabrication: skills outside the profile's boundary
  are still refused.
- `agent cover` sanitizes the letter (the same dash handling the LLM
  path applies), runs `validate_cover_letter`, and refuses invalid
  letters with a retryable `invalid_cover_letter` envelope. Covering a
  job without a tailored resume is `not_coverable`; a second letter is
  `already_covered`.
- Every `job-next` payload reports `progress.pending_detail`, so a host
  loop can see stranded jobs instead of dropping them: drain the enrich
  queue and those jobs re-enter the score queue.
- Like the scoring verbs, none of these touch the LLM tier gate — they
  run on a store with zero provider configuration.

### Apply Bookkeeping by Agent (host-driven pipeline, T3)

The same protocol now covers submission bookkeeping. `agent apply-acquire`
takes the launcher's per-job lock on the next eligible job (the same
`acquire_job` path a worker run uses, stamped `host-worker-0`), with the
live daily-cap recheck in front — the 5/day external default applies and
cannot be lifted at the verb. `agent apply-result` records the outcome
through the same `mark_result` write the worker loop uses, releases the
lock, and reports the remaining cap headroom.

```
applypilot agent apply-acquire                        # next eligible job, locked
applypilot agent apply-acquire --min-score 8          # raise the fit threshold
applypilot agent apply-acquire --reveal               # resume text + resume_facts
applypilot agent apply-result --url <url> --status applied
applypilot agent apply-result --url <url> --status captcha --reason "blocked"
```

**Guardrails — read before driving a real submission:**

- **The verbs are bookkeeping only.** They take a lock and record an
  outcome. No verb invokes an apply engine, launches Chrome, or touches a
  job board: the driving agent performs the real browser work itself, with
  its own tooling, after acquiring the job.
- **Plan-then-confirm stays required upstream.** These verbs do NOT plan,
  stage, or consume the PR #54 token: a real submission batch must still
  go through `applypilot apply --plan` and `--i-confirm <hash>` (same-day,
  sha256-pinned, consumed on use). A confirmed plan pins the batch; the
  verbs honor the pin because they sit inside the same lock and cap
  machinery — they do not bypass it.
- **The cap is not liftable at the verb.** `apply-acquire` checks the
  daily count live (applications recorded by any path — portal, CLI,
  protocol — all count) and refuses with `cap_reached` once the 5/day
  external default is hit.
- **Status vocabulary is the PR #57 set, exactly:** `applied`, `failed`,
  `expired`, `captcha`, `login_issue`. `captcha` (like `expired` and
  `login_issue`) is recorded terminal — attempts banned, the job never
  requeues. Unknown values are refused with `invalid_status`.
- **Lock discipline.** `apply-result` reports only acquired jobs: a
  non-acquired URL is refused (`not_acquired`), and a locked queue
  answers `queue_busy` instead of fabricating work. Human corrections
  belong on the portal board or `apply --mark-applied/--mark-failed`,
  which carry the launcher's coordination rules.

Refusal envelopes: `cap_reached` (daily cap hit, no lock taken),
`no_eligible_job` (nothing passes the queue's filters),
`queue_busy` (fresh locks hold every candidate — retry later),
`not_acquired` (apply-result without a held lock), `invalid_status`,
`invalid_min_score`.

---

## Driving ApplyPilot as an agent

Every feature in ApplyPilot is reachable from a shell — which means every
feature is drivable by a host coding agent with **zero LLM provider keys**.
The division of labor is fixed: the driving agent supplies the intelligence
(judgments, text, browser work); ApplyPilot owns state, validation,
persistence, and guardrails. The `agent` protocol verbs and the plain CLI
are two surfaces over the same store — nothing below requires a provider
credential unless its row says so.

| Feature | Agent-CLI path | Notes |
|---------|----------------|-------|
| Onboarding / init | `agent start` → `agent resume --file` → `agent start` → `agent next` / `agent answer` / `agent send` → `agent review --set` → `agent confirm` | Host backend when nothing is ready — zero LLM calls. `agent keep-editing`, `agent redo`, `agent agent-led on\|off` round out the interview. |
| Discover | `run --stage discover` | Plain CLI, LLM-free (jobspy/Workday sources; Composio optional). Not a protocol verb — plain-CLI reachability is the mapping. The optional smartextract strategy uses an LLM, but is never required. |
| Enrich | `run --stage enrich` (HTML tier, LLM-free) · fallback: `agent job-next --stage enrich` + `agent enrich --url … --full-description …` | The host verb is the fallback tier: it captures descriptions the HTML tier missed and moves the job into the score queue. |
| Score | `agent job-next --stage score` + `agent score --url … --score N --reasoning …` | The 1-10 rubric rides the job-next payload; writes go through the same UPDATE as the LLM path, stamped `scored_by: 'host'`. |
| Rescore | `agent score --url … --rescore` (host) · `run rescore` (provider) | Same overwrite semantics both paths; `--rescore` without the flag on a scored job is refused (`already_scored`). |
| Tailor | `agent job-next --stage tailor` + `agent tailor --url … --data '<resume JSON>'` | Lenient local validator + assembler; the LLM judge is skipped. Validator failures return a retryable envelope carrying the errors as avoid-notes. |
| Cover letter | `agent job-next --stage cover` + `agent cover --url … --text …` | Local `validate_cover_letter` after the same sanitization the LLM path applies. |
| PDF | `run --stage pdf` | Pure local rendering — plain CLI, no protocol verb needed. |
| Regenerate documents | `regenerate <url> [--cover]` | Plain CLI: clears tailoring state and re-enqueues; existing files stay on disk (history). |
| Pipeline status / loop termination | `status --json` | `progress.unscored == 0` terminates the scoring loop; `progress.pending_detail` counts stranded jobs (no captured description) on every payload. |
| Diagnostics | `doctor --json` | Plain CLI, machine-readable. |
| Apply planning (guardrail) | `apply --plan [--min-score N] [--limit N]` | Pure JSON: plan hash (sha256, same-day validity), cap, live headroom. Stages the token; commits nothing. |
| Apply run (worker-engine path) | `apply --i-confirm <hash>` | Consumes the token and runs the batch through a Tier-3 engine (Claude Code / Droid) — needs the engine credential and local Chrome. |
| Apply bookkeeping (host path) | `agent apply-acquire` / `agent apply-result --url … --status applied\|failed\|expired\|captcha\|login_issue [--reason …]` | Takes/releases the per-job lock through the launcher's own seams, with the live daily-cap recheck. Bookkeeping only — the real browser work is the driving agent's, and plan-then-confirm stays required upstream. |
| Apply corrections | `apply --mark-applied <url>` / `--mark-failed <url>` / `--reset-failed` | Plain CLI (human corrections; the launcher's coordination rules apply). |
| Portal | `portal` | Separate surface; its routes keep their provider gates — out of host scope this round. |

### The two host loops

The scoring loop — terminates when `status --json` (or any payload's
`progress`) reports `unscored == 0`:

```
loop:
  payload = agent job-next --stage score
  if payload.progress.unscored == 0: break
  # read url/title/site/location/salary + the description excerpt
  # (6000-char cap) and judge fit against the onboarded profile
  agent score --url <url> --score <1-10> --keywords <…> --reasoning <…>
  # → the same UPDATE as the LLM path, scored_by='host'; the response
  #   carries the next pending payload, so one call per job drains the queue

stragglers: payload.progress.pending_detail > 0
  → agent job-next --stage enrich → agent enrich --url X --full-description …
  → those jobs re-enter the score queue
```

The apply bookkeeping loop — plan-then-confirm is required upstream and
un-bypassable; the real browser work stays with the driving agent:

```
apply --plan --min-score N          # pure JSON: plan hash, cap headroom
apply --i-confirm <hash>            # pins the batch (same-day, consumed on use)

loop:
  job = agent apply-acquire         # per-job lock + live daily-cap recheck
  # the driving agent performs the REAL browser work itself (its own
  # Chrome/Playwright tooling against the live board) — no verb fakes this
  agent apply-result --url <url> --status applied|failed|captcha|… --reason …
```

Tailor and cover sit between the two: the same loop shape as scoring, with
the validator returning retryable error envelopes (avoid-notes) instead of
the queue moving on.

### Operational notes

- **Scoring has no per-job lock — deliberate.** A host scoring loop racing
  a streaming `run --stage score` can double-score a job last-write-wins.
  Benign at this scale (both writes are valid scores over the same inputs)
  and documented here rather than engineered: apply keeps its lock
  machinery; scoring stays lock-free.
- **`scored_by` semantics.** Host-written scores carry `'host'`;
  provider-written scores carry the provider name; rows scored before the
  column existed carry NULL and render exactly as they always did (no
  backfill). `status --json` reports the provenance split, and the summary
  label reads the column instead of assuming every score came from an LLM.
- **Redaction defaults.** Protocol stdout lands in host chat logs, so
  payloads are redacted by default: the description is excerpt-capped at
  the same 6000 chars the LLM path sees, the resume travels as metadata
  only, and `resume_facts` are withheld. `--reveal` opts in per call —
  pass it when the driver genuinely needs the text to judge, author, or
  validate.

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
