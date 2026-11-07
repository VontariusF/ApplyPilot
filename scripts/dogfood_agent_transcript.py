"""Scripted host-agent dogfood: the full agent-protocol journey as a transcript.

Final verification for spec art_A71wyYS4 on the merged test-suite head: one
host-agent simulation plays the protocol exactly the way the feasibility
transcript (art_Kc1yCL3A section 4) draws it —

    agent start → next → resume → start (kickoff) → agent-led send
    → answer xN → review --set → keep-editing → agent-led off → confirm
    → run discover enrich score → run tailor cover pdf
    → apply --plan → apply --i-confirm → status --json

...plus every remaining protocol verb (agent-led on/off, keep-editing,
redo) and two negative probes that demonstrate the JSON error envelope.

Rig (the deterministic stub-provider rig of the merged suites, art_M7Xt2rnH):

- The onboarding backend and the apply engine both resolve `claude` off
  PATH; the harness installs a scripted fake that answers `auth status`,
  plays the print-mode interview contract (`-p --output-format json`),
  and plays the apply engine's stream-json contract — RESULT:APPLIED for
  one job, RESULT:CAPTCHA for the other, so the real launcher's RESULT
  scan and status writes are exercised on both paths.
- The LLM provider is a real localhost OpenAI-compatible stub server
  (LLM_PROVIDER=custom → LLM_URL): scoring, tailoring (with the LLM
  judge), and cover letters make real HTTP calls against it.
- Discovery and enrichment are stubbed at their network seams (the same
  seams tests/test_agent_journey.py pins): the stage code runs for real,
  the scrapers do not touch the network.
- Chromium (Playwright's) renders the PDFs and serves as the apply
  worker browser via CHROME_PATH.

Everything runs in throwaway directories; nothing outside the run dir is
touched. The transcript lands at $DOGFOOD_OUT (default: stdout + run dir).

Run:  python scripts/dogfood_agent_transcript.py [--out PATH]
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = Path(os.environ.get("DOGFOOD_RUN_DIR", "/home/user/work/dogfood-run"))

# --- Environment BEFORE any applypilot import binds paths at import time. ---
RUN_DIR.mkdir(parents=True, exist_ok=True)
os.environ["APPLYPILOT_DIR"] = str(RUN_DIR / "applypilot")

BIN_DIR = RUN_DIR / "bin"
BIN_DIR.mkdir(exist_ok=True)
os.environ["PATH"] = f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"

_CHROME_CANDIDATES = sorted(
    Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
if not _CHROME_CANDIDATES:
    sys.exit("no Playwright Chromium found — run: playwright install chromium")
os.environ["CHROME_PATH"] = str(_CHROME_CANDIDATES[-1])
os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("TERM", "dumb")
os.environ.setdefault("COLUMNS", "100")


# ---------------------------------------------------------------------------
# The LLM stub: a real OpenAI-compatible localhost endpoint
# ---------------------------------------------------------------------------

class _StubState:
    """Records what the provider saw, in order, across threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def record(self, kind: str, marker: str) -> None:
        with self.lock:
            self.calls.append({"kind": kind, "marker": marker})


STUB = _StubState()

SCORE_REPLY = ("SCORE: 9\nKEYWORDS: python, sql, airflow\n"
               "REASONING: Direct match on every pipeline skill in the posting.")

TAILOR_JSON = {
    "title": "Senior Data Engineer",
    "summary": ("Data engineer with six years of Python and SQL pipeline work, "
                "most recently orchestrating Airflow workflows for analytics teams."),
    "skills": {"Languages": "Python, SQL", "Orchestration": "Airflow",
               "Modeling": "dbt"},
    "experience": [
        {"header": "Data Engineer, Example Co (2020 - present)",
         "subtitle": "Analytics platform team",
         "bullets": ["Built Python and SQL pipelines feeding analytics dashboards.",
                     "Orchestrated nightly Airflow workflows for reporting."]},
    ],
    "projects": [],
    "education": "B.S. Computer Science",
}

