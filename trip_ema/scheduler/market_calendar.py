"""Multi-market trading-day calendar.

Generalizes the previous `NseTradingCalendar` (in `etl.holidays_calendar`)
to support arbitrary exchanges. A `TradingCalendar` instance is
constructed per market and asks the same questions of any exchange:
"is this date a trading day?", "what's the previous trading day?".

Backing data sources, in priority order:

  1. The YAML override file (`holidays_override_file`), if supplied.
     Exact wins over algorithm — operators always have the final word.
     Two override sections:
       - `extra_holidays:` — explicit non-trading dates
       - `special_trading_days:` — dates that ARE trading despite
         being holidays or weekends (e.g. NSE Muhurat Trading on
         Diwali)

  2. The `python-holidays` package's country calendar, keyed by
     `holidays_country` (e.g. "IN", "US", "GB"). This is lazy-imported
     so the package only needs to be installed when a calendar with
     a country code is actually constructed.

  3. The weekday filter from the market config (`weekdays`, ISO 1..7).
     A date whose ISO weekday is NOT in this set is automatically
     non-trading.

If `holidays_country` is None and no override file is supplied, the
calendar reduces to "every configured weekday is a trading day" —
useful for tests, exchanges without a published holiday calendar, or
markets where you want full operator control.

Backward-compat note: `NseTradingCalendar` (in
`trip_ema/etl/holidays_calendar.py`) is unchanged for now. The ETL
runner still uses it. A follow-up commit can switch the runner to
this generalized calendar via a NSE-preset construction helper, but
that's out of scope for this slice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TradingCalendarConfig:
    """Inputs needed to construct a TradingCalendar for one market.

    All fields except `exchange` and `weekdays` are optional. The
    minimal viable config is `(exchange="X", weekdays=[1,2,3,4,5])`,
    which yields a Mon-Fri-only calendar with no holiday awareness.
    """
    exchange: str
    weekdays: frozenset[int]
    """ISO weekdays open (1=Mon..7=Sun). Pre-validated upstream."""
    holidays_country: str | None = None
    """python-holidays country code, e.g. 'IN', 'US'. None disables
    algorithmic holiday lookup."""
    holidays_override_file: Path | None = None
    """YAML file with manual overrides; see module docstring."""


class TradingCalendar:
    """Asks 'is this date a trading day for exchange X?'.

    Constructed once per market at startup. Holds a per-year cache of
    looked-up holidays from the python-holidays package; the cache is
    populated lazily on first query for each year.

    Thread-safe for reads (the cache uses `dict.setdefault`-style
    population which is atomic for individual key insertions in
    CPython). Multiple TradingCalendar instances for different
    markets share no state.
    """

    # Hard cap on how far back `previous_trading_day` will walk before
    # giving up. 60 days is generous — a string of 60 consecutive
    # non-trading days is impossible in any real market (even Indian
    # Diwali week tops out at ~5 closed days).
    _MAX_LOOKBACK_DAYS = 60

    def __init__(self, config: TradingCalendarConfig):
        self._config = config
        # Per-year cache of dates returned by python-holidays. None
        # means we tried and it failed (package missing or country
        # unsupported); use the empty set as the effective cache for
        # any subsequent query.
        self._holiday_cache: dict[int, set[date]] = {}
        self._override_holidays: set[date] = set()
        self._override_special_trading: set[date] = set()
        self._load_overrides()

    # ---- Public API ---- #

    @property
    def exchange(self) -> str:
        return self._config.exchange

    def is_trading_day(self, d: date) -> bool:
        """True iff `d` is a trading day for this market.

        Decision order: special-trading override > weekday filter >
        explicit holiday override > algorithmic holiday lookup. The
        first three short-circuit; only if none of them produce a
        verdict do we consult python-holidays.
        """
        # 1. Operator override: special trading day. Wins over everything.
        if d in self._override_special_trading:
            return True
        # 2. Weekday filter.
        if d.isoweekday() not in self._config.weekdays:
            return False
        # 3. Operator override: explicit holiday.
        if d in self._override_holidays:
            return False
        # 4. Algorithmic holiday lookup, if a country was configured.
        if self._config.holidays_country is None:
            return True
        return d not in self._holidays_for_year(d.year)

    def previous_trading_day(self, d: date) -> date:
        """Walk backwards until we find a trading day.

        Returns a trading day strictly earlier than `d`; if `d` itself
        is a trading day, the *previous* trading day is returned (not
        `d` itself). Mirrors the contract from the old
        `NseTradingCalendar.previous_trading_day`.

        Raises `RuntimeError` if no trading day is found within
        `_MAX_LOOKBACK_DAYS` — strong signal of misconfiguration
        (every Mon-Fri marked as a holiday, etc.) rather than a real
        long closure.
        """
        candidate = d - timedelta(days=1)
        for _ in range(self._MAX_LOOKBACK_DAYS):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise RuntimeError(
            f"No trading day found within {self._MAX_LOOKBACK_DAYS} days "
            f"before {d.isoformat()} for exchange {self._config.exchange}; "
            f"check weekdays/holidays/override configuration."
        )

    def next_trading_day(self, d: date) -> date:
        """Walk forwards until we find a trading day.

        Returns a trading day strictly later than `d`; if `d` itself
        is a trading day, the *next* trading day is returned (not
        `d` itself).
        """
        candidate = d + timedelta(days=1)
        for _ in range(self._MAX_LOOKBACK_DAYS):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError(
            f"No trading day found within {self._MAX_LOOKBACK_DAYS} days "
            f"after {d.isoformat()} for exchange {self._config.exchange}; "
            f"check weekdays/holidays/override configuration."
        )

    # ---- Internals ---- #

    def _load_overrides(self) -> None:
        """Read the YAML override file, if configured. Failures here
        are logged-and-ignored: the calendar still functions correctly
        from the weekday filter and algorithmic holidays, it just
        loses operator overrides. We do NOT raise — that would prevent
        startup, which is worse than a malformed override file."""
        path = self._config.holidays_override_file
        if path is None or not path.exists():
            return
        try:
            import yaml  # lazy: only needed when overrides are configured
        except ImportError:
            logger.warning(
                "PyYAML not installed; cannot load holiday override file %s",
                path,
            )
            return
        try:
            data = yaml.safe_load(path.read_text())
            if not data:
                return
            for v in data.get("extra_holidays", []) or []:
                self._override_holidays.add(_coerce_date(v))
            for v in data.get("special_trading_days", []) or []:
                self._override_special_trading.add(_coerce_date(v))
        except Exception:
            logger.exception(
                "Failed to load holiday override file for %s; ignoring",
                self._config.exchange,
                extra={"path": str(path)},
            )

    def _holidays_for_year(self, year: int) -> set[date]:
        """Lazily populate and return the holiday set for one year."""
        if year in self._holiday_cache:
            return self._holiday_cache[year]
        result = self._fetch_holidays(year)
        self._holiday_cache[year] = result
        return result

    def _fetch_holidays(self, year: int) -> set[date]:
        """Call python-holidays for one year, with graceful failure.

        Returns an empty set on any failure (package missing,
        unsupported country, network issue in some backends, etc.) —
        the calendar then defaults to "weekday filter only," which
        is the safest fallback (slightly more trading days than
        reality, never fewer).
        """
        country = self._config.holidays_country
        assert country is not None  # enforced by caller
        try:
            import holidays as hpkg
        except ImportError:
            logger.warning(
                "python-holidays package not installed; %s holiday detection disabled",
                self._config.exchange,
            )
            return set()
        try:
            cal = hpkg.country_holidays(country, years=year)
            return set(cal.keys())
        except Exception:
            logger.exception(
                "Failed to fetch %s holidays for year %d via python-holidays",
                country, year,
            )
            return set()


def _coerce_date(v: Any) -> date:
    """YAML's auto-typing may give us either a `date` object directly
    (most common, since `2026-01-26` in YAML parses as a date) or a
    string that needs parsing. Handle both."""
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v)
    raise ValueError(
        f"Cannot coerce {v!r} (type {type(v).__name__}) to date; "
        f"expected ISO string or YAML date."
    )
