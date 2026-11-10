# SPDX-License-Identifier: AGPL-3.0-only
"""Shared location filtering for discovery sources (B11).

Single canonical implementation of the location accept/reject filter used by
the JobSpy, Workday, and smart-extract paths. Semantics:

- Remote-marker tags are stripped and the geographic remainder is filtered
  ("France, Remote" filters on "France"); a location that is only remote
  markers (plain "Remote") is always accepted.
- When a location accept list is configured, blank locations are derived
  from the job URL first (best-effort URL parsing, NOT geocoding — see
  ``derive_location_from_url``); jobs still blank after derivation are
  rejected. Without an accept list, blank locations still pass (the scorer
  decides later).
- An empty or absent ``location_accept`` list means "accept all non-remote
  locations" — previously an empty list rejected every non-remote job, which
  silently filtered out everything for wizard-generated configs (the wizard
  does not write a ``location_accept`` key).
- A non-empty ``location_accept`` list behaves as a lexical allowlist that
  can never match inside a word (see ``_matches_pattern``).
- ``location_reject_non_remote`` always rejects matching non-remote jobs.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

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


def _matches_pattern(pattern: str, geo: str) -> bool:
    """Lexically match one accept/reject pattern, never inside a word (F1).

    Substring matching leaked inside words: the derived pattern "NY" matched
    "Munich, Germany" because "germany" contains "ny". A pattern now must
    start at a token boundary — an interior match never fires — while the
    documented lexical looseness stays: a match at a word start may extend
    ("NY" still matches "Remote — NYC", "US" still matches "US-based"), and
    a standalone "NY" token still matches "Buffalo, NY".
    """
    needle = pattern.strip().lower()
    if not needle:
        return True  # an empty pattern matched everything under substring rules
    return re.search(rf"(?<!\w){re.escape(needle)}", geo.lower()) is not None


def _geo_remainder(location: str) -> str:
    """Return the geographic part of a location string, remote markers removed.

    "France, Remote" -> "france"; "Remote" -> ""; "Remote - Boston" ->
    "boston". Non-remote locations pass through lowercased and otherwise
    untouched. Token matching downstream means a leftover fragment still
    matches accept/reject patterns ("US (Remote)" -> "us").
    """
    text = location.lower()
    for marker in sorted(_REMOTE_MARKERS, key=len, reverse=True):
        text = text.replace(marker, " ")
    parts = []
    for chunk in text.split(","):
        cleaned = chunk.strip(" \t;/|.-–—()'\"[]")
        if cleaned:
            parts.append(cleaned)
    return ", ".join(parts)


# Two-letter US state codes that are also common English words ("in", "or",
# "me", "de", "co", "ok", "hi", "id") are excluded from slug matching: the
# full state name still matches ("austin-oregon"), and a URL token like
# "sign-in" or "opt-in" is far more likely to be a word than a state.
_STATE_CODES = {
    "al": "AL",
    "ak": "AK",
    "az": "AZ",
    "ar": "AR",
    "ca": "CA",
    "ct": "CT",
    "fl": "FL",
    "ga": "GA",
    "il": "IL",
    "ia": "IA",
    "ks": "KS",
    "ky": "KY",
    "la": "LA",
    "ma": "MA",
    "md": "MD",
    "mi": "MI",
    "mn": "MN",
    "ms": "MS",
    "mo": "MO",
    "mt": "MT",
    "ne": "NE",
    "nv": "NV",
    "nh": "NH",
    "nj": "NJ",
    "nm": "NM",
    "ny": "NY",
    "nc": "NC",
    "nd": "ND",
    "oh": "OH",
    "pa": "PA",
    "ri": "RI",
    "sc": "SC",
    "sd": "SD",
    "tn": "TN",
    "tx": "TX",
    "ut": "UT",
    "vt": "VT",
    "va": "VA",
    "wa": "WA",
    "wv": "WV",
    "wi": "WI",
    "wy": "WY",
    "dc": "DC",
}
_STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}
_COUNTRIES = {
    "united states",
    "united states of america",
    "usa",
    "us",
    "canada",
    "united kingdom",
    "uk",
    "germany",
    "france",
    "netherlands",
    "spain",
    "italy",
    "ireland",
    "poland",
    "sweden",
    "switzerland",
    "austria",
    "belgium",
    "denmark",
    "norway",
    "finland",
    "portugal",
    "india",
    "australia",
    "japan",
    "singapore",
    "israel",
    "brazil",
    "mexico",
    "argentina",
    "south africa",
    "new zealand",
}
# Locale-ish path segments ("en", "en-us", "en_us") are not locations.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$")


def _location_from_slug_tokens(tokens: list[str]) -> str | None:
    """Parse a slug token list like ["norwood", "ma"] into a location string.

    Recognized shapes, checked against the END of the token list (job-title
    words, when a slug mixes title and location, are tolerated as city
    tokens — the filter only needs token matching):

    - any token "remote" (or the whole slug is a remote marker) -> "Remote";
    - a US state code or state name as the last or second-to-last token
      ("norwood-ma", "austin-tx-usa", "new-york-ny") -> "City, ST";
    - a country name as the last or last-two tokens ("munich-germany",
      "toronto-canada", plain "united-states") -> "City, Country".

    Returns None when nothing recognizable is present.
    """
    if not tokens:
        return None
    joined = " ".join(tokens)
    if joined in _REMOTE_MARKERS or "remote" in tokens:
        return "Remote"
    alpha = [t for t in tokens if t.isalpha() and len(t) >= 2]

    def _city(head: list[str]) -> str:
        return " ".join(head).title() if head else ""

    # Two-token tails first ("new york", "united states"), then single tokens.
    for n in (2, 1):
        if len(alpha) <= n:
            continue
        tail = " ".join(alpha[-n:])
        head = alpha[:-n]
        state = _STATE_CODES.get(tail) or _STATE_NAMES.get(tail)
        if state:
            city = _city(head)
            if city:
                return f"{city}, {state}"
        if tail in _COUNTRIES:
            city = _city(head)
            return f"{city}, {tail.title()}" if city else tail.title()

    # Single-token slug that IS a state/country ("ma", "germany").
    if len(alpha) == 1:
        state = _STATE_CODES.get(alpha[0]) or _STATE_NAMES.get(alpha[0])
        if state:
            return state
        if alpha[0] in _COUNTRIES:
            return alpha[0].title()
    return None


def _slug_location_candidates(segment: str) -> list[str]:
    """Candidate location substrings of one URL path segment.

    Workday encodes a location hierarchy with underscores
    ("Norwood-MA_United-States"); the part before the first underscore is
    the most specific level, so it is tried first, then the whole segment.
    """
    if "_" in segment:
        first, _, _rest = segment.partition("_")
        return [first, segment]
    return [segment]


def derive_location_from_url(url: str | None) -> str | None:
    """Best-effort location extraction from a job posting URL.

    THIS IS URL PATTERN PARSING, NOT GEOCODING. It runs three local
    heuristics, in order, and returns the first hit (or None):

    1. Explicit query parameters many boards carry: ``location``, ``city``,
       ``loc``, ``state`` (returned verbatim).
    2. Workday-style paths: the segment after ``/job``/``/jobs``, split on
       the underscore hierarchy (".../job/Norwood-MA_United-States/R123"
       -> "Norwood, MA").
    3. Generic slug tokens anywhere in the path, rightmost segment first:
       US state codes/names and a small country-name list used as the
       terminal token (".../careers/austin-texas" -> "... Austin, TX").

    Known limits, accepted by design (dogfood 2026-11-10): boards with
    opaque URLs (Greenhouse/Ashby numeric ids, hashed Indeed/Glassdoor
    links) yield nothing; a slug mixing job title and location may keep
    title words in the "city" part ("engineer-austin-texas" -> "Engineer
    Austin, TX") — harmless for the substring accept filter; two-letter
    state codes that are common English words ("in", "or", "me", "de",
    "co", "ok", "hi", "id") are not matched; and nothing here verifies
    that a recognized token is actually where the job sits. None means
    "no location evidence", which callers treat as still-blank.
    """
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    query = parse_qs(parts.query)
    for key in ("location", "city", "loc", "state"):
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()

    segments = [seg for seg in parts.path.split("/") if seg and not _LOCALE_SEGMENT.match(seg.lower())]

    # Workday-style: the segment right after job/jobs encodes the location.
    for idx, seg in enumerate(segments):
        if seg.lower() in ("job", "jobs") and idx + 1 < len(segments):
            for candidate in _slug_location_candidates(segments[idx + 1]):
                parsed = _location_from_slug_tokens([t for t in re.split(r"[-_\s]+", candidate.lower()) if t])
                if parsed:
                    return parsed

    # Generic slugs: rightmost segment that parses as a location.
    for seg in reversed(segments):
        for candidate in _slug_location_candidates(seg):
            parsed = _location_from_slug_tokens([t for t in re.split(r"[-_\s]+", candidate.lower()) if t])
            if parsed:
                return parsed
    return None


def location_ok(
    location: str | None,
    accept: list[str],
    reject: list[str],
    url: str | None = None,
) -> bool:
    """Check if a job location passes the user's location filter.

    Args:
        location: The job's location string (may be None/empty).
        accept: Accept patterns; empty means "accept all non-remote".
        reject: Reject patterns applied to the geographic part of the
            location string.
        url: The job's URL, used to derive a location when the location
            string is blank (see ``derive_location_from_url``; best-effort
            URL parsing, not geocoding).

    Returns:
        True if the job location passes.

    Semantics (updated after the 2026-11-10 dogfood run, in which 253/635
    stored jobs had blank locations and "France, Remote" style strings
    bypassed the accept list via the old remote-marker early return):

    - A blank location is first filled from the URL. If it is still blank:
      rejected when an accept list is configured (no evidence to match
      against), accepted otherwise (the scorer decides, as before).
    - Remote markers are stripped from what remains; a location that is
      only remote markers ("Remote", "Work From Home") always passes.
    - The geographic remainder is checked against reject patterns, then the
      accept list (empty accept = no restriction beyond reject rules).
      Non-remote locations are their own remainder, so their behavior is
      unchanged from the pre-derivation filter.
    """
    loc = location.strip() if location and location.strip() else derive_location_from_url(url)
    if not loc:
        # Blank and no URL evidence. With an accept list configured the old
        # "let the scorer decide" default flooded the queue, so reject;
        # the accept-all baseline is unchanged.
        return not accept

    geo = _geo_remainder(loc)
    if not geo:
        return True  # pure remote -- nothing geographic to filter

    # Reject non-remote matches (and the geo remainder of remote-tagged ones)
    for r in reject:
        if _matches_pattern(r, geo):
            return False

    # Empty accept list = no location restriction beyond reject rules
    if not accept:
        return True

    # Accept matches
    for a in accept:
        if _matches_pattern(a, geo):
            return True

    # No match -- reject unknown
    return False
