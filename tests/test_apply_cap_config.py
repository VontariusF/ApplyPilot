"""Config-seam validation for the apply daily cap (spec art_NcNShMYR).

The cap is requested, never mandatory: apply.yaml's daily_cap is an
integer >= 0, malformed values are refused loudly (ApplyConfigError —
the typed config_invalid refusal), and an absent key/file means None —
no cap, which the launcher treats as uncapped. Validation mirrors
leads_daily_discovery_cap() exactly.
"""

import pytest

from kestrel import config


def write_cap(app_dir, raw: str) -> None:
    config.write_secret_text(config.APP_DIR / "apply.yaml",
                             f"daily_cap: {raw}\n")


def test_absent_file_means_no_cap(app_dir):
    assert config.load_apply_config() == {}
    assert config.apply_daily_cap() is None


def test_absent_key_means_no_cap(app_dir):
    config.write_secret_text(config.APP_DIR / "apply.yaml", "other_key: 1\n")
    assert config.apply_daily_cap() is None


@pytest.mark.parametrize("raw, expected", [("0", 0), ("5", 5), ("25", 25)])
def test_integer_zero_and_up_accepted(app_dir, raw, expected):
    write_cap(app_dir, raw)
    assert config.apply_daily_cap() == expected


@pytest.mark.parametrize("raw", ["banana", "'[1, 2]'"])
def test_non_integer_refused(app_dir, raw):
    write_cap(app_dir, raw)
    with pytest.raises(config.ApplyConfigError, match="must be an integer"):
        config.apply_daily_cap()


def test_negative_refused(app_dir):
    write_cap(app_dir, "-1")
    with pytest.raises(config.ApplyConfigError, match="must be >= 0"):
        config.apply_daily_cap()
