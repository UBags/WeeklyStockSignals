"""Per-exchange trading session — the multi-market basis for
daily-grain volume proration.

Weekly-indicator-sampled-daily needs, per exchange:
  * the session window (open/close local time) -> session length in
    hours, which drives the volume proration denominator,
  * the exchange timezone (week-Monday anchor + local trade date),
  * a trading calendar (weekday mask + public holidays) so the
    proration denominator counts *actual* sessions in the week.

`MarketSession` bundles those and owns the proration math, so the
daily-grain compute is exchange-agnostic: it just asks the session for
`imputed_weekly_volume(...)`.

The exchange specs below mirror config.yaml's `market_hours` block
(NSE/BSE/NYSE/LSE). config.market_hours remains the eventual single
source of truth; `market_session_for` can be re-seeded from a loaded
Config without touching callers. Unknown exchanges fall back to the
caller-supplied tz with a generic 09:00-16:00 weekday session.

Half-day early closes (e.g. NYSE 13:00 sessions) are not modelled —
proration uses the standard session length, a small and rare error on
those days only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cache
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Trading calendar (weekday mask + public holidays), country-driven.
# ------------------------------------------------------------------ #

class ExchangeCalendar:
    """Trading-day calendar for one exchange.

    A trading day is a configured weekday that is not a public holiday
    for `holidays_country` and not in the operator override file, plus
    any explicit special-trading-day overrides. Mirrors
    NseTradingCalendar's logic, generalised by country code.
    """

    def __init__(
        self,
        holidays_country: str | None,
        trading_weekdays: frozenset[int],  # 1=Mon..7=Sun (config convention)
        override_path: Path | None = None,
    ):
        self._country = holidays_country
        self._weekdays = trading_weekdays
        self._override_path = override_path
        self._holiday_cache: dict[int, set[date]] = {}
        self._override_holidays: set[date] = set()
        self._override_special: set[date] = set()
        self._load_overrides()

    def _load_overrides(self) -> None:
        if not self._override_path or not self._override_path.exists():
            return
        import yaml
        try:
            data = yaml.safe_load(self._override_path.read_text()) or {}
            for v in data.get("extra_holidays", []) or []:
                self._override_holidays.add(_coerce_date(v))
            for v in data.get("special_trading_days", []) or []:
                self._override_special.add(_coerce_date(v))
        except Exception:
            logger.exception(
                "ExchangeCalendar: failed to load override %s; ignoring",
                self._override_path,
            )

    def _country_holidays(self, year: int) -> set[date]:
        if not self._country:
            return set()
        if year not in self._holiday_cache:
            self._holiday_cache[year] = _holidays_for(self._country, year)
        return self._holiday_cache[year]

    def is_trading_day(self, d: date) -> bool:
        if d in self._override_special:
            return True
        if (d.weekday() + 1) not in self._weekdays:  # config 1=Mon..7=Sun
            return False
        if d in self._override_holidays:
            return False
        return d not in self._country_holidays(d.year)


# ------------------------------------------------------------------ #
# MarketSession
# ------------------------------------------------------------------ #

@cache
def _holidays_for(country: str, year: int) -> frozenset:
    """PERF-NAN-COUNT (same batch): process-wide holiday cache.

    ExchangeCalendar._holiday_cache is per-INSTANCE, and market_session_for()
    builds a fresh session per ticker — so every one of ~2,673 tickers was
    reconstructing all ~21 years of holiday objects (~2.9s each, profiled).
    Holidays for a (country, year) are immutable: build them once per process.

    Returns a frozenset so the cached value cannot be mutated by a caller.
    """
    try:
        import holidays as hpkg
        return frozenset(hpkg.country_holidays(country, years=year).keys())
    except Exception:
        logger.warning(
            "ExchangeCalendar: holidays unavailable for %s/%d; "
            "weekday-only calendar", country, year,
        )
        return frozenset()


@dataclass(frozen=True)
class MarketSession:
    """One exchange's session window, timezone and calendar, plus the
    trading-hours volume-proration math."""
    exchange: str
    tz: str
    open_time: time
    close_time: time
    calendar: ExchangeCalendar = field(compare=False)
    # Memo for week_total_hours (see below). compare/repr excluded so it plays
    # no part in equality or hashing.
    _wth_cache: dict = field(
        default_factory=dict, compare=False, repr=False,
    )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def session_hours(self) -> float:
        """Length of one regular session in hours."""
        open_dt = datetime.combine(date(2000, 1, 1), self.open_time)
        close_dt = datetime.combine(date(2000, 1, 1), self.close_time)
        return (close_dt - open_dt).total_seconds() / 3600.0

    def is_trading_day(self, d: date) -> bool:
        return self.calendar.is_trading_day(d)

    def _session_hours_elapsed(self, now_local: datetime) -> float:
        """Trading hours elapsed within `now_local`'s own session."""
        tod = now_local.timetz().replace(tzinfo=None)
        if tod <= self.open_time:
            return 0.0
        if tod >= self.close_time:
            return self.session_hours
        open_dt = datetime.combine(
            now_local.date(), self.open_time, tzinfo=now_local.tzinfo,
        )
        return (now_local - open_dt).total_seconds() / 3600.0

    def week_total_hours(self, monday_local: date) -> float:
        """Total scheduled trading hours in the Mon-Fri week starting
        `monday_local`, counting only calendar trading days.

        Memoized: a pure function of the Monday, but called once per DAY
        (5,003x per ticker) when only ~1,000 distinct Mondays exist. Mutating
        the cache dict is fine on a frozen dataclass; rebinding it is not.
        """
        hit = self._wth_cache.get(monday_local)
        if hit is not None:
            return hit
        v = self.session_hours * sum(
            1 for i in range(5)
            if self.is_trading_day(monday_local + timedelta(days=i))
        )
        self._wth_cache[monday_local] = v
        return v

    def week_hours_elapsed(
        self,
        monday_local: date,
        as_of: date,
        now_local: datetime,
    ) -> float:
        """Trading hours elapsed from Monday through `as_of`.

        Sessions strictly before `as_of` count in full. `as_of` itself
        counts as a full session when it is closed (past day / EoD /
        backfill) and as a partial session when it is the current day
        mid-session (live intraday, via `now_local`).
        """
        days_before = (as_of - monday_local).days
        full = sum(
            1 for i in range(days_before)
            if self.is_trading_day(monday_local + timedelta(days=i))
        )
        hours = self.session_hours * full
        if not self.is_trading_day(as_of):
            return hours
        if now_local.date() == as_of:
            hours += self._session_hours_elapsed(now_local)
        else:
            hours += self.session_hours
        return hours

    def imputed_weekly_volume(
        self,
        volume_week_to_date: float,
        monday_local: date,
        as_of: date,
        now_local: datetime,
    ) -> float:
        """Project week-to-date volume up to a full-week estimate:
        volume * (total_week_hours / hours_elapsed)."""
        total = self.week_total_hours(monday_local)
        elapsed = self.week_hours_elapsed(monday_local, as_of, now_local)
        if elapsed <= 0.0:
            return volume_week_to_date
        return volume_week_to_date * (total / elapsed)


