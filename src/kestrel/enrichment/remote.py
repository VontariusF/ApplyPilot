# SPDX-License-Identifier: AGPL-3.0-only
"""Remote-work inference heuristic (spec D; tightened by dogfood F6).

The is_remote flag is *derived* from the listing text we already hold when
no discovery source supplied one, and must always be presented as inferred
in the UI. The heuristic is deliberately conservative: it claims remote
only when the text says so — and, since F6, a bare "remote" mention is not
enough when the posting explicitly describes a hybrid / in-office schedule.
"""

import re

# Strong signals: any one of these asserts remote work outright. A
# remote-location format ("Remote", "Remote (US)", "Remote - Boston",
# "Remote Senior Engineer") also counts — "Remote" leading a text field is
# how boards write a remote workplace.
_STRONG_REMOTE_PATTERNS = (
    re.compile(r"\bfully\s+remote\b", re.IGNORECASE),
    re.compile(r"\b100\s*(?:%|percent)\s*remote\b", re.IGNORECASE),
    re.compile(r"\bwork\s+from\s+home\b", re.IGNORECASE),
    re.compile(r"\bwfh\b", re.IGNORECASE),
    re.compile(r"\btelecommut\w*\b", re.IGNORECASE),
    re.compile(r"\bwork\s+anywhere\b", re.IGNORECASE),
    re.compile(r"^\s*(?:remote|anywhere)\b", re.IGNORECASE),
)

# Weak signal: a bare "remote" mention. Word-bounded — "remotely close to
# downtown" and "hypothermia" match nothing. It infers remote only when no
# hybrid / in-office-days signal contradicts it.
_BARE_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)

# Hybrid / in-office-days signals (dogfood F6: a Kaizen posting reading
# "hybrid, 4 days/week in office" also mentioned "remote" and was mislabeled).
_HYBRID_PATTERNS = (
    re.compile(r"\bhybrid\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+days?\s*(?:/\s*|per\s+)?week\b.{0,60}\b(?:in[\s-]?office|on[\s-]?site|onsite)\b", re.IGNORECASE),
    re.compile(r"\b(?:in[\s-]?office|on[\s-]?site|onsite)\b.{0,60}\b\d+\s+days?\b", re.IGNORECASE),
)


def infer_remote(*texts: str | None) -> bool:
    """True when the texts assert remote work.

    Args:
        texts: Listing text fields (title, location, description...).
            None and empty strings are skipped.

    Returns:
        True only on an explicit remote assertion:

        - a strong signal ("fully remote", "100% remote", "work from
          home", WFH/telecommute, a Remote-first location/title format)
          always infers remote — even alongside a hybrid mention;
        - otherwise a bare "remote" mention infers remote only when no
          hybrid / in-office-days signal contradicts it, so a hybrid
          posting that idly mentions "remote" stays non-remote.

    This is an inference, not site data; callers must label it inferred.
    """
    present = [text for text in texts if text]
    if any(pattern.search(text) for text in present for pattern in _STRONG_REMOTE_PATTERNS):
        return True
    if not any(_BARE_REMOTE.search(text) for text in present):
        return False
    return not any(pattern.search(text) for text in present for pattern in _HYBRID_PATTERNS)
