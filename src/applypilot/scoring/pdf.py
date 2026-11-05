"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = sections["EDUCATION"].strip()
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.35in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def _record_pdf_failure(text_path: Path) -> None:
    """Burn one pdf attempt on the job row whose tailored resume failed.

    Keeps retry semantics bounded: after _MAX pdf_attempts the row leaves
    the pending count, the same cap the tailor and cover stages apply.
    """
    from applypilot.database import get_connection

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET pdf_attempts = COALESCE(pdf_attempts, 0) + 1 "
        "WHERE tailored_resume_path = ?",
        (str(text_path),),
    )
    conn.commit()


def _settle_job_pdf_states(convert_budget: int | None = None) -> int:
    """Reconcile the jobs table's PDF state with the filesystem.

    The streaming pipeline counts pending pdf work from the jobs table
    (pipeline._PENDING_SQL), so batch_convert must be the writer that
    settles that state — otherwise a row whose .txt never changes keeps
    the count above zero and the pdf stage busy-loops. Three settle paths:

      - a matching PDF already exists on disk (including one this pass
        just generated): mark pdf_generated_at — the work is done;
      - the source .txt exists but has no PDF (e.g. outside the
        TAILORED_DIR scan): convert it here, so the row settles
        wherever the file lives;
      - the source .txt has vanished: burn one pdf attempt so the row
        ages out of the pending count under the same attempt cap the
        tailor and cover stages use.

    Args:
        convert_budget: Maximum number of renders this reconciliation may
            attempt (None = unbounded). Bookkeeping-only updates — marking
            an existing PDF generated, burning an attempt for a vanished
            source — always run; only renders consume budget. Rows beyond
            the budget simply stay pending for the next pass.

    Returns:
        Number of rows settled as generated this pass.
    """
    from applypilot.database import _utc_now, get_connection

    conn = get_connection()
    rows = conn.execute(
        "SELECT url, tailored_resume_path FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND tailored_resume_path LIKE '%.txt' "
        "AND pdf_generated_at IS NULL "
        # Cap parity with pipeline._PENDING_SQL: an exhausted row is not
        # pending, so reconciliation must not re-render it either (it
        # would re-invoke Chromium for a known poison input forever).
        "AND COALESCE(pdf_attempts, 0) < 5"
    ).fetchall()

    # Pass 1 — classify rows and do all filesystem work BEFORE any DB
    # write: an open SQLite write transaction held across a Chromium
    # render would block other stages' writes for the render's duration.
    generated: list[tuple[str, Path]] = []
    failed: list[tuple[str, Path]] = []
    conversions: list[tuple[str, Path]] = []
    for row in rows:
        txt = Path(row["tailored_resume_path"])
        pdf = txt.with_suffix(".pdf")
        if pdf.exists():
            generated.append((row["url"], txt))
        elif txt.exists():
            if convert_budget is not None and len(conversions) >= convert_budget:
                continue  # over quota — stays pending for the next pass
            conversions.append((row["url"], txt))
        else:
            failed.append((row["url"], txt))  # source vanished — burn an attempt

    for url, txt in conversions:
        try:
            convert_to_pdf(txt)
        except Exception as e:
            log.error("PDF conversion failed for %s: %s", txt.name, e)
            failed.append((url, txt))
        else:
            generated.append((url, txt))

    # Pass 2 — one short write transaction with no renders inside it.
    # Both updates are predicated on the row still pointing at the exact
    # path this pass inspected: a concurrent reset + re-tailor that swaps
    # the row to a new path must not be settled (or attempt-burned) by an
    # obsolete render result for the old path.
    now = _utc_now()
    settled = 0
    for url, txt in generated:
        # Re-verify at write time: with history off a concurrent
        # reset_job_tailoring deletes the stale .pdf — invalidating this
        # mark keeps the row pending so the regenerated text converts.
        if not txt.with_suffix(".pdf").exists():
            continue
        cur = conn.execute(
            "UPDATE jobs SET pdf_generated_at = ? "
            "WHERE url = ? AND tailored_resume_path = ?",
            (now, url, str(txt)),
        )
        settled += cur.rowcount
    for url, txt in failed:
        conn.execute(
            "UPDATE jobs SET pdf_attempts = COALESCE(pdf_attempts, 0) + 1 "
            "WHERE url = ? AND tailored_resume_path = ?",
            (url, str(txt)),
        )
    conn.commit()
    return settled


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Afterwards, reconciles the jobs table's PDF bookkeeping with the
    filesystem (see _settle_job_pdf_states) so the streaming pipeline's
    pending count converges instead of re-counting unchanged .txt paths
    forever.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        # No directory does not mean no pending rows — sources may have
        # been deleted since the last run. Settle the jobs table anyway.
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        _settle_job_pdf_states()
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Skip files whose job row has exhausted its pdf attempts (cap parity
    # with pipeline._PENDING_SQL): the pipeline no longer counts them as
    # pending, so re-invoking Chromium for a known poison input on every
    # call would defeat the bounded-retry semantics.
    from applypilot.database import get_connection

    conn = get_connection()
    exhausted = {
        row["tailored_resume_path"]
        for row in conn.execute(
            "SELECT tailored_resume_path FROM jobs "
            "WHERE COALESCE(pdf_attempts, 0) >= 5 "
            "AND tailored_resume_path IS NOT NULL"
        ).fetchall()
    }

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        if str(f) in exhausted:
            continue
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    converted = 0
    if to_convert:
        log.info("Converting %d files to PDF...", len(to_convert))
        for f in to_convert:
            try:
                convert_to_pdf(f)
                converted += 1
            except Exception as e:
                log.error("Failed to convert %s: %s", f.name, e)
                _record_pdf_failure(f)
        log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    else:
        log.info("All text files already have PDFs.")

    # Settle the jobs table's PDF bookkeeping so the streaming pipeline's
    # pending count converges (rows whose PDF exists or whose source is
    # gone must not pend forever). Runs on every exit path — including
    # when nothing needed converting, which is exactly the stale-
    # accounting state that used to busy-loop the pdf stage. The
    # reconciliation's render quota is whatever is left of this call's
    # limit, so batch_convert never renders more than `limit` PDFs total.
    settled = _settle_job_pdf_states(convert_budget=limit - converted)
    if settled:
        log.info("Settled %d job(s) with PDFs already on disk.", settled)
    return converted
