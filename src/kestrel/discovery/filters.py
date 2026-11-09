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
    """Extract (accept, reject) location patterns from a search config dict.

    Three config shapes exist in the wild (dogfood 2026-11-09: a wizard-
    generated config silently disabled location filtering because only the
    flat keys were read, storing ~3,350 out-of-area jobs):

    - flat ``location_accept`` / ``location_reject_non_remote`` lists (the
      canonical keys — they win whenever present, even explicitly empty);
    - nested ``location: {accept_patterns, reject_patterns}`` (the shape the
      packaged searches.example.yaml writes — previously never read);
    - no pattern keys at all → patterns are derived from the wizard shape
      (``locations:`` entries, falling back to ``defaults.location``).
    """
    accept = search_cfg.get("location_accept")
    reject = search_cfg.get("location_reject_non_remote")

    nested = search_cfg.get("location")
    if isinstance(nested, dict):
        if accept is None:
            accept = nested.get("accept_patterns")
        if reject is None:
            reject = nested.get("reject_patterns")

    if accept is None:
        accept = accept_patterns_from_locations(search_cfg)

    return list(accept or []), list(reject or [])


def accept_patterns_from_locations(search_cfg: dict) -> list[str]:
    """Derive location accept patterns from the wizard config shape.

    Onboarding writes ``locations:`` entries (and a ``defaults.location``
    fallback) but no accept patterns: board sources pass the location
    string to the provider, but employer-site sources (Workday career
    portals) have no server-side location parameter and must filter
    locally. Derived patterns cover the full string ("New York, NY")
    and each comma part ("New York", "NY") so postings written
    "New York, NY, USA" or "Brooklyn, NY" still match the substring
    filter. Remote-marker entries contribute nothing — remote jobs
    always pass. Empty result = "accept all non-remote" (unchanged B11
    semantics for configs that name no location).
    """
    patterns: list[str] = []
    seen: set[str] = set()

    def _add(value) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text.lower() in _REMOTE_MARKERS:
            return
        if text.lower() not in seen:
            seen.add(text.lower())
            patterns.append(text)

    for entry in search_cfg.get("locations") or []:
        loc = entry.get("location") if isinstance(entry, dict) else entry
        _add(loc)
        if isinstance(loc, str):
            for part in loc.split(","):
                _add(part)

    if not patterns:
        default = (search_cfg.get("defaults") or {}).get("location")
        _add(default)
        if isinstance(default, str):
            for part in default.split(","):
                _add(part)

    return patterns


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
