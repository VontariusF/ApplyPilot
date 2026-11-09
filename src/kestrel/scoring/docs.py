# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for generated output documents (tailored resumes, cover letters)."""

import hashlib
import re
import unicodedata

# One or more separator characters (S3): a run of two or more collapses to
# a single underscore, so removed glyphs (em-dashes) and stripped tokens
# cannot leave '__' or ' _' sequences behind.
_SEPARATOR_RUN = re.compile(r"[\s_-]{2,}")

# F3: anything non-ASCII that survives NFKD decomposition (CJK, Cyrillic,
# ø) has no ASCII spelling to fall back to — reduce it to an underscore
# so generated filenames are ASCII-only. Runs of these collapse via
# _SEPARATOR_RUN below.
_ASCII_FALLBACK = re.compile(r"[^A-Za-z0-9_-]")


def _safe_component(value: str | None, limit: int) -> str:
    """Filename-safe, idempotent ASCII slug for one prefix component (S3).

    Non-ASCII look-alikes are normalized to their ASCII spelling
    (é → e via NFKD decomposition plus combining-mark removal) before the
    character filter; anything non-ASCII that survives decomposition
    (CJK, Cyrillic, ø) becomes an underscore, so the output is
    ASCII-only. Runs of two or more separators collapse to one
    underscore. Applying this to its own output is a no-op.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s-]", "", text)
    text = _ASCII_FALLBACK.sub("_", text)
    text = _SEPARATOR_RUN.sub("_", text.strip()).replace(" ", "_")
    text = text.strip("_ ")
    return text[:limit] or "none"


def output_prefix(job: dict) -> str:
    """Build a collision-free filename prefix for a job's documents (B3).

    ``site + title`` alone collides across jobs at the same company with
    similar titles, silently overwriting the other job's resume, report and
    PDF. A URL-derived hash suffix makes every job's output files unique
    while keeping the human-readable prefix.
    """
    safe_title = _safe_component(job.get("title") or "untitled", 50)
    safe_site = _safe_component(job.get("site") or "unknown", 20)
    url_hash = hashlib.sha1((job.get("url") or "").encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{url_hash}"


def versioned_stem(prefix: str, version: int) -> str:
    """Filename stem for a versioned document (spec D2): ``{prefix}_v{N}``.

    History-on writes use this stem for the document and its sidecars
    (_JOB.txt, _REPORT.json, _CL.txt); the legacy deterministic ``{prefix}``
    file is left untouched. History-off writes keep the bare prefix.
    """
    return f"{prefix}_v{version}"
