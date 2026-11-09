# SPDX-License-Identifier: AGPL-3.0-only
"""Shared location filtering for discovery sources (B11).

Single canonical implementation of the location accept/reject filter used by
the JobSpy, Workday, and smart-extract paths. Semantics:

- Remote jobs are always accepted.
- Unknown/empty locations are always accepted (the scorer decides later).
- An empty or absent ``location_accept`` list means "accept all non-remote
  locations" — previously an empty list rejected every non-remote job, which
  silently filtered out everything for wizard-generated configs (the wizard
  does not write a ``location_accept`` key).
- A non-empty ``location_accept`` list behaves as a substring allowlist.
- ``location_reject_non_remote`` always rejects matching non-remote jobs.
"""

from __future__ import annotations

_REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh", "distributed")


def location_filters(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract (accept, reject) location patterns from a search config dict."""
    accept = search_cfg.get("location_accept") or []
    reject = search_cfg.get("location_reject_non_remote") or []
    return accept, reject


def location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Args:
        location: The job's location string (may be None/empty).
        accept: Accept patterns; empty means "accept all non-remote".
        reject: Reject patterns applied to non-remote jobs.

    Returns:
        True if the job location passes.
    """
    if not location:
        return True  # unknown location -- keep it, let scorer decide

    loc = location.lower()

    # Remote jobs always OK
    if any(r in loc for r in _REMOTE_MARKERS):
        return True

    # Reject non-remote matches
    for r in reject:
        if r.lower() in loc:
            return False

    # Empty accept list = no location restriction beyond reject rules
    if not accept:
        return True

    # Accept matches
    for a in accept:
        if a.lower() in loc:
            return True

    # No match -- reject unknown
    return False
