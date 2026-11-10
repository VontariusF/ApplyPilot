# SPDX-License-Identifier: AGPL-3.0-only
"""PDF defaults to HTML (spec art_VGFSMgRh, piece 3).

Kestrel writes print-ready styled HTML and never renders a PDF itself:
the host agent prints the HTML with its own browser when a PDF is wanted.
These tests pin the HTML output contract (styling present, honest result
keys), the typed PdfRenderSteer for any path still asking for a PDF, and
the absence of any Playwright/Chromium import in the materials/pdf path.
"""

from pathlib import Path

import pytest

from kestrel import database
from kestrel.scoring import pdf as pdf_mod

RESUME_TEXT = "\n".join([
    "Jane Doe",
    "Backend Engineer",
    "Austin, TX",
    "jane@example.com | 555-0100",
    "SUMMARY",
    "Ships reliable systems.",
    "EXPERIENCE",
    "Senior Engineer at Acme",
    "Acme Corp, 2020-Present",
    "- Built the thing",
    "- Scaled the thing",
    "TECHNICAL SKILLS",
    "Languages: Python, Go",
    "PROJECTS",
    "Kestrel",
    "A job hunter",
    "- Wrote tests",
    "EDUCATION",
    "BS Computer Science, UT Austin",
])

LETTER_TEXT = "\n\n".join([
    "Dear Hiring Manager,",
    "I built the ingestion pipeline that doubled throughput.\nIt would transfer directly to this role.",
    "I would welcome the chance to talk.",
    "Sincerely,\nJane Doe",
])


# ---------------------------------------------------------------------------
# convert_to_pdf — HTML is the default and only written output
# ---------------------------------------------------------------------------


class TestConvertsToHtml:
    def test_default_output_is_html(self, tmp_path):
        txt = tmp_path / "resume_acme.txt"
        txt.write_text(RESUME_TEXT, encoding="utf-8")

        out = pdf_mod.convert_to_pdf(txt)

        assert out == tmp_path / "resume_acme.html"
        assert out.exists()
        assert not (tmp_path / "resume_acme.pdf").exists()

    def test_resume_html_carries_title_header_and_sections(self, tmp_path):
        txt = tmp_path / "resume_acme.txt"
        txt.write_text(RESUME_TEXT, encoding="utf-8")

        out = pdf_mod.convert_to_pdf(txt)
        html = out.read_text(encoding="utf-8")

        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert "<style>" in html, "the output must be self-styled"
        assert "Jane Doe" in html, "name header block present"
        assert "Backend Engineer" in html, "title present"
        assert "Experience" in html, "body section present"
        assert "Technical Skills" in html, "body section present"

    def test_cover_letter_html_carries_letter_styling(self, tmp_path):
        txt = tmp_path / "resume_acme_CL.txt"
        txt.write_text(LETTER_TEXT, encoding="utf-8")

        out = pdf_mod.convert_to_pdf(txt)
        html = out.read_text(encoding="utf-8")

        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert "Cover Letter" in html, "title present"
        assert "Jane Doe" in html, "sign-off name header block present"
        assert "doubled throughput" in html, "body paragraphs present"

    def test_explicit_kind_overrides_filename_inference(self, tmp_path):
        txt = tmp_path / "letter_CL.txt"
        txt.write_text(RESUME_TEXT, encoding="utf-8")  # resume text at a _CL stem

        out = pdf_mod.convert_to_pdf(txt, kind="resume")

        assert "Technical Skills" in out.read_text(encoding="utf-8")

    def test_html_only_false_raises_the_typed_steer(self, tmp_path):
        txt = tmp_path / "resume_acme.txt"
        txt.write_text(RESUME_TEXT, encoding="utf-8")

        with pytest.raises(pdf_mod.PdfRenderSteer) as excinfo:
            pdf_mod.convert_to_pdf(txt, html_only=False)

        steer = str(excinfo.value)
        assert "host agent" in steer, "the steer must point at the host route"
        assert str(excinfo.value.html_path).endswith(".html")
        assert not (tmp_path / "resume_acme.pdf").exists(), "nothing renders"


# ---------------------------------------------------------------------------
# persist_tailored / persist_cover_letter — honest result keys and .html files
# ---------------------------------------------------------------------------