# ------------------------------------------------------------------ #
# Exchange registry (mirrors config.yaml market_hours)
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class _ExchangeSpec:
    tz: str
    open_time: time
    close_time: time
    holidays_country: str | None
    trading_weekdays: frozenset[int] = frozenset({1, 2, 3, 4, 5})


_EXCHANGE_SPECS: dict[str, _ExchangeSpec] = {
    "NSE": _ExchangeSpec("Asia/Kolkata", time(9, 15), time(15, 30), "IN"),
    "BSE": _ExchangeSpec("Asia/Kolkata", time(9, 15), time(15, 30), "IN"),
    "NYSE": _ExchangeSpec("America/New_York", time(9, 30), time(16, 0), "US"),
    "NASDAQ": _ExchangeSpec("America/New_York", time(9, 30), time(16, 0), "US"),
    "LSE": _ExchangeSpec("Europe/London", time(8, 0), time(16, 30), "GB"),
}


def market_session_for(
    exchange: str,
    fallback_tz: str = "UTC",
    *,
    override_path: Path | None = None,
) -> MarketSession:
    """Resolve the MarketSession for `exchange`.

    Known exchanges use their spec (authoritative tz + session window +
    holiday country). Unknown exchanges fall back to `fallback_tz`
    (typically the ticker's stored tz) with a generic 09:00-16:00
    weekday session and no holiday calendar.
    """
    spec = _EXCHANGE_SPECS.get(exchange.upper())
    if spec is None:
        logger.warning(
            "market_session_for: unknown exchange %r; using generic "
            "09:00-16:00 session in tz=%s", exchange, fallback_tz,
        )
        cal = ExchangeCalendar(None, frozenset({1, 2, 3, 4, 5}))
        return MarketSession(
            exchange=exchange, tz=fallback_tz,
            open_time=time(9, 0), close_time=time(16, 0), calendar=cal,
        )
    cal = ExchangeCalendar(
        spec.holidays_country, spec.trading_weekdays, override_path,
    )
    return MarketSession(
        exchange=exchange, tz=spec.tz,
        open_time=spec.open_time, close_time=spec.close_time,
        calendar=cal,
    )


