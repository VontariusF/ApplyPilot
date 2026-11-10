# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for two location-filter leaks seen in a real dogfood run.

User-verified 2026-11-10: of 635 stored jobs, 253 had blank locations
(Thomson Reuters, Moderna, PwC career sites) — one Moderna role in Norwood,
MA had the city visible in its URL but a blank location field — and
remote-tagged locations like "France, Remote" passed the US accept list
unconditionally because the remote-marker early return fired first.

Design (user-approved):
1. When a location accept list is configured, the location is derived from
   the job URL first (best-effort URL parsing, NOT geocoding); jobs still
   blank after derivation are REJECTED. The accept-all baseline (no accept
   list configured) is unchanged: blank locations still pass.
2. Remote markers are stripped from the location string and the geographic
   remainder is filtered against accept/reject. "France, Remote" filters on
   "France"; plain "Remote" (nothing left) always passes. Non-remote jobs
   behave exactly as before.
"""

import pytest

from kestrel.discovery.filters import derive_location_from_url, location_ok

MODERNA_NORWOOD_URL = "https://moderna.wd5.myworkdayjobs.com/en-US/ModernaCareers/job/Norwood-MA_United-States/R8685001"


# -- Leak 1: blank locations flooded the queue --------------------------------


def test_blank_location_with_city_in_url_is_accepted_and_derived():
    # The Moderna Norwood case: blank location, city in the URL slug.
    assert location_ok(None, ["Norwood"], [], url=MODERNA_NORWOOD_URL) is True


def test_blank_location_derived_from_url_fails_nonmatching_accept():
    # URL says London; a Norwood-only accept list must reject it.
    assert (
        location_ok(
            "",
            ["Norwood"],
            [],
            url="https://www.acme.co.uk/careers/senior-engineer-london-uk",
        )
        is False
    )


def test_blank_location_no_url_evidence_with_accept_list_is_rejected():
    # Opaque URL (no location evidence) + configured accept list -> reject.
    assert location_ok(None, ["Norwood"], [], url="https://boards.greenhouse.io/acme/jobs/123456") is False


def test_blank_location_no_accept_list_baseline_unchanged():
    # Accept-all configs keep the old semantics: blank locations still pass.
    assert location_ok(None, [], [], url=None) is True
    assert location_ok(None, [], [], url="https://example.com/jobs/123") is True


def test_derive_location_from_workday_url():
    assert derive_location_from_url(MODERNA_NORWOOD_URL) == "Norwood, MA"


def test_derive_location_from_query_param():
    assert derive_location_from_url("https://jobs.example.com/search?location=Austin%2C%20TX") == "Austin, TX"


def test_derive_location_from_generic_slug_state_name():
    derived = derive_location_from_url("https://www.example.com/careers/austin-texas")
    assert derived is not None
    assert "Austin" in derived
    assert derived.endswith("TX")


def test_derive_location_from_generic_slug_country():
    assert derive_location_from_url("https://www.example.com/jobs/munich-germany") == ("Munich, Germany")


def test_derive_location_remote_slug():
    assert derive_location_from_url("https://www.example.com/jobs/us-remote") == "Remote"


def test_derive_location_skips_locale_segments():
    # "en-us" is a locale, not a location.
    assert derive_location_from_url("https://www.example.com/en-us/jobs/12345") is None


def test_derive_location_opaque_url_returns_none():
    assert derive_location_from_url("https://jobs.ashbyhq.com/acme/abc123def456") is None


def test_derive_location_empty_url_returns_none():
    assert derive_location_from_url(None) is None
    assert derive_location_from_url("") is None


# -- Leak 2: remote-marker early return let geo-tagged remotes through --------


def test_remote_geo_tagged_rejected_against_accept_list():
    # The NVIDIA "France, Remote" case: filter the remainder ("france").
    assert location_ok("France, Remote", ["United States"], []) is False


def test_remote_geo_tagged_passes_matching_accept_list():
    assert location_ok("Norwood, MA, Remote", ["Norwood"], []) is True


def test_remote_geo_tagged_respects_reject_list():
    assert location_ok("Remote - Boston", [], ["boston"]) is False


def test_plain_remote_always_passes():
    # Nothing geographic remains -> always pass, even with an accept list.
    assert location_ok("Remote", ["Norwood"], []) is True
    assert location_ok("Remote", [], []) is True
    assert location_ok("Work From Home", ["Norwood"], []) is True


# -- Existing behavior is unchanged --------------------------------------------


def test_nonremote_accept_reject_unchanged():
    assert location_ok("New York, NY", ["new york"], []) is True
    assert location_ok("Boston, MA", ["new york"], []) is False
    assert location_ok("Boston, MA", [], ["boston"]) is False
    assert location_ok("San Francisco, CA", [], []) is True


def test_remote_with_empty_accept_and_reject_unchanged():
    assert location_ok("Remote", [], ["boston"]) is True
    assert location_ok("Anywhere", [], []) is True


@pytest.mark.parametrize(
    ("location", "accept", "reject", "expected"),
    [
        ("Remote", [], ["boston"], True),
        ("Remote", ["new york"], [], True),
        ("France, Remote", [], [], True),  # accept-all: geo remainder has no accept gate
    ],
)
def test_parametrized_baseline(location, accept, reject, expected):
    assert location_ok(location, accept, reject) is expected


# -- F1: accept patterns never match inside a word -----------------------------
#
# Dogfood 2026-11-09: 8 Intel jobs in Munich, Germany passed a "New York, NY"
# accept list because the derived pattern "NY" substring-matched inside
# "germany". Patterns must now start at a token boundary; the documented
# lexical looseness (word-start matches may extend) is unchanged.


def _new_york_accept() -> list[str]:
    from kestrel.discovery.filters import location_filters

    accept, _ = location_filters({"locations": [{"location": "New York, NY", "remote": False}]})
    return accept


def test_f1_derived_ny_rejects_munich_germany():
    accept = _new_york_accept()
    assert "NY" in accept  # the derived two-letter pattern is the one that leaked
    assert location_ok("Munich, Germany", accept, []) is False


def test_f1_new_york_jobs_still_accepted():
    accept = _new_york_accept()
    assert location_ok("New York, NY", accept, []) is True
    assert location_ok("New York, NY, USA", accept, []) is True
    assert location_ok("Brooklyn, NY", accept, []) is True


def test_f1_explicit_flat_key_behavior_unchanged():
    from kestrel.discovery.filters import location_filters

    accept, reject = location_filters(
        {"location_accept": ["New York, NY"], "location_reject_non_remote": ["Boston"]}
    )
    assert accept == ["New York, NY"]  # flat key used verbatim, no derivation
    assert location_ok("New York, NY", accept, reject) is True
    assert location_ok("Munich, Germany", accept, reject) is False
    assert location_ok("Boston, MA", accept, reject) is False  # reject still applies


def test_f1_two_letter_pattern_inside_a_longer_word():
    # Class-of-bugs guard beyond the Munich case: "MA" must not match
    # inside "Germantown".
    assert location_ok("Germantown, MD", ["MA"], []) is False


def test_f1_interior_reject_match_no_longer_fires():
    # Same shape on the reject side: "or" used to reject "Norway" (and
    # "Oregon" via its interior). Interior matches are gone...
    assert location_ok("Oslo, Norway", [], ["or"]) is True
    # ...while a word-start match keeps the lexical looseness.
    assert location_ok("Portland, OR", [], ["or"]) is False


def test_f1_word_start_prefix_looseness_unchanged():
    # The pinned B11 looseness: derived "NY" still matches "Remote — NYC".
    assert location_ok("Remote — NYC", _new_york_accept(), []) is True
