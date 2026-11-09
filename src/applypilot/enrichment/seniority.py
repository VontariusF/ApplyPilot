"""Seniority classification: title/description text → a seniority tier.

Deterministic and table-driven — no LLM in the loop (locked decision,
spec art_we4AUxEy). The rule list is ordered; the FIRST rule whose pattern
matches wins, so more specific tiers (`intern`) outrank generic ones
(`senior`) regardless of position in the text. Description text is only
consulted when the title yields nothing, and then only for weak signals
(`mid` via an explicit years-of-experience hint) — a description mention
of "lead" in "you will lead a team" must not manufacture a tier.
"""

import re

# Ordered (pattern, tier) rules. First match on the title wins; the same
# list runs over the description only as a last resort before the
# experience-hint fallback.
SENIORITY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bintern(ship)?\b|\bapprentice\b", re.IGNORECASE), "intern"),
    (re.compile(r"\bjunior\b|\bentry[-\s]?level\b|\bgraduate\b", re.IGNORECASE), "junior"),
    (re.compile(r"\bprincipal\b", re.IGNORECASE), "principal"),
    (re.compile(r"\bstaff\b|\barchitect\b", re.IGNORECASE), "staff"),
    (re.compile(r"\blead\b", re.IGNORECASE), "lead"),
    (re.compile(r"\bmanager\b|\bhead\b|\bdirector\b", re.IGNORECASE), "manager"),
    (re.compile(r"\bsenior\b|\bsr\.?\b", re.IGNORECASE), "senior"),
)

# A years-of-experience mention is the weakest signal: it only justifies
# `mid` (never a named tier) when no rule above matched.
_EXPERIENCE_HINT_RE = re.compile(
    r"\b\d+\s*\+?\s*(?:to\s*\d+\s*\+?\s*)?years?\b", re.IGNORECASE
)


def classify_seniority(title: str | None, description: str | None = None) -> str | None:
    """Classify one job's seniority tier, or None when nothing asserts one.

    Args:
        title: The job title (the primary signal).
        description: The listing text, used only when the title matches
            no rule — and even then only for rule matches and
            years-of-experience hints.

    Returns:
        One of intern|junior|mid|senior|staff|principal|lead|manager,
        or None (unknown — the value the filter surface treats as
        "not classified").
    """
    for text in (title, description):
        if not text or not text.strip():
            continue
        for pattern, tier in SENIORITY_RULES:
            if pattern.search(text):
                return tier

    # No named tier anywhere: a years-of-experience hint upgrades the row
    # from unknown to `mid` (spec's ordered rules) — but only in the
    # description, where such requirements actually live.
    if description and _EXPERIENCE_HINT_RE.search(description):
        return "mid"

    return None
