"""Tests for the per-exchange `tick_schedule:` block introduced in
Phase 16a.

The schedule lives under each `market_hours[]` entry and explicitly
names the four daily tick instants (post-open, mid-session, pre-close,
post-close) as signed offsets from market open/close. The midpoint
entry is computed at runtime from the flanking open- and close-anchored
ticks; configuration only declares its existence.

Two surfaces under test here:

  1. `MarketHoursConfig.tick_schedule` field — backward compatibility
     when the block is absent, happy-path parsing when it is present.

  2. `MarketHoursConfig._validate_tick_schedule` — cross-entry
     validation: non-empty, unique names, and the midpoint-requires-
     flanking-ticks invariant. Validation runs at config-load time so
     misconfigurations surface in /tmp/trip-ema.log on restart rather
     than at first tick.

The legacy `post_open_offset_minutes` / `pre_close_offset_minutes` /
`intraday_cadence_minutes` fields are still validated by their own
single-field rules; this file does not retest them.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from trip_ema.config import Config, ConfigError, load_config
from trip_ema.config.schema import (
    MarketHoursConfig,
    TickScheduleEntry,
)


# ---------- YAML scaffolding ---------- #


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# Base YAML with one market (NSE), no tick_schedule. Used to verify
# backward-compat and as a scaffold for the positive cases.
_BASE_YAML = """
database:
  driver: postgresql
  host: localhost
  port: 5432
  database: market_data
  user: app
  password_env: TEST_DB_PWD
ema:
  med:     { base: 8,  s1: 4, s2: 4, final: null }
  long:    { base: 15, s1: 4, s2: 4, final: 3 }
  longest: { base: 28, s1: 7, s2: 5, final: 3 }
atr:
  length: 14
  inner_mult: 0.2
  mid_mult: 2.0
  outer_mult: 4.0
market_hours:
  - exchange: NSE
    open: "09:15"
    close: "15:30"
    tz: Asia/Kolkata
    weekdays: [1, 2, 3, 4, 5]
"""


# YAML snippet to splice an explicit tick_schedule into the NSE entry.
# Matches the four-tick spec from the original request:
#   - post_open  = open + 15
#   - mid_session = midpoint of post_open and pre_close
#   - pre_close  = close - 30
#   - post_close = close + 60
# These offsets are NSE-specific and the operator can edit them in
# config.yaml without code changes — that's the point of the block.
_NSE_TICK_SCHEDULE = """
    tick_schedule:
      - { name: post_open,   anchor: open,     offset_minutes:  15 }
      - { name: mid_session, anchor: midpoint                       }
      - { name: pre_close,   anchor: close,    offset_minutes: -30 }
      - { name: post_close,  anchor: close,    offset_minutes:  60 }
