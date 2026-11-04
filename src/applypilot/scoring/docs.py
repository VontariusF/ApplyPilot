"""Shared helpers for generated output documents (tailored resumes, cover letters)."""

import hashlib
import re


def output_prefix(job: dict) -> str:
    """Build a collision-free filename prefix for a job's documents (B3).

    ``site + title`` alone collides across jobs at the same company with
    similar titles, silently overwriting the other job's resume, report and
    PDF. A URL-derived hash suffix makes every job's output files unique
    while keeping the human-readable prefix.
    """
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "untitled")[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job.get("site") or "unknown")[:20].strip().replace(" ", "_")
    url_hash = hashlib.sha1((job.get("url") or "").encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{url_hash}"


def versioned_stem(prefix: str, version: int) -> str:
    """Filename stem for a versioned document (spec D2): ``{prefix}_v{N}``.

    History-on writes use this stem for the document and its sidecars
    (_JOB.txt, _REPORT.json, _CL.txt); the legacy deterministic ``{prefix}``
    file is left untouched. History-off writes keep the bare prefix.
    """
    return f"{prefix}_v{version}"
