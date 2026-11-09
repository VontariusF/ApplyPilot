"""Structured salary parsing: JobSpy pay strings → filterable numbers (spec Layer 1).

JobSpy delivers `min_amount`/`max_amount`/`currency`/`interval`, but the
store historically kept only the rendered display string (`salary`), whose
live shapes form a zoo: `60,000-82,000` (no currency),
`CN¥18,000-CN¥28,000/monthly`, `EUR5,030-EUR7,183/monthly`,
`USD 175,000-195,000/YEAR`, `USD119,000-USD224,000/yearly`, and single-value
variants. Two parsers serve two sources (locked decision, spec
art_we4AUxEy):

- `parse_salary_row` — ingestion reads the RAW JobSpy row values directly
  (no round trip through the display string, which truncates fractional
  amounts and drops max-only rows).
- `parse_salary_display` — the startup backfill parses the display
  strings older rows already carry.

Stored values are annualized (`salary_min`/`salary_max` in annual units);
the original interval is kept in `salary_interval`. Pure functions only.
"""

import logging
import math
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
    "yr": "yearly",
    "month": "monthly", "monthly": "monthly", "mo": "monthly",
    "week": "weekly", "weekly": "weekly", "wk": "weekly",
    "day": "daily", "daily": "daily",
    "hour": "hourly", "hourly": "hourly", "hr": "hourly",
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

# One amount: digits with optional thousands separators and decimals,
# optionally k/m-suffixed (`90k`, `1.5m`). The lookahead keeps a trailing
# k/m from gluing onto a longer word ("walk", "income").
_AMOUNT_RE = re.compile(r"(?P<num>\d+(?:,\d{3})*(?:\.\d+)?)(?P<suffix>[km])?(?![a-z])", re.IGNORECASE)


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


def _canonical_interval(interval: str | None) -> str | None:
    """Map one interval spelling to its canonical stored form (or None)."""
    if not interval or not interval.strip():
        return None
    name = interval.strip().lower()
    return _INTERVAL_ALIASES.get(name, name)


def annualize_amount(amount: float, interval: str | None) -> float:
    """Scale one pay amount to annual units.

    Unknown or missing intervals are treated as already-annual (the live
    store's un-suffixed strings are annual figures).
    """
    return amount * _ANNUAL_FACTORS.get(interval or "", 1.0)


def _amount_value(match: re.Match[str]) -> float:
    """One _AMOUNT_RE match → its numeric value, k/m suffixes scaled."""
    value = float(match.group("num").replace(",", ""))
    suffix = (match.group("suffix") or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


def parse_salary_row(min_amount, max_amount, currency=None, interval=None) -> ParsedSalary | None:
    """Parse a raw JobSpy row's structured pay fields into an annualized range.

    Reads the row's `min_amount`/`max_amount`/`currency`/`interval` values
    directly — fractional amounts keep their value and a max-only row
    (missing minimum) still yields a populated range. Amounts that are
    missing, non-numeric, or non-finite (pandas NaN) are ignored; a row
    with no usable amount yields None. Total: never raises.
    """
    amounts = []
    for raw in (min_amount, max_amount):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            amounts.append(value)
    if not amounts:
        return None

    low = min(amounts)
    high = max(amounts)
    cur = str(currency).strip() if currency is not None else ""
    return ParsedSalary(
        min_annual=annualize_amount(low, _canonical_interval(interval)),
        max_annual=annualize_amount(high, _canonical_interval(interval)),
        currency=cur or None,
        interval=_canonical_interval(interval),
    )


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
        # Unknown unit — keep it verbatim (lowercased) so the data
        # isn't silently discarded, and treat the amounts as annual.
        interval = _canonical_interval(suffix)

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

    amounts = [_amount_value(m) for m in _AMOUNT_RE.finditer(text)]
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