"""


@pytest.fixture
def db_pwd(monkeypatch):
    monkeypatch.setenv("TEST_DB_PWD", "secret")


# ---------- Backward compatibility ---------- #


class TestTickScheduleBackCompat:
    """Configs predating Phase 16a (no tick_schedule:) must keep loading
    with the same semantics. The legacy three-field cadence drives
    scheduling; the new field defaults to None."""

    def test_config_without_tick_schedule_loads_with_field_None(
        self, tmp_path, db_pwd,
    ):
        p = _write(tmp_path, _BASE_YAML)
        cfg = load_config(p)
        assert len(cfg.market_hours) == 1
        nse = cfg.market_hours[0]
        assert nse.tick_schedule is None

    def test_legacy_cadence_fields_still_have_their_defaults(
        self, tmp_path, db_pwd,
    ):
        """The legacy cadence fields are NOT removed — they're just
        ignored when tick_schedule is set. With tick_schedule absent,
        the defaults are unchanged from pre-16a."""
        p = _write(tmp_path, _BASE_YAML)
        cfg = load_config(p)
        nse = cfg.market_hours[0]
        assert nse.post_open_offset_minutes == 5
        assert nse.pre_close_offset_minutes == 15
        assert nse.intraday_cadence_minutes == 60

    def test_legacy_to_tick_config_still_works(self, tmp_path, db_pwd):
        """The existing `to_tick_config()` runtime adapter keeps producing
        a legacy MarketTickConfig when tick_schedule is None. Step 16b's
        tick_planner rewrite consumes the new shape via a different
        adapter; this one stays unchanged for callers that still need it."""
        p = _write(tmp_path, _BASE_YAML)
        cfg = load_config(p)
        nse = cfg.market_hours[0]
        # Lazy import sidesteps the scheduler-package dependency at test
        # collection time; same pattern as the existing adapter.
        try:
            tick_cfg = nse.to_tick_config()
        except ModuleNotFoundError:
            pytest.skip("scheduler package not importable in this test env")
        assert tick_cfg.exchange == "NSE"
        assert tick_cfg.post_open_offset.total_seconds() == 5 * 60


# ---------- Happy-path parsing ---------- #


class TestTickScheduleValidYaml:
    """A well-formed tick_schedule: block parses into the four-tick
    shape, in YAML order, with correct anchor and offset values."""

    def test_four_tick_schedule_parses(self, tmp_path, db_pwd):
        yaml = _BASE_YAML + _NSE_TICK_SCHEDULE
        p = _write(tmp_path, yaml)
        cfg = load_config(p)
        nse = cfg.market_hours[0]
        assert nse.tick_schedule is not None
        names = [t.name for t in nse.tick_schedule]
        assert names == ["post_open", "mid_session", "pre_close", "post_close"]

    def test_anchors_parse_correctly(self, tmp_path, db_pwd):
        yaml = _BASE_YAML + _NSE_TICK_SCHEDULE
        p = _write(tmp_path, yaml)
        cfg = load_config(p)
        nse = cfg.market_hours[0]
        by_name = {t.name: t for t in nse.tick_schedule}
        assert by_name["post_open"].anchor == "open"
        assert by_name["mid_session"].anchor == "midpoint"
        assert by_name["pre_close"].anchor == "close"
        assert by_name["post_close"].anchor == "close"

    def test_signed_offsets_parse_correctly(self, tmp_path, db_pwd):
        yaml = _BASE_YAML + _NSE_TICK_SCHEDULE
        p = _write(tmp_path, yaml)
        cfg = load_config(p)
        nse = cfg.market_hours[0]
        by_name = {t.name: t for t in nse.tick_schedule}
        assert by_name["post_open"].offset_minutes == 15
        assert by_name["mid_session"].offset_minutes == 0   # default
        assert by_name["pre_close"].offset_minutes == -30
        assert by_name["post_close"].offset_minutes == 60

    def test_two_markets_each_with_their_own_schedule(self, tmp_path, db_pwd):
        """Independence: NSE and NYSE can carry distinct schedules with
        different offsets — covers the design intent of per-exchange
        wall-clock control via YAML alone."""
        yaml = _BASE_YAML + _NSE_TICK_SCHEDULE + """
  - exchange: NYSE
    open: "09:30"
    close: "16:00"
    tz: America/New_York
    weekdays: [1, 2, 3, 4, 5]
    tick_schedule:
      - { name: post_open,   anchor: open,     offset_minutes:  15 }
      - { name: mid_session, anchor: midpoint                       }
      - { name: pre_close,   anchor: close,    offset_minutes: -30 }
      - { name: post_close,  anchor: close,    offset_minutes:  60 }