def market_tz(exchange: str) -> ZoneInfo:
    """The exchange's timezone. THE single authority — nothing else may decide.

    Three modules on the signal path used to carry their own
    `_IST = ZoneInfo("Asia/Kolkata")`, which silently assumed every market is in
    India. On a NUC set to Asia/Kolkata running NSE, that assumption is invisibly
    true — which is exactly why it survived this long.
    """
    return market_session_for(exchange).zone


def post_session_time(exchange: str) -> time:
    """One hour after the close, in the exchange's LOCAL time.

    The daywalk needs an instant that is unambiguously after the session, to pick
    "the last bar at or before as_of". It used a hardcoded 16:30, which is not
    arbitrary — NSE closes at 15:30 — but which is 06:00 in New York, three hours
    BEFORE the opening bell.

    close + 1h gives 16:30 for NSE (bit-identical to the constant it replaces) and
    17:00 for NYSE. DST is handled by ZoneInfo at the point of attachment; there is
    no offset arithmetic here or anywhere.
    """
    close = market_session_for(exchange).close_time
    h = (close.hour + 1) % 24
    return time(h, close.minute)


def exchange_of_engine(db_engine) -> str:
    """Which exchange does this DATABASE belong to?

    Under one-database-per-exchange the database IS the exchange, so code holding
    only a db_engine can recover it without threading `exchange` through every
    signature — _build_industry_weekly_bars has none in scope, and giving it one
    would ripple through both industry runners and their callers.

    Asserts EXACTLY ONE exchange: the same invariant Commit L enforces when it
    refuses a backfill whose industry layer belongs to someone else. If this ever
    fires, two exchanges share a database and every industry index in it is
    suspect.

    Cached on the URL — called from per-ticker hot paths.
    """
    from sqlalchemy import text as _text

    url = str(db_engine.url)
    hit = _EXCHANGE_OF_DB.get(url)
    if hit is not None:
        return hit

    with db_engine.connect() as conn:
        rows = conn.execute(_text(
            "SELECT DISTINCT exchange FROM tickers WHERE exchange IS NOT NULL"
        ))
        found = sorted(r[0] for r in rows)

    if len(found) != 1:
        raise ValueError(
            f"exchange_of_engine: expected EXACTLY ONE exchange in this "
            f"database, found {found or 'none'}. Under one-database-per-exchange "
            f"a database belongs to a single exchange; more than one means the "
            f"industry layer (whose PK is (industry_name, trade_date), with no "
            f"exchange column) is shared, and every index in it is suspect."
        )
    _EXCHANGE_OF_DB[url] = found[0]
    return found[0]


_EXCHANGE_OF_DB: dict[str, str] = {}


def _coerce_date(v) -> date:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v)
    raise ValueError(f"cannot coerce {v!r} to date")
