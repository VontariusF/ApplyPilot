"""Remote-work inference heuristic (spec D).

The store has no authoritative remote field from any discovery site, so
the is_remote flag is *derived* from the listing text we already hold and
must always be presented as inferred in the UI. The heuristic is
deliberately conservative: it claims remote only when the text says so.
"""

import re

# Word-bounded markers; "Remote (US)", "work from home", "WFH-friendly",
# "telecommuting OK" all match. "hybrid" and bare city names match nothing.
_REMOTE_PATTERNS = (
    re.compile(r"\bremote\b", re.IGNORECASE),
    re.compile(r"\bwork\s+from\s+home\b", re.IGNORECASE),
    re.compile(r"\bwfh\b", re.IGNORECASE),
    re.compile(r"\btelecommut\w*\b", re.IGNORECASE),
    re.compile(r"\bwork\s+anywhere\b", re.IGNORECASE),
)


def infer_remote(*texts: str | None) -> bool:
    """True when any of the texts asserts remote work.

    Args:
        texts: Listing text fields (title, location, description...).
            None and empty strings are skipped.

    Returns:
        True only on an explicit remote marker — a "hybrid" posting or a
        bare location never counts. This is an inference, not site data;
        callers must label it inferred.
    """
    for text in texts:
        if not text:
            continue
        if any(pattern.search(text) for pattern in _REMOTE_PATTERNS):
            return True
    return False
