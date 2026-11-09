<p align="center">
  <img src="docs/assets/kestrel-wordmark.png" alt="Kestrel — the autonomous job-application agent" width="560">
</p>

<h1 align="center">Kestrel</h1>

<p align="center"><em>The patient hunter for your job search — it hovers over the whole field, strikes once, and you hold the final call.</em></p>

<p align="center">
  <a href="#how-it-works">How it works</a> ·
  <a href="#requirements">Requirements</a> ·
  <a href="#get-started">Get started</a> ·
  <a href="#your-keys-your-data">Your keys, your data</a> ·
  <a href="#contributing">Contribute</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-green.svg?style=flat-square"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg?style=flat-square"></a>
  <a href="https://github.com/Pickle-Pixel/ApplyPilot"><img alt="Forked from Pickle-Pixel/ApplyPilot" src="https://img.shields.io/badge/forked%20from-Pickle--Pixel%2FApplyPilot-blue.svg?style=flat-square"></a>
</p>

---

Kestrel is a six-stage autonomous job-application pipeline. It discovers jobs across job boards, employer portals, and direct career sites, scores them against your resume with an LLM, tailors your resume per job, writes cover letters, and drives browser-based applications — with a human confirmation gate in front of every real submission.

It is also fully drivable *by* an agent: every feature is reachable from a shell, so a coding agent can run onboarding, scoring, tailoring, and apply bookkeeping on your behalf — with Kestrel owning the state, validation, and guardrails.

> **Fork of record.** Kestrel is a fork of [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) by Pickle-Pixel, first published February 17, 2026. Upstream copyright notices are retained per the AGPL-3.0; see [NOTICE](NOTICE). We are not affiliated with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name.

---

## How it works

| Stage | What happens |
|-------|--------------|
| **1. Discover** | Scrapes Indeed, LinkedIn, Glassdoor, ZipRecruiter, and Google Jobs, plus Workday employer portals and direct career sites. Optional Composio source for web3.career. Deduplicates by URL. |
| **2. Enrich** | Fetches the full description for every job: JSON-LD structured data first, CSS selectors next, AI-powered extraction as the fallback tier. |
| **3. Score** | An LLM rates every job 1–10 against your resume and preferences. Only jobs above your threshold proceed. |
| **4. Tailor** | Reworks your resume per job — reorganizes, emphasizes relevant experience, incorporates keywords. Your declared resume facts are preserved exactly; never fabricated. |
| **5. Cover letter** | A targeted letter per job, referencing the specific company, role, and your mapped experience. |
| **6. Apply** | A browser automation engine navigates forms, uploads documents, and answers screening questions — but nothing is submitted until you confirm a plan. |

Each stage is independent: run all six, or pick the ones you want.

**Human-gated by design.** Real submissions require an explicit plan-then-confirm step (`kestrel apply --plan` → `--i-confirm <hash>`), a per-day application cap, and terminal CAPTCHA handling that never retries blocked applications.

## Requirements

| Component | Required for | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run the Playwright MCP server |
| An LLM API key | Scoring, tailoring, cover letters | Gemini (free tier is enough), OpenAI, or any OpenAI-compatible endpoint — including local (Ollama, llama.cpp) |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply (default engine) | Install from [claude.ai/code](https://claude.ai/code) |

Optional: Factory Droid CLI as an alternative apply engine, a CapSolver key for CAPTCHAs, and a Composio key for the web3.career discovery source. Discovery-only mode needs no LLM key at all.

## Get started

```bash
pip install kestrel-agent
kestrel init          # one-time setup: resume, profile, preferences, API keys
kestrel doctor        # verify your setup — shows what's installed and what's missing
kestrel run           # discover → enrich → score → tailor → cover letters
kestrel apply         # browser-driven application (plan-then-confirm gated)
kestrel portal        # optional: localhost web cockpit at http://127.0.0.1:8765
```

> **Note:** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine with any modern numpy at runtime. If `pip install kestrel-agent` fails to resolve, install it separately: `pip install --no-deps python-jobspy`, then reinstall its runtime deps (`pydantic tls-client requests markdownify regex`).

From source:

```bash
git clone https://github.com/Vontarius-Ventures/kestrel.git
cd kestrel
pip install -e ".[dev]"
playwright install chromium
```

## Your keys, your data

- Everything lives on your machine: job state in a local SQLite database under `~/.kestrel`, configuration in `~/.kestrel/.env` (created owner-only, mode 0600).
- Keys stay local. LLM calls send your resume text, profile data, and job descriptions to whichever provider *you* configure — point `LLM_URL` at a local endpoint (Ollama, llama.cpp) and nothing AI-related leaves your machine.
- The web portal binds to `127.0.0.1` only, requires a generated token, and never renders secrets back to the page.
- Where each connected provider gets data — and what never leaves the machine — is tabulated in [PRIVACY.md](PRIVACY.md). The threat model and security practices are in [SECURITY.md](SECURITY.md).

## Contributing

Kestrel is an AGPL-3.0 fork and welcomes contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the must-pass gates (`pytest`, `ruff check`), and ground rules.

---

AGPL-3.0 · [License](LICENSE) · [Notice](NOTICE) · Forked from [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) — upstream copyright retained.
