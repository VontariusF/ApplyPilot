"""Structured salary parsing: JobSpy pay strings → filterable numbers (spec Layer 1).

JobSpy delivers `min_amount`/`max_amount`/`currency`/`interval`, but the
store historically kept only the rendered display string (`salary`), whose
live shapes form a zoo: `60,000-82,000` (no currency),
`CN¥18,000-CN¥28,000/monthly`, `EUR5,030-EUR7,183/monthly`,
`USD 175,000-195,000/YEAR`, `USD119,000-USD224,000/yearly`, and single-value
variants. This module is the ONE parser for that shape family — ingestion
parses the rendered string it just built, and the startup backfill parses
the strings older rows already carry — so comparisons never regex at query
time (locked decision, spec art_we4AUxEy).

Stored values are annualized (`salary_min`/`salary_max` in annual units);
the original interval is kept in `salary_interval`. Pure functions only.
"""

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Interval suffixes seen in the wild, lowercased: JobSpy's canonical set
# (hourly/daily/weekly/monthly/yearly) plus bare-noun variants the boards
# render (`/YEAR`). Canonical names go into salary_interval; the factor
# table annualizes the amounts. Weekly/daily use working-time conventions
# (52 weeks, 260 working days) — the spec fixes monthly and hourly only.
_INTERVAL_ALIASES = {
    "year": "yearly", "yearly": "yearly", "annual": "yearly", "annually": "yearly",
    "month": "monthly", "monthly": "monthly",
    "week": "weekly", "weekly": "weekly",
    "day": "daily", "daily": "daily",
    "hour": "hourly", "hourly": "hourly",
}

_ANNUAL_FACTORS = {
    "yearly": 1.0,
    "monthly": 12.0,
    "weekly": 52.0,
    "daily": 260.0,
    "hourly": 2080.0,
}

# Leading currency token: an ISO-style code (USD, EUR), or a symbol,
# possibly with a country prefix (`CN¥`, `US$`). Optional single space
# between code and amount (`USD 175,000`).
_CURRENCY_RE = re.compile(r"^(?P<cur>[A-Za-z]{1,3}[€$£¥]|[$€£¥]|[A-Za-z]{2,4})\s*")

# One amount: digits with optional thousands separators and decimals.
_AMOUNT_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True)
class ParsedSalary:
    """Annualized pay range parsed from a salary display string.

    `interval` is the canonical stored form (yearly|daily|weekly|monthly|
    hourly, lowercased); `currency` is as delivered (None when the string
    carries none — the live store has such rows).
    """

    min_annual: float
    max_annual: float
    currency: str | None
    interval: str | None


def annualize_amount(amount: float, interval: str | None) -> float:
    """Scale one pay amount to annual units.

    Unknown or missing intervals are treated as already-annual (the live
    store's un-suffixed strings are annual figures).
    """
    return amount * _ANNUAL_FACTORS.get(interval or "", 1.0)


def parse_salary_display(salary: str | None) -> ParsedSalary | None:
    """Parse one salary display string into an annualized range.

    Tolerant by design: a string whose currency, interval, or formatting
    doesn't match anything known yields None (the caller keeps the job
    with NULL structured fields — B10's keep-the-row pattern), never
    raises. Returns None for None/empty input.
    """
    if not salary or not salary.strip():
        return None

    text = salary.strip()

    # Interval suffix after the first '/'
    interval: str | None = None
    if "/" in text:
        text, _, suffix = text.partition("/")
        alias = _INTERVAL_ALIASES.get(suffix.strip().lower())
        if alias:
            interval = alias
        elif suffix.strip():
            # Unknown unit — keep it verbatim (lowercased) so the data
            # isn't silently discarded, and treat the amounts as annual.
            interval = suffix.strip().lower()

    # Currency prefix, if any
    currency: str | None = None
    match = _CURRENCY_RE.match(text)
    if match and match.group("cur"):
        # A bare alphabetic token followed by more letters/digits could be
        # part of a word, not a currency — require an amount to follow.
        rest = text[match.end():]
        if _AMOUNT_RE.match(rest.lstrip()) or not match.group("cur").isalpha():
            currency = match.group("cur")
            text = rest

    amounts = [float(a.replace(",", "")) for a in _AMOUNT_RE.findall(text)]
    if not amounts:
        return None

    low = min(amounts[0], amounts[-1])
    high = max(amounts[0], amounts[-1])

    return ParsedSalary(
        min_annual=annualize_amount(low, interval),
        max_annual=annualize_amount(high, interval),
        currency=currency,
        interval=interval,
    )
