# Privacy — where your data goes

Kestrel is a local-first, single-user application. Your job state, resume,
profile, and configuration live on your machine. Third parties only ever
receive what the table below says, and only when you configure that
integration.

## What stays local, always

- **SQLite database** (`~/.kestrel/`) — every job record, score, reasoning
  note, tailored resume, cover letter, and apply outcome. Nothing in the
  store is uploaded anywhere by Kestrel itself.
- **Configuration** — `profile.json`, `searches.yaml`, and `.env` live under
  `~/.kestrel/`, created with owner-only permissions (mode 0600).
- **Generated documents** — tailored resumes and PDFs are rendered locally.
- **Portal token and logs** — stored under `~/.kestrel/`; the portal binds
  to `127.0.0.1` only and is reachable solely from your machine.

## Where data goes, per integration

| Integration | When data is sent | What is sent | What comes back |
|-------------|-------------------|--------------|-----------------|
| **OpenAI-compatible endpoints** (LLM providers: Gemini, OpenAI, or a custom `LLM_URL` — including local Ollama/llama.cpp) | On every AI call: scoring, AI-powered description enrichment, resume tailoring, cover-letter generation | The job description (capped excerpt), your resume text, and the profile fields relevant to the call (contact info, work authorization, compensation, skills) | Fit scores with reasoning, tailored resume content, cover-letter text |
| **Gmail MCP** (apply engine) | Only while an apply run is active | Nothing about you leaves your machine directly — the `@gongrzhe/server-gmail-autoauth-mcp` server runs locally and talks to Google's Gmail API with your account's OAuth session; the apply agent can read email (e.g., to confirm an application landed). Destructive Gmail tools (draft, delete, modify, label, filter) are denied by name | Email content the agent reads, displayed in the run log |
| **Composio** (optional discovery source) | Only during discovery runs, when `COMPOSIO_API_KEY` is set | Your search parameters (tag, limit, country, remote-only, description flag) and your Composio user namespace | web3.career job listings (title, company, location, apply URL, optional description) |
| **Job boards and employer sites** | On every discovery/enrichment run and every application | Whatever a normal browser visit sends, plus search parameters; during applications, the form answers and documents you (or the agent, on your confirmed plan) submit | Job listings, job descriptions, application confirmations |

## Opt-down options

- **Discovery-only mode** — run `kestrel run --stage discover` and skip AI
  stages. Nothing leaves your machine except requests to the job boards
  themselves.
- **Local LLM** — point `LLM_URL` at Ollama or llama.cpp on this machine;
  your resume and profile never leave your machine, though job descriptions
  still come from the web.
- **Skip optional integrations** — Gmail MCP and Composio are opt-in; don't
  configure them and they don't run.

Provider data-retention is the provider's policy, not ours: if you use a
hosted LLM provider, pick one whose retention and training-on-inputs terms
you accept.

Questions or a privacy concern? See [SECURITY.md](SECURITY.md) for the
private reporting channel.
