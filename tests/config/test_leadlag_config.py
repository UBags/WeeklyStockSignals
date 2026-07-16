"""Tests for the leadlag config section (leadlag G1)."""
from __future__ import annotations

import pytest

from trip_ema.config.schema import Config, LeadlagConfig


def test_defaults():
    c = LeadlagConfig()
    assert c.industry_window_weeks == 260
    assert c.stock_windows_weeks == (52, 260)
    assert c.max_corroborators == 3


def test_overrides():
    c = LeadlagConfig(
        industry_window_weeks=312,
        stock_windows_weeks=[26, 104],
        max_corroborators=2,
    )
    assert c.industry_window_weeks == 312
    assert c.stock_windows_weeks == (26, 104)   # coerced to tuple
    assert c.max_corroborators == 2


@pytest.mark.parametrize("bad", [
    {"industry_window_weeks": 5},        # < 13
    {"stock_windows_weeks": []},         # empty
    {"stock_windows_weeks": [5, 52]},    # a window < 13
    {"max_corroborators": 0},            # < 1
    {"unknown_key": 1},                  # extra forbidden
])
def test_validation_rejects(bad):
    with pytest.raises(Exception):
        LeadlagConfig(**bad)


def test_config_section_is_optional_with_default():
    # The top-level Config exposes leadlag with a default instance, so the
    # section may be omitted from config.yaml.
    assert Config.model_fields["leadlag"].default == LeadlagConfig()