COVER_LETTER = (
    "Dear Hiring Manager,\n"
    "\n"
    "The Data Engineer opening on exampleboard matches the work I do: Python\n"
    "and SQL pipelines, orchestrated in Airflow, feeding analytics that people\n"
    "act on. My last six years were spent building exactly those systems.\n"
    "\n"
    "I would welcome the chance to talk about the role.\n"
    "\n"
    "Sincerely,\n"
    "Test Person\n"
)

JUDGE_REPLY = "VERDICT: PASS\nISSUES: none"


def _route_reply(body: str) -> str:
    """One deterministic reply per call kind, routed by prompt markers."""
    if "Judge this tailored resume" in body:
        STUB.record("judge", "Judge this tailored resume")
        return JUDGE_REPLY
    if "You are a job fit evaluator" in body:
        STUB.record("score", "SCORE_PROMPT")
        return SCORE_REPLY
    if "cover letter" in body.lower():
        STUB.record("cover_letter", "cover-letter prompt")
        return COVER_LETTER
    STUB.record("tailor", "tailor prompt")
    return json.dumps(TAILOR_JSON)


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the default stderr chatter
        pass

    def do_GET(self):  # noqa: N802  (http.server API)
        payload = json.dumps(
            {"object": "list", "data": [{"id": "stub-model", "object": "model"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802  (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        reply = _route_reply(body)
        payload = json.dumps({
            "id": "stub-" + str(len(STUB.calls)),
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_stub_server() -> tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1", server


# ---------------------------------------------------------------------------
# The scripted `claude` fake: auth status + interview print-mode + apply engine
# ---------------------------------------------------------------------------

PROFILE_BLOCK = """Here is your profile.

```json
{"_applypilot_profile": {
    "personal": {"full_name": "Test Person", "email": "test@example.com"},
    "work_authorization": {"legally_authorized_to_work": "Yes",
                           "require_sponsorship": "No"},
    "availability": {"earliest_start_date": "Immediately"},
    "compensation": {"salary_expectation": "140000", "salary_currency": "USD"},
    "experience": {"current_title": "Data Engineer",
                   "years_of_experience_total": "6",
                   "target_role": "Data Engineer"},
    "skills_boundary": {"programming_languages": "Python, SQL"},
    "resume_facts": {},
    "eeo_voluntary": {}},
 "_applypilot_search": {"queries": ["Data Engineer", "Analytics Engineer"],
                        "location": "Austin, TX", "remote": true}}
```
"""

os.environ["FAKE_PROFILE_BLOCK"] = PROFILE_BLOCK

# A partial draft for the first document-informed chat turn: enough to feed
# the prefill bridge (title + languages) without closing the agenda.
PARTIAL_BLOCK = """Here's what I have so far.

```json
{"_applypilot_profile": {
    "experience": {"current_title": "Data Engineer",
                   "years_of_experience_total": "6"},
    "skills_boundary": {"programming_languages": "Python, SQL"}},
 "_applypilot_search": {"queries": ["Data Engineer"],
                        "location": "Austin, TX", "remote": true}}
```
"""

os.environ["FAKE_PARTIAL_BLOCK"] = PARTIAL_BLOCK

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Scripted claude fake: auth status, print-mode interview, apply engine."""
import json
import os
import sys

# The draft the interview hands back on every resumed turn — injected by the
# harness via the environment so this standalone script stays self-contained.
PROFILE_BLOCK = os.environ.get("FAKE_PROFILE_BLOCK", "")
PARTIAL_BLOCK = os.environ.get("FAKE_PARTIAL_BLOCK", "")

args = sys.argv[1:]
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args[:6]}) + "\n")


def envelope(result, is_error=False):
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "result": result, "session_id": "fake-session-1",
    }))


if args[:2] == ["auth", "status"]:
    print(json.dumps({"authMethod": "claude.ai"}))
    sys.exit(0)

if args[:3] == ["-p", "--output-format", "json"]:
    rest = args[3:]
    resume = None
    if len(rest) >= 2 and rest[0] == "--resume":
        resume, rest = rest[1], rest[2:]
    prompt = rest[0] if rest else ""
    if "opened onboarding" in prompt:      # kickoff turn
        envelope("Hello! Tell me about your background.")
    elif "switched to agent-led" in prompt:
        # Mode directive: a chat reply, no draft — the guided agenda keeps
        # driving until the host hands the draft over explicitly.
        envelope("Got it — I'll take it from here. What's your full name?")
    elif os.environ.get("FAKE_CLAUDE_PROFILE") == "1":
        # The draft hand-off turn: the full fenced profile block.
        envelope("Perfect — here is everything we discussed.\n\n" + PROFILE_BLOCK)
    elif os.environ.get("FAKE_CLAUDE_DRAFT") == "1":
        # A document-informed turn with a partial draft: feeds the prefill
        # bridge while the pending resume context is still unspent.
        envelope("Got it — leading with your background.\n\n" + PARTIAL_BLOCK)
    elif resume == "fake-session-1":       # plain chat turn
        envelope("Noted — tell me more when you're ready.")
    elif resume is not None:
        sys.stderr.write("unknown session: %s\n" % resume)
        sys.exit(2)
    else:
        sys.stderr.write("user turn without --resume\n")
        sys.exit(2)
    sys.exit(0)

# Apply engine mode: claude --model M -p --mcp-config ... --output-format
# stream-json --verbose -   (the prompt arrives on stdin; the verdict is
# deterministic per job — the beta URL gets a captcha, everything else applies)
if "--output-format" in args and "stream-json" in args:
    prompt = sys.stdin.read()
    if "jobs.example/beta" in prompt:
        text = ("Opened the posting, but a captcha blocked submission. "
                "RESULT:CAPTCHA — retry later.")
    else:
        text = ("Opened the posting, completed the application form, and "
                "submitted. RESULT:APPLIED")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "apply-1"}))
    print(json.dumps({"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": text}]}}))
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "session_id": "apply-1", "num_turns": 1,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "total_cost_usd": 0.001}))
    sys.exit(0)

sys.stderr.write("unexpected argv: %r\n" % (args,))
sys.exit(2)
'''


def _install_fake_claude() -> Path:
    fake = BIN_DIR / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


# ---------------------------------------------------------------------------
# The transcript writer
# ---------------------------------------------------------------------------

class Transcript:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.failures: list[str] = []

    def h(self, level: int, text: str) -> None:
        self.parts.append(f"\n{'#' * level} {text}\n")

    def cmd(self, argv: list[str], output: str, exit_code: int) -> None:
        rendered = " ".join(a if " " not in a else f'"{a}"' for a in argv)
        self.parts.append(f"```bash\n$ applypilot {rendered}\n```\n")
        if exit_code == 0:
            self.parts.append(f"```json\n{output.strip()}\n```\n")
        else:
            self.parts.append(f"```\n(exit {exit_code})\n{output.strip()}\n```\n")

    def note(self, text: str) -> None:
        self.parts.append(f"{text}\n")

    def block(self, body: str) -> None:
        """Dump raw evidence (e.g. an engine run log) verbatim."""
        self.parts.append(f"```\n{body.strip()}\n```\n")

    def flag(self, text: str) -> None:
        """A disclosed observation — never silently absorbed."""
        self.parts.append(f"> FLAGGED: {text}\n")

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        line = f"- {mark}: {label}" + (f" — {detail}" if detail else "")
        self.parts.append(line + "\n")
        if not ok:
            self.failures.append(label)

    def text_out(self) -> str:
        return "\n".join(self.parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    llm_url, server = _start_stub_server()
    os.environ["LLM_PROVIDER"] = "custom"
    os.environ["LLM_URL"] = llm_url
    os.environ["LLM_MODEL"] = "stub-model"
    os.environ.pop("LLM_API_KEY", None)
    os.environ["FAKE_CLAUDE_LOG"] = str(RUN_DIR / "claude-prompts.log")

    fake = _install_fake_claude()

    import pytest
    from typer.testing import CliRunner

    import applypilot.scoring.scorer as scorer  # noqa: F401  (RESUME_PATH binds once)
    from applypilot import config, database, onboarding
    from applypilot.apply import planning
    from applypilot.cli import app

    mp = pytest.MonkeyPatch()
    runner = CliRunner()
    t = Transcript()

    t.h(1, "ApplyPilot agent-protocol dogfood transcript")
    t.note(f"Product head under test: `{_git(['merge-base', 'origin/main', 'HEAD'])}` "
           f"(origin/main — the harness adds scripts only). Branch head: `{_git_head()}`.")
    t.note(f"Run directory (throwaway): `{RUN_DIR}` · fake claude: `{fake}` · "
           f"LLM stub: `{llm_url}` · Chromium: `{os.environ['CHROME_PATH']}`")
    t.note("")
    t.note("Rig boundaries, disclosed up front (the deterministic stub-provider rig,")
    t.note("the same seams tests/test_agent_journey.py pins):")
    t.note("")
    t.note("- Onboarding interview turns ride a scripted `claude -p` fake (print-mode")
    t.note("  contract pinned by tests/test_onboarding_agent.py).")
    t.note("- Scoring, tailoring, judging, and cover letters make real HTTP calls to a")
    t.note("  localhost OpenAI-compatible stub server (`LLM_PROVIDER=custom`).")
    t.note("- Discovery and enrichment are stubbed at their network seams; the stage")
    t.note("  code, DB writes, and scoring run for real.")
    t.note("- PDFs render through Playwright Chromium; the apply run drives the real")
    t.note("  launcher (Chrome worker + engine subprocess) with the fake engine")
    t.note("  emitting stream-json and the pinned RESULT verdicts.")
    t.note("- Every protocol verb below is the real CLI in a fresh invocation surface;")

    # ---- Stub the discovery/enrichment network seams (stage code stays real).
    from applypilot.discovery import composio, jobspy, smartextract, workday
    from applypilot.enrichment import detail

    JOBS = [
        {"url": "https://jobs.example/alpha", "title": "Data Engineer",
         "site": "exampleboard"},
        {"url": "https://jobs.example/beta", "title": "Analytics Engineer",
         "site": "exampleboard"},
    ]

    def seed_jobspy(results_per_site=None):
        conn = database.get_connection()
        for job in JOBS:
            conn.execute(
                "INSERT INTO jobs (url, title, site, discovered_at) VALUES (?, ?, ?, ?)",
                (job["url"], job["title"], job["site"],
                 "2026-11-07T00:00:00+00:00"))
        conn.commit()

    mp.setattr(jobspy, "run_discovery", seed_jobspy)
    mp.setattr(workday, "run_workday_discovery", lambda workers=1: None)
    mp.setattr(smartextract, "run_smart_extract", lambda workers=1: None)
    mp.setattr(composio, "run_composio_discovery", lambda: None)

    def fill_descriptions(workers=1, limit=100):
        conn = database.get_connection()
        for job in JOBS:
            conn.execute(
                "UPDATE jobs SET full_description = ?, application_url = ? WHERE url = ?",
                (f"Full posting for {job['title']}: Python, SQL, Airflow, dbt.",
                 job["url"], job["url"]))
        conn.commit()

    mp.setattr(detail, "run_enrichment", fill_descriptions)

    def run(argv: list[str], compact: bool = False) -> str:
        """Invoke one real CLI verb; narrate it; fail loudly on nonzero exit.

        compact=True renders the payload on one line — used for the
        repeated drain-loop payloads so the transcript stays readable
        without dropping any of the wire traffic.
        """
        result = runner.invoke(app, argv)
        body = result.output.strip()
        if compact and body.startswith("{"):
            try:
                body = json.dumps(json.loads(body), separators=(",", ":"))
            except json.JSONDecodeError:
                pass
        t.cmd(argv, body, result.exit_code)
        if result.exit_code != 0:
            t.failures.append(f"unexpected nonzero exit: applypilot {' '.join(argv)}")
        return result.output

    def run_probe(argv: list[str], expected_code: str) -> None:
        """Invoke a verb expected to REFUSE; narrate the error envelope."""
        result = runner.invoke(app, argv)
        t.cmd(argv, result.output, result.exit_code)
        try:
            code = json.loads(result.output)["error"]["code"]
        except Exception:
            code = "<unparseable>"
        t.check(f"refusal envelope code == {expected_code}",
                result.exit_code == 1 and code == expected_code, f"got {code!r}")

    def payload(out: str) -> dict:
        return json.loads(out)

    # =====================================================================
    t.h(2, "1 · Onboarding: intake → resume → kickoff → agent-led turn")
    # =====================================================================

    resume_file = RUN_DIR / "resume.txt"
    resume_file.write_text(
        "Test Person — data engineer resume.\n"
        "Skills: Python, SQL, Airflow.\n"
        "Experience: built analytics pipelines at Example Co, 2020-present.\n",
        encoding="utf-8")

    # start on a fresh store: intake opens, resume needed.
    out = run(["agent", "start"])
    p = payload(out)
    t.check("start → phase intake", p["phase"] == "intake")
    t.check("start → needs.resume", p["needs"]["resume"] is True)

    # next: a pure read of the same phase.
    out = run(["agent", "next"])
    t.check("next → phase intake, no question",
            payload(out)["phase"] == "intake"
            and payload(out)["current_question"] is None)

    # resume intake: the document queues; needs.resume flips off.
    out = run(["agent", "resume", "--file", str(resume_file)])
    p = payload(out)
    t.check("resume → queued (pending)", p["resume"]["pending"] is True)
    t.check("resume → needs.resume off", p["needs"]["resume"] is False)
    t.check("resume block carries no text by default", "text" not in p["resume"])

    # start again: the kickoff turn runs → interview.
    out = run(["agent", "start"])
    p = payload(out)
    t.check("start → kickoff → interview", p["phase"] == "interview")
    t.check("start → mode choices", p["mode"] == "choices")

    # agent-led on: hand the agenda to free-form chat, send one message.
    out = run(["agent", "agent-led", "on"])
    t.check("agent-led on → mode agent", payload(out)["mode"] == "agent")

    # A document-informed chat turn: the fake hands a partial draft while
    # the pending resume context is still unspent — by design this feeds
    # the prefill bridge (the agenda keeps driving) and spends the resume
    # context; the reply's fenced block is redacted in the payload.
    os.environ["FAKE_CLAUDE_DRAFT"] = "1"
    out = run(["agent", "send", "--text",
               "Lead with my data engineering background, please."])
    os.environ.pop("FAKE_CLAUDE_DRAFT", None)
    p = payload(out)
    t.check("send → agent turn lands", p["phase"] == "interview")
    t.check("send → partial draft redacted in the payload",
            "fenced text redacted" in (p["last_agent_reply"] or ""),
            (p["last_agent_reply"] or "")[:60])
    t.check("redacted payload carries no resume text",
            "text" not in p["resume"] and "resume_facts" not in p)
    out = run(["agent", "next"])
    p = payload(out)
    t.check("prefill bridge: chips carry the extracted draft",
            len(p.get("prefill_chips", [])) > 0,
            f"{len(p.get('prefill_chips', []))} chips")

    # Negative probe: an answer to an unknown question must refuse in JSON.
    run_probe(["agent", "answer", "--question", "does-not-exist", "--choice", "x"],
              "unknown_question")

    # agent-led off: back to guided choices, agenda resumes.
    out = run(["agent", "agent-led", "off"])
    t.check("agent-led off → mode choices", payload(out)["mode"] == "choices")

    # =====================================================================
    t.h(2, "2 · Guided agenda: answer xN, payload-driven")
    # =====================================================================

    answered = 0
    for _ in range(len(onboarding.QUESTION_SPECS) + 1):
        q = payload(run(["agent", "next"], compact=True))["current_question"]
        if q is None:
            break
        if q["choices"]:
            argv = ["agent", "answer", "--question", q["id"],
                    "--choice", q["choices"][0]["value"]]
            how = f"choice={q['choices'][0]['value']!r}"
        elif q["allow_free_text"]:
            argv = ["agent", "answer", "--question", q["id"],
                    "--free-text", "Something reasonable"]
            how = "free-text"
        else:
            argv = ["agent", "answer", "--question", q["id"], "--skip"]
            how = "skip"
        t.note(f"- answering `{q['id']}` ({how})")
        run(argv, compact=True)
        answered += 1
    t.check("agenda drained to review", answered > 0, f"{answered} answers")

    # =====================================================================
    t.h(3, "3 · Review: fields, edits, keep-editing, back to review, confirm")
    # =====================================================================

    out = run(["agent", "next"])
    p = payload(out)
    t.check("next → phase review", p["phase"] == "review")
    t.check("next → needs.confirm", p["needs"]["confirm"] is True)
    t.check("review fields ride the payload", len(p.get("review_fields", [])) > 0)

    out = run(["agent", "next", "--reveal"])
    p = payload(out)
    t.check("--reveal surfaces resume_facts", "resume_facts" in p)

    out = run(["agent", "review", "--set", "phone=555-0199"])
    fields = {f["id"]: f for f in payload(out)["review_fields"]}
    t.check("review --set lands", fields.get("phone", {}).get("value") == "555-0199")

    out = run(["agent", "keep-editing"])
    t.check("keep-editing → back to interview", payload(out)["phase"] == "interview")

    # The agent hands the full draft in one free-form turn: the fenced block
    # is extracted, the stored reply is redacted, and the interview lands on
    # review — the payload-driven equivalent of the portal's draft flip.
    os.environ["FAKE_CLAUDE_PROFILE"] = "1"
    out = run(["agent", "send", "--text", "Lock it in with everything we discussed."])
    os.environ.pop("FAKE_CLAUDE_PROFILE", None)
    p = payload(out)
    t.check("draft hand-off → review", p["phase"] == "review")
    t.check("send → fenced draft redacted by default",
            "fenced text redacted" in (p["last_agent_reply"] or ""),
            (p["last_agent_reply"] or "")[:60])
    t.check("redacted draft carries no raw profile JSON",
            "_applypilot_profile" not in (p["last_agent_reply"] or ""))

    # The documented exit from the kept-draft interview state: hand the
    # agenda to the agent and back — the completed agenda lands on review.
    out = run(["agent", "keep-editing"])
    t.check("second keep-editing → interview", payload(out)["phase"] == "interview")
    run(["agent", "agent-led", "on"])
    out = run(["agent", "agent-led", "off"])
    t.check("agent-led round-trip → review", payload(out)["phase"] == "review")

    out = run(["agent", "confirm"])
    p = payload(out)
    t.check("confirm → phase done", p["phase"] == "done")
    t.check("profile.json written", (config.PROFILE_PATH).exists())
    t.check("searches.yaml written", (config.SEARCH_CONFIG_PATH).exists())
    t.check("resume.txt on disk", (config.RESUME_PATH).exists())

    # =====================================================================
    t.h(2, "4 · Pipeline: run discover enrich score, then run tailor cover pdf")
    # =====================================================================

    out = run(["run", "discover", "enrich", "score", "--min-score", "7"])
    conn = database.get_connection()
    rows = {r["url"]: dict(r) for r in conn.execute(
        "SELECT url, fit_score, full_description FROM jobs").fetchall()}
    t.check("both jobs scored", sorted(rows) == sorted(j["url"] for j in JOBS))
    for job in JOBS:
        row = rows.get(job["url"], {})
        t.check(f"score==9 for {job['url']}", row.get("fit_score") == 9,
                f"got {row.get('fit_score')}")
    t.check("stub provider saw 2 scoring calls",
            sum(1 for c in STUB.calls if c["kind"] == "score") == 2,
            f"provider calls so far: {json.dumps(STUB.calls)}")

    out = run(["run", "tailor", "cover", "pdf", "--min-score", "7"])
    tailored = sorted(p.name for p in config.TAILORED_DIR.glob("*"))
    letters = sorted(p.name for p in config.COVER_LETTER_DIR.glob("*"))
    t.check("tailored resumes written", len(tailored) >= 2, ", ".join(tailored))
    t.check("cover letters written", len(letters) >= 2, ", ".join(letters))
    pdfs = sorted(p.name for p in (config.TAILORED_DIR.rglob("*.pdf")))
    pdfs += sorted(p.name for p in (config.COVER_LETTER_DIR.rglob("*.pdf")))
    t.check("PDFs rendered", len(pdfs) >= 2, ", ".join(pdfs))
    for pdf in (list(config.TAILORED_DIR.rglob("*.pdf"))
                + list(config.COVER_LETTER_DIR.rglob("*.pdf"))):
        header = pdf.read_bytes()[:5]
        t.check(f"{pdf.name} is a real PDF", header == b"%PDF-", str(header))
    conn2 = database.get_connection()
    paths = {r["url"]: dict(r) for r in conn2.execute(
        "SELECT url, tailored_resume_path, cover_letter_path FROM jobs").fetchall()}
    for job in JOBS:
        row = paths.get(job["url"], {})
        t.check(f"queue row updated for {job['url']}",
                bool(row.get("tailored_resume_path")) and bool(row.get("cover_letter_path")))

    # =====================================================================
    t.h(2, "5 · Apply: plan → confirm → the pinned batch runs (RESULT lines)")
    # =====================================================================

    out = run(["apply", "--plan", "--limit", "2"])
    plan = payload(out)
    t.check("plan commits nothing", plan.get("commits_nothing") is True)
    t.check("plan batch == 2", plan.get("batch_size") == 2)
    t.check("plan cap == 5/day default",
            plan.get("cap") == planning.AGENT_DEFAULT_DAILY_CAP
            and plan.get("cap_source") == "default")
    t.check("plan hash staged",
            len(plan.get("plan_hash", "")) == 64
            and (config.APP_DIR / planning.STAGED_PLAN_FILE).exists())
    t.check("plan jobs == the tailored queue",
            [j["url"] for j in plan.get("jobs", [])] == sorted(j["url"] for j in JOBS))

    # The fake engine reads the job URL from the prompt it receives on
    # stdin and picks a deterministic verdict: alpha → APPLIED, beta →
    # CAPTCHA. The real launcher, Chrome worker, spawn, stream parse, and
    # RESULT scan all run unmodified.
    out = run(["apply", "--i-confirm", plan["plan_hash"], "--headless"])
    t.check("run events narrate APPLIED", "APPLIED (" in out)
    t.check("run events narrate CAPTCHA", "CAPTCHA (" in out)
    t.check("staged plan consumed", not (config.APP_DIR / planning.STAGED_PLAN_FILE).exists())

    # The RESULT: lines live in the per-job engine logs — the launcher writes
    # one transcript per engine run and the RESULT scan reads that text. The
    # CLI surface narrates the settled event (APPLIED/CAPTCHA), the log holds
    # the engine's own words; both are evidence, so both land here.
    job_logs = sorted(config.LOG_DIR.glob("claude_*.txt"))
    t.note(f"- engine run logs ({len(job_logs)}): " + ", ".join(p.name for p in job_logs))
    combined = "\n".join(p.read_text(encoding="utf-8") for p in job_logs)
    t.block(combined)
    t.check("engine narrates RESULT:APPLIED", "RESULT:APPLIED" in combined)
    t.check("engine narrates RESULT:CAPTCHA", "RESULT:CAPTCHA" in combined)

    conn3 = database.get_connection()
    rows = {r["url"]: dict(r) for r in conn3.execute(
        "SELECT url, apply_status, apply_error, apply_attempts, applied_at "
        "FROM jobs").fetchall()}
    t.check("alpha recorded applied",
            rows["https://jobs.example/alpha"]["applied_at"] is not None,
            json.dumps(rows["https://jobs.example/alpha"], default=str))
    t.check("beta terminal, not applied (CAPTCHA)",
            rows["https://jobs.example/beta"]["applied_at"] is None,
            json.dumps(rows["https://jobs.example/beta"], default=str))
    t.check("beta persists distinct captcha status, attempts pinned",
            rows["https://jobs.example/beta"]["apply_status"] == "captcha"
            and rows["https://jobs.example/beta"]["apply_error"] == "captcha"
            and rows["https://jobs.example/beta"]["apply_attempts"] == 99,
            json.dumps(rows["https://jobs.example/beta"], default=str))

    # status --json: the machine-readable scoreboard after the run.
    out = run(["status", "--json"])
    stats = json.loads(out)
    t.check("status --json counts 1 applied", stats.get("applied") == 1,
            f"applied={stats.get('applied')}, total={stats.get('total')}")
    t.check("status --json counts captcha separately from failures",
            stats.get("captcha") == 1 and stats.get("apply_errors") == 0,
            f"captcha={stats.get('captcha')}, "
            f"apply_errors={stats.get('apply_errors')}")

    # Replay probe: the one-shot token is burned — the confirm refuses.
    run_probe(["apply", "--i-confirm", plan["plan_hash"]], "plan_required")

    # =====================================================================
    t.h(2, "6 · Closing probes: redo wipes the interview, profile survives")
    # =====================================================================

    out = run(["agent", "redo"])
    p = payload(out)
    t.check("redo → fresh interview (observed semantics)", p["phase"] == "interview",
            f"phase={p['phase']}, answered={p['progress']['answered']}")
    t.check("redo resets the agenda", p["progress"]["answered"] == 0)
    t.check("profile.json survived redo", (config.PROFILE_PATH).exists())
    out = run(["agent", "next"])
    p = payload(out)
    t.check("next after redo → fresh guided interview",
            p["phase"] == "interview" and p["progress"]["answered"] == 0)

    # =====================================================================
    t.h(2, "7 · Provider call ledger")
    # =====================================================================
    kinds = {}
    for call in STUB.calls:
        kinds[call["kind"]] = kinds.get(call["kind"], 0) + 1
    t.note(f"- provider calls by kind: `{json.dumps(kinds)}`")
    t.note(f"- fake-claude turns logged: `{RUN_DIR / 'claude-prompts.log'}`")

    t.h(2, "Verdict")
    if t.failures:
        t.note(f"**FAILED checks: {len(t.failures)}**")
        for f in t.failures:
            t.note(f"- {f}")
    else:
        t.note("All checks passed — every protocol verb ran end-to-end; every error")
        t.note("in this transcript is an intentional, labeled probe of the error")
        t.note("envelope (unknown-question refusal, burned-token replay refusal).")

    server.shutdown()

    out_path = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv \
        else RUN_DIR / "transcript.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(t.text_out(), encoding="utf-8")
    print(f"transcript written: {out_path}")
    return 1 if t.failures else 0


def _git(args: list[str]) -> str:
    from subprocess import run as _run
    proc = _run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    return proc.stdout.strip() or "unknown"


def _git_head() -> str:
    return _git(["rev-parse", "HEAD"])


if __name__ == "__main__":
    sys.exit(main())
