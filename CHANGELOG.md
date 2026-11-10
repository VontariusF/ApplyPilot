# Changelog

All notable changes to Kestrel will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `kestrel jobs filter` now defaults to `--sort score` (fit score descending),
  so the triage view is ordered by the score a human sorts by; pass
  `--sort discovered` to restore the previous discovery-order default. The
  default table is also pruned to the triage columns (Score, Title, Location,
  Salary, Posted, short URL) — every dropped column remains available via
  `--json`, `--urls`, and the existing filter flags.

## [0.1.0] - 2026-11-09

First public release of Kestrel, the open-source job-search and application
assistant.

### About

Kestrel helps candidates discover and evaluate jobs, prepare tailored
application materials, and manage applications through a human-supervised,
localhost-first workflow. This release is cut from a fresh versioning start:
`0.1.0` restarts the version lineage and this changelog begins anew.

### Lineage

Kestrel is a fork of [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
by Pickle-Pixel, released under the GNU Affero General Public License
v3.0. Upstream copyright is retained; Kestrel's changes continue to be
distributed under AGPL-3.0. See `LICENSE` and `NOTICE` for attribution
details.

### Added (carried from the fork)

- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support with a preconfigured employer registry
- Direct career site scraping with a 3-tier extraction cascade
  (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale) and composable
  filters with reranking and score history
- Resume tailoring with factual preservation (no fabrication) and
  per-job cover letter generation
- Autonomous browser-based application submission via Playwright, with
  plan-then-confirm controls, attempt limits, and human-gated submissions
- Typer-based CLI (`kestrel`), machine-readable `status --json` and
  `doctor --json` output, and an agent-driven onboarding flow
- Optional single-user FastAPI portal (localhost, auth by default)
- Multi-provider LLM support (Gemini, OpenAI, local models via
  OpenAI-compatible endpoints)
- Local SQLite storage under `~/.kestrel`

### Changed from upstream

- Package distribution and PyPI name: `kestrel-agent` (CLI: `kestrel`)
- Default data directory: `~/.kestrel` (migrates from `~/.applypilot`
  on first run; `KESTREL_DIR` replaces `APPLYPILOT_DIR` with the legacy
  variable still honored as a fallback)
- Environment namespace: `KESTREL_*` replaces `APPLYPILOT_*`, with legacy
  fallbacks for compatibility
- Privacy: removed personal fixtures, machine-specific paths, and
  internal transcripts; rebalanced the example employer registry toward a
  geographically neutral sample
