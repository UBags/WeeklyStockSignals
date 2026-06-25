"""NSE trading-day calendar.

A trading day is any weekday (Mon-Fri) that's NOT in the NSE holiday
list. We use the `holidays` package's India calendar (BSE/NSE share
the same holidays as the broader Indian banking calendar plus a few
extras), with an optional YAML override file for the rare cases where
NSE deviates (muhurat trading days, mid-week closures for elections,
etc.).

The runner uses this to:
  - Decide whether to run the hourly intraday ETL (skip on holidays).
  - Compute the expected "last good bar date" for staleness checks.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


# IST (NSE local time)
IST_OFFSET_HOURS = 5
IST_OFFSET_MINUTES = 30


def _india_holidays_for_year(year: int) -> set[date]:
    """Wrap the holidays package. Imported lazily so the etl extra
    can install it without the rest of the codebase needing it."""
    try:
        import holidays as hpkg
    except ImportError:
        logger.warning(
            "holidays package not installed — NSE holiday detection disabled. "
            "Install via `pip install trip-ema[etl]` or `pip install holidays`."
        )
        return set()
    # holidays.India() covers federal holidays. NSE adds market-specific
    # ones (Maharashtra Day, Ganesh Chaturthi observance), but they
    # mostly overlap India().
    try:
        return set(hpkg.India(years=year).keys())
    except Exception:
        logger.exception("Failed to fetch India holidays for year %d", year)
        return set()


class NseTradingCalendar:
    """Mutable: the YAML override can be reloaded. Construct once per
    CLI invocation."""

    def __init__(
        self,
        override_path: Path | None = None,
    ):
        self._override_path = override_path
        self._holiday_cache: dict[int, set[date]] = {}
        self._override_holidays: set[date] = set()
        self._override_special_trading: set[date] = set()
        self._load_overrides()

    def _load_overrides(self) -> None:
        if not self._override_path or not self._override_path.exists():
            return
        import yaml
        try:
            data = yaml.safe_load(self._override_path.read_text())
            if not data:
                return
            for v in data.get("extra_holidays", []) or []:
                self._override_holidays.add(_coerce_date(v))
            for v in data.get("special_trading_days", []) or []:
                self._override_special_trading.add(_coerce_date(v))
        except Exception:
            logger.exception(
                "Failed to load NSE holiday override; ignoring",
                extra={"path": str(self._override_path)},
            )

    def is_trading_day(self, d: date) -> bool:
        """A trading day is Mon-Fri minus India federal holidays minus
        explicit overrides, plus explicit special trading days."""
        # Explicit special trading day overrides weekday/holiday checks
        if d in self._override_special_trading:
            return True
        # Weekend: not a trading day
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        # Explicit holiday override
        if d in self._override_holidays:
            return False
        # India federal holidays
        if d.year not in self._holiday_cache:
            self._holiday_cache[d.year] = _india_holidays_for_year(d.year)
        return d not in self._holiday_cache[d.year]

    def previous_trading_day(self, d: date) -> date:
        """Walk back until we find a trading day. If `d` itself is a
        trading day, returns `d - 1d`'s nearest trading day."""
        from datetime import timedelta
        candidate = d - timedelta(days=1)
        # Safety: don't loop forever if every day in the lookback
        # window is somehow a holiday (impossible in practice).
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        return candidate


def _coerce_date(v) -> date:
    """YAML auto-typing may give us a date object directly (for
    `2026-05-04`-style values) or a string. Accept both."""
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v)
    raise ValueError(f"cannot coerce {v!r} to date")
