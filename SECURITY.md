# Security Policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub's security
advisory workflow: on the repository's **Security** tab, choose
"Report a vulnerability". Please do **not** open a public issue for a
security report — include a description of the issue, how to reproduce it,
and the affected component (`src/kestrel/...`) if you can.

We will acknowledge reports as soon as practicable, then work with you on
a fix and a coordinated disclosure timeline.

## Threat model

Kestrel is a single-user, localhost-only job-application agent. What it
protects, and against what:

### Keys live in a local store, never in prompts or logs

- API keys and your profile live under `~/.kestrel/` — `.env` and
  `profile.json` are created with owner-only permissions (mode 0600) and
  tightened automatically if found looser.
- Provider keys are never embedded in LLM prompts or written to log files.
  The portal's settings page masks every secret read to its last four
  characters, and the run-log viewer redacts configured keys, tokens, and
  passwords before display.
- Your job-site password is never given to the apply agent. It is told to
  use the browser profile's saved session instead.

### Human-gated submissions

- Real applications never fire without an explicit plan-then-confirm
  handshake: `kestrel apply --plan` produces a same-day, sha256-pinned plan
  hash, and `kestrel apply --i-confirm <hash>` consumes that token to run
  the batch. No agent path bypasses this.
- A per-day application cap (5/day external default) is enforced live on
  every submission path — CLI, portal, and agent protocol alike — and
  cannot be lifted at the verb level.
- CAPTCHA-blocked applications are terminal: they are never auto-retried,
  and `--reset-failed` deliberately skips them.

### Localhost-only portal

- The web portal binds to `127.0.0.1` only — it is never exposed to your
  network.
- A token is generated at first launch, stored at `~/.kestrel/portal_token`
  (mode 0600), required on every request, and carried by an HttpOnly
  `SameSite=Strict` cookie.
- Non-localhost `Host` headers are refused (DNS-rebinding defense). This
  check stays on even when the token check is disabled for kiosk-style
  local runs, so an opted-out portal remains unreachable from other
  machines as long as the bind stays on `127.0.0.1`.

### Browser isolation

- Apply workers run in a dedicated automation Chrome profile, not your
  daily-driver profile. Credential stores (passwords, cookies, autofill)
  are excluded when the worker profile is first cloned, so workers start
  logged out of job sites.
- The apply agent operates with broad browser permissions over whatever
  session its profile holds — keep accounts where a stray submission would
  hurt out of the automation profile.

## What is out of scope

- A compromised machine or `~/.kestrel/` directory: keys in the local
  store are readable by anything running as your user.
- Job boards harvesting data you type into their forms during automated
  submission; that traffic is between your browser profile and the board.
- The upstream project's bundled examples.

## Ground rules for contributors

Never commit keys, tokens, `.env*.local` files, or personal machine
details (IPs, hostnames, home paths) — see [CONTRIBUTING.md](CONTRIBUTING.md).