"""
        p = _write(tmp_path, yaml)
        cfg = load_config(p)
        assert len(cfg.market_hours) == 2
        by_exchange = {mh.exchange: mh for mh in cfg.market_hours}
        # Same offsets in this fixture, but the point is each market
        # carries its own list and is validated independently.
        assert len(by_exchange["NSE"].tick_schedule) == 4
        assert len(by_exchange["NYSE"].tick_schedule) == 4
        # Each market's tz/open/close are independent.
        assert by_exchange["NYSE"].tz == "America/New_York"
        assert by_exchange["NSE"].tz == "Asia/Kolkata"


# ---------- Validation failures (load-time) ---------- #


class TestTickScheduleValidation:
    """A tick_schedule: block can fail validation at load time in five
    documented ways. Each is verified to fail fast with a clear error
    that names the offending market — "fail loud at startup" beats
    "fire incorrectly at noon Tuesday"."""

    def test_empty_tick_schedule_rejected(self, tmp_path, db_pwd):
        """An empty list is rejected. Omit the block to keep legacy
        behavior; the empty-list case is almost certainly a YAML
        editing mistake."""
        yaml = _BASE_YAML + """
    tick_schedule: []
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="tick_schedule is empty"):
            load_config(p)

    def test_duplicate_tick_name_rejected(self, tmp_path, db_pwd):
        """Two entries with the same name silently mask one tick at
        scheduling time — reject at load time."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: post_open, anchor: open,  offset_minutes:  15 }
      - { name: post_open, anchor: close, offset_minutes: -30 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="duplicate tick name"):
            load_config(p)

    def test_midpoint_without_open_anchored_entry_rejected(
        self, tmp_path, db_pwd,
    ):
        """A midpoint-anchored entry without any open-anchored entry to
        compute from is undefined."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: mid_session, anchor: midpoint                  }
      - { name: post_close,  anchor: close, offset_minutes: 60 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="missing 'open'"):
            load_config(p)

    def test_midpoint_without_close_anchored_entry_rejected(
        self, tmp_path, db_pwd,
    ):
        """And symmetric — needs at least one close-anchored entry too."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: post_open,   anchor: open, offset_minutes: 15 }
      - { name: mid_session, anchor: midpoint                  }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="missing 'close'"):
            load_config(p)

    def test_midpoint_with_offset_rejected(self, tmp_path, db_pwd):
        """The midpoint anchor is fully determined by the flanking
        ticks; allowing a nonzero offset would be ambiguous."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: post_open,   anchor: open,     offset_minutes:  15 }
      - { name: mid_session, anchor: midpoint, offset_minutes:  10 }
      - { name: pre_close,   anchor: close,    offset_minutes: -30 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="does not support"):
            load_config(p)

    def test_unknown_anchor_value_rejected(self, tmp_path, db_pwd):
        """Anchor must be one of the three Literal values — typos like
        'midday' fail at load time, not at first tick."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: post_open, anchor: open,    offset_minutes:  15 }
      - { name: lunchtime, anchor: midday                       }
      - { name: pre_close, anchor: close,   offset_minutes: -30 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError):
            load_config(p)

    def test_extra_field_on_entry_rejected(self, tmp_path, db_pwd):
        """TickScheduleEntry uses extra='forbid'; a typo'd sub-key
        surfaces immediately rather than being silently ignored."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: post_open, anchor: open, offset_minutes: 15, when_local: "09:30" }
      - { name: pre_close, anchor: close, offset_minutes: -30 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError):
            load_config(p)

    def test_offset_out_of_range_rejected(self, tmp_path, db_pwd):
        """offset_minutes is bounded [-720, 720] (12 hours either side
        of the anchor) — bigger values almost certainly indicate a unit
        confusion (seconds vs minutes)."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: post_open, anchor: open,  offset_minutes:  15 }
      - { name: pre_close, anchor: close, offset_minutes: -7200 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError):
            load_config(p)

    def test_bad_name_with_special_char_rejected(self, tmp_path, db_pwd):
        """Names go into log labels and (potentially) Prometheus metric
        labels — enforce ASCII alphanumeric + underscore at config
        load time, not at metric emission."""
        yaml = _BASE_YAML + """
    tick_schedule:
      - { name: "post-open!", anchor: open, offset_minutes: 15 }
      - { name: pre_close,    anchor: close, offset_minutes: -30 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="ASCII alphanumeric"):
            load_config(p)


# ---------- Direct schema-level checks (no YAML/loader detour) ---------- #


class TestTickScheduleEntryDirect:
    """A few direct-construction checks for the smallest model, useful
    when a regression appears at the schema layer rather than at the
    loader. The YAML-driven tests above remain the primary surface."""

    def test_construct_explicit(self):
        e = TickScheduleEntry(name="post_open", anchor="open", offset_minutes=15)
        assert e.name == "post_open"
        assert e.anchor == "open"
        assert e.offset_minutes == 15

    def test_offset_defaults_to_zero(self):
        e = TickScheduleEntry(name="mid_session", anchor="midpoint")
        assert e.offset_minutes == 0

    def test_close_anchor_with_negative_offset(self):
        e = TickScheduleEntry(name="pre_close", anchor="close", offset_minutes=-30)
        assert e.offset_minutes == -30