def _seed_job(conn, url, **extra):
    cols = {
        "url": url, "title": "Engineer", "site": "acme",
        "full_description": "d" * 300, "fit_score": 9,
        "discovered_at": "2026-01-01T00:00:00+00:00",
    }
    cols.update(extra)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()


@pytest.fixture()
def scoring_env(app_dir, isolated_db, monkeypatch):
    """Isolated app dir + DB, with the scoring modules' paths repointed."""
    from kestrel.scoring import cover_letter, tailor

    monkeypatch.setattr(tailor, "TAILORED_DIR", app_dir / "tailored")
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", app_dir / "cover_letters")
    monkeypatch.setattr(tailor, "load_profile",
                        lambda: {"personal": {"full_name": "Test"}})
    monkeypatch.setattr(cover_letter, "load_profile",
                        lambda: {"personal": {"full_name": "Test"}})
    database.init_db()
    return database.get_connection()


class TestPersistWriters:
    def test_persist_tailored_writes_styled_html(self, scoring_env, monkeypatch):
        from kestrel.scoring.tailor import persist_tailored

        url = "https://example.com/job-1"
        _seed_job(scoring_env, url)
        report = {"attempts": 1, "status": "approved", "validator": {"passed": True}}

        result = persist_tailored(scoring_env, dict(scoring_env.execute(
            "SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()),
            RESUME_TEXT, report)

        assert result["html_path"] is not None
        assert result["html_path"].endswith(".html")
        html = Path(result["html_path"]).read_text(encoding="utf-8")
        assert "Jane Doe" in html and "<style>" in html
        assert not result["html_path"].endswith(".pdf")

    def test_persist_cover_letter_writes_styled_html(self, scoring_env):
        from kestrel.scoring.cover_letter import persist_cover_letter

        url = "https://example.com/job-2"
        _seed_job(scoring_env, url)

        result, _validation = persist_cover_letter(
            scoring_env,
            dict(scoring_env.execute(
                "SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()),
            LETTER_TEXT)

        assert result["html_path"] is not None
        assert result["html_path"].endswith(".html")
        html = Path(result["html_path"]).read_text(encoding="utf-8")
        assert "Cover Letter" in html
        assert not result["html_path"].endswith(".pdf")


# ---------------------------------------------------------------------------
# batch_convert — the pdf stage produces styled HTML for pending rows
# ---------------------------------------------------------------------------


class TestBatchConvertHtml:
    def test_batch_convert_writes_html_for_pending_rows(self, isolated_db, tmp_path, monkeypatch):
        tailored = tmp_path / "tailored"
        tailored.mkdir()
        txt = tailored / "resume_acme.txt"
        txt.write_text(RESUME_TEXT, encoding="utf-8")
        database.init_db()
        conn = database.get_connection()
        conn.execute(
            "INSERT INTO jobs (url, tailored_resume_path) VALUES (?, ?)",
            ("https://example.com/job-1", str(txt)),
        )
        conn.commit()
        monkeypatch.setattr(pdf_mod, "TAILORED_DIR", tailored)

        converted = pdf_mod.batch_convert()

        assert converted == 1
        html = txt.with_suffix(".html")
        assert html.exists(), "batch_convert must write the styled HTML"
        assert not txt.with_suffix(".pdf").exists(), "no PDF is ever rendered"
        assert "Jane Doe" in html.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Regression pin: no browser anywhere in the materials/pdf path
# ---------------------------------------------------------------------------


class TestNoBrowserInTheMaterialsPath:
    def test_pdf_module_has_no_playwright_or_chromium_references(self):
        source = Path(pdf_mod.__file__).read_text(encoding="utf-8")

        lowered = source.lower()
        assert "playwright" not in lowered, (
            "the Chromium render path is deleted (spec art_VGFSMgRh)"
        )
        assert "chromium" not in lowered
        assert "kestrel.browser" not in lowered

    def test_render_pdf_is_gone(self):
        assert not hasattr(pdf_mod, "render_pdf")

    def test_pdf_module_imports_with_base_deps_only(self):
        # With the render path deleted, importing the module must not pull
        # anything beyond the base dependency set — the [browser] extra is
        # irrelevant to this area. Runs in a subprocess so a leftover
        # playwright module from another test cannot mask a real import.
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import kestrel.scoring.pdf; "
             "left = [m for m in sys.modules if m.startswith('playwright')]; "
             "assert not left, left"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    pytest.main([__file__])
