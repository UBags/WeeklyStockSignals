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
from datetime import UTC, date, datetime, time, timedelta
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
    # date -> early close (LOCAL). Empty for exchanges with no early closes.
    half_days: dict = field(default_factory=dict, compare=False, repr=False)
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
        """Length of one REGULAR session in hours."""
        return self._hours(self.open_time, self.close_time)

    @staticmethod
    def _hours(o: time, c: time) -> float:
        open_dt = datetime.combine(date(2000, 1, 1), o)
        close_dt = datetime.combine(date(2000, 1, 1), c)
        return (close_dt - open_dt).total_seconds() / 3600.0

    def session_hours_on(self, d: date) -> float:
        """Length of the session ON `d` — a half-day is shorter.

        The proration denominator used to be `session_hours * n_trading_days`,
        which claims a full session on a day the exchange closed early. For a NYSE
        13:00 close that is 6.5 hours claimed against 3.5 actually traded, and
        imputed_weekly_volume is overstated for the whole week.
        """
        early = self.half_days.get(d)
        if early is None:
            return self.session_hours
        return self._hours(self.open_time, early)

    def close_time_on(self, d: date) -> time:
        """The close ON `d`. Half-days close early."""
        return self.half_days.get(d, self.close_time)

    def is_trading_day(self, d: date) -> bool:
        return self.calendar.is_trading_day(d)

    def _session_hours_elapsed(self, now_local: datetime) -> float:
        """Trading hours elapsed within `now_local`'s own session.

        Uses the close for THAT day — on a half-day the session is already over
        at 13:00, and treating it as open until 16:00 understates elapsed hours
        (inflating the projection) for the rest of the afternoon.
        """
        d = now_local.date()
        close = self.close_time_on(d)
        tod = now_local.timetz().replace(tzinfo=None)
        if tod <= self.open_time:
            return 0.0
        if tod >= close:
            return self.session_hours_on(d)
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
        # SUM per-day lengths, do not multiply one constant by a day count: a
        # half-day is a shorter session, and claiming a full one inflates the
        # proration denominator for the entire week.
        v = sum(
            self.session_hours_on(d)
            for d in (monday_local + timedelta(days=i) for i in range(5))
            if self.is_trading_day(d)
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
        # Per-day again: a completed half-day contributes its SHORT session.
        hours = sum(
            self.session_hours_on(d)
            for d in (monday_local + timedelta(days=i) for i in range(days_before))
            if self.is_trading_day(d)
        )
        if not self.is_trading_day(as_of):
            return hours
        if now_local.date() == as_of:
            hours += self._session_hours_elapsed(now_local)
        else:
            hours += self.session_hours_on(as_of)
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
    # date -> early close, LOCAL time. NYSE closes at 13:00 on ~9 days a year
    # (day after Thanksgiving, Christmas Eve, July 3rd). Proration that assumes a
    # full 6.5h session on those days overstates imputed weekly volume.
    half_days: tuple[tuple[date, time], ...] = ()


_EXCHANGE_SPECS: dict[str, _ExchangeSpec] = {
    "NSE": _ExchangeSpec("Asia/Kolkata", time(9, 15), time(15, 30), "IN"),
    "BSE": _ExchangeSpec("Asia/Kolkata", time(9, 15), time(15, 30), "IN"),
    "NYSE": _ExchangeSpec("America/New_York", time(9, 30), time(16, 0), "US"),
    "NASDAQ": _ExchangeSpec("America/New_York", time(9, 30), time(16, 0), "US"),
    "LSE": _ExchangeSpec("Europe/London", time(8, 0), time(16, 30), "GB"),
}

# L6-SPECS-ARE-ISOLATED: the specs as they are BEFORE any config touches them.
#
# _EXCHANGE_SPECS is a module-global mutable dict and seed_specs_from_config()
# rewrites it in place — which is right for a process that loads one config at
# startup, and catastrophic for a process that loads several. A test suite loads
# dozens. One fixture declaring `exchange: NYSE, tz: Asia/Kolkata` (which one did)
# silently moved New York to India for every test that ran after it in the same
# worker, and nothing noticed until something finally asked market_tz("NYSE").
#
# Snapshotting the defaults costs one dict copy and gives the state a way back.
_DEFAULT_EXCHANGE_SPECS: dict[str, _ExchangeSpec] = dict(_EXCHANGE_SPECS)


def reset_specs_to_defaults() -> None:
    """Restore the exchange registry to its pre-config state.

    For tests. Production seeds once, at startup, from the operator's config and
    never looks back — that is the intended lifecycle. A test process, though,
    loads many configs, and each one is entitled to be wrong about markets it does
    not care about. This is the undo that makes that safe.
    """
    _EXCHANGE_SPECS.clear()
    _EXCHANGE_SPECS.update(_DEFAULT_EXCHANGE_SPECS)


class UnknownExchangeError(ValueError):
    """No session spec for this exchange. There is no safe default."""


def seed_specs_from_config(config) -> None:
    """Make config.yaml the single source of truth for what a market is.

    _EXCHANGE_SPECS duplicated config.market_hours: two places declaring every
    exchange's timezone and session window, kept in step by nothing. Called once
    at startup, this replaces the hardcoded defaults with what the operator
    actually configured.

    The hardcoded specs remain only as the pre-config default (for tests and for
    tooling that never loads a Config); the drift test pins the two together.
    """
    for mh in config.market_hours:
        _EXCHANGE_SPECS[mh.exchange.upper()] = _ExchangeSpec(
            tz=mh.tz,
            open_time=_hhmm(mh.open),
            close_time=_hhmm(mh.close),
            holidays_country=mh.holidays_country,
            trading_weekdays=frozenset(mh.weekdays),
            half_days=tuple(
                (date.fromisoformat(h.date), _hhmm(h.close))
                for h in (mh.half_days or [])
            ),
        )


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def market_session_for(
    exchange: str,
    *,
    override_path: Path | None = None,
) -> MarketSession:
    """Resolve the MarketSession for `exchange`.

    RAISES on an unknown exchange. There used to be a `fallback_tz` here — fed,
    in practice, by tickers.tz, which was 'UTC' for all 2,685 NSE tickers — and an
    unknown exchange silently got a generic 09:00-16:00 UTC session with nothing
    but a log warning.

    That is not a safe default; it is a wrong one. If you do not know when the
    market opens you cannot prorate its volume, and every downstream number is
    quietly wrong. A warning in a log is not a gate.
    """
    spec = _EXCHANGE_SPECS.get(exchange.upper())
    if spec is None:
        raise UnknownExchangeError(
            f"market_session_for: no session spec for exchange {exchange!r}. "
            f"Known: {sorted(_EXCHANGE_SPECS)}. Add it to config.yaml's "
            f"market_hours — guessing a session window silently mis-prorates "
            f"volume for every ticker on the exchange."
        )
    cal = ExchangeCalendar(
        spec.holidays_country, spec.trading_weekdays, override_path,
    )
    return MarketSession(
        exchange=exchange, tz=spec.tz,
        open_time=spec.open_time, close_time=spec.close_time,
        calendar=cal,
        half_days=dict(spec.half_days),
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


def market_today(exchange: str) -> date:
    """The date it is RIGHT NOW on `exchange` — not on this machine.

    date.today() takes no arguments. There is no timezone parameter and no way to
    make it correct; that is precisely why it is the dangerous one. This is the
    only correct spelling, given a name.

    At 22:30 UTC a NUC in Bangalore says the date is the 14th. For a NYSE ticker
    it is still the 13th.
    """
    return datetime.now(market_tz(exchange)).date()


def market_now(exchange: str) -> datetime:
    """The instant it is right now, in `exchange`'s local clock.

    For display and for filenames: an export of NYSE signals stamped in Indian
    time is a lie about when it was taken.
    """
    return datetime.now(market_tz(exchange))


# --------------------------------------------------------------------------- #
# L1-ANCHOR-RULE: the instant <-> local-calendar boundary.
#
# A timestamptz is an INSTANT. A trading date and a week anchor are LOCAL
# CALENDAR FACTS. Crossing between them without naming a zone is not a shortcut,
# it is an assumption — and on this machine (server TZ Asia/Kolkata, market NSE,
# UTC to the west) every such assumption happens to come out right, which is
# precisely why they survived. They stop being right at NYSE.
#
# THE RULE: a bar's timestamp is the local-midnight instant of the first calendar
# day of the period it covers, in the exchange's own zone.
#
#     daily bar  -> local midnight of its trading date
#     weekly bar -> local midnight of its local Monday
#
# Both are invertible by construction: trading_date_of(bar_anchor(d, ex), ex) == d
# for every exchange and every date, DST included. Midnight-UTC is invertible only
# for zones east of UTC, which is a property of India, not a property of the code.
# --------------------------------------------------------------------------- #


def market_tz_name(exchange: str) -> str:
    """The exchange's IANA zone name, for SQL binds (`AT TIME ZONE :tz`).

    SQL cannot take a ZoneInfo. It takes the name — and the name must come from
    the same authority the Python side uses, or the two drift.
    """
    return market_tz(exchange).key


def monday_of(d: date) -> date:
    """The Monday of the local week containing local date `d`.

    Pure calendar arithmetic on a date that is ALREADY local — no zone involved,
    because a date has no zone. Three modules carried their own copy of this line;
    this is the one.
    """
    return d - timedelta(days=d.weekday())


def local_midnight_in_zone(d: date, zone: ZoneInfo) -> datetime:
    """The instant at which local date `d` begins in `zone`, as UTC.

    ZoneInfo attaches the correct offset for that date, so DST is handled at the
    point of attachment and there is no offset arithmetic anywhere.
    """
    return datetime.combine(d, time.min, tzinfo=zone).astimezone(UTC)


def monday_instant_of(ts: datetime, zone: ZoneInfo) -> datetime:
    """The local-Monday-midnight instant of the week containing instant `ts`.

    The week anchor. `ts` MUST be aware: a naive datetime has no instant, so
    there is no correct answer, and guessing one is how this class of bug is born.
    """
    if ts.tzinfo is None:
        raise ValueError(
            "monday_instant_of requires a tz-aware datetime; got naive. A naive "
            "datetime names no instant — supply the zone at the point of origin."
        )
    local = ts.astimezone(zone)
    return local_midnight_in_zone(monday_of(local.date()), zone)


def bar_anchor(trading_date: date, exchange: str) -> datetime:
    """THE timestamp of a daily bar: local midnight of its trading date.

    The only sanctioned way to turn a trading date into a bar_timestamp.
    """
    return local_midnight_in_zone(trading_date, market_tz(exchange))


def week_anchor(d: date, exchange: str) -> datetime:
    """THE timestamp of a weekly bar: local midnight of the local Monday of `d`."""
    zone = market_tz(exchange)
    return local_midnight_in_zone(monday_of(d), zone)


def trading_date_of(ts: datetime, exchange: str) -> date:
    """The local calendar date of instant `ts` on `exchange`.

    The inverse of bar_anchor, and the ONLY sanctioned way to get a date out of a
    timestamp that came from the database. A bare `ts.date()` on a value psycopg
    handed back is a silent read of the SESSION timezone — a clock nobody in this
    codebase chose and nobody tests.
    """
    if ts.tzinfo is None:
        raise ValueError(
            f"trading_date_of got a naive datetime ({ts!r}). Naive datetimes do "
            f"not name an instant; there is no correct trading date for one."
        )
    return ts.astimezone(market_tz(exchange)).date()


def day_bounds(d: date, exchange: str) -> tuple[datetime, datetime]:
    """Half-open [start, end) covering local date `d` on `exchange`.

    Half-open, not inclusive-with-23:59:59.999999: the closed form has to invent
    a last representable instant, and "the largest microsecond before midnight" is
    a fact about the storage type, not about the day. `>= start AND < end` is the
    only form that cannot lose a row to rounding or gain one from the next day.
    """
    start = local_midnight_in_zone(d, market_tz(exchange))
    end = local_midnight_in_zone(d + timedelta(days=1), market_tz(exchange))
    return start, end


def range_bounds(
    date_from: date, date_to: date, exchange: str,
) -> tuple[datetime, datetime]:
    """Half-open [start, end) covering local dates [date_from, date_to] inclusive."""
    start, _ = day_bounds(date_from, exchange)
    _, end = day_bounds(date_to, exchange)
    return start, end


def exchange_of_engine_or_none(db_engine) -> str | None:
    """The database's exchange, or None if it does not have exactly one.

    For DISPLAY only. An un-provisioned database — a fresh install, before the
    first backfill — has no tickers and therefore no exchange. That is a real
    state, and the GUI has to render in it.
    """
    try:
        return exchange_of_engine(db_engine)
    except Exception:
        return None


def display_now(db_engine) -> datetime:
    """"Now", in the exchange's clock, for a GUI label.

    Falls back to UTC when the database has no exchange yet. UTC is explicit and
    deterministic; the machine's local clock is neither, and is the bug this whole
    workstream exists to remove.

    A DISPLAY fallback, never a compute one. Nothing on the signal path may guess
    an exchange — but a label that tells an operator the time may say UTC rather
    than crash the panel it lives in.
    """
    ex = exchange_of_engine_or_none(db_engine)
    return market_now(ex) if ex else datetime.now(UTC)


def exchange_of_conn(conn) -> str:
    """Which exchange does this DATABASE belong to? (Connection variant.)

    Same invariant as exchange_of_engine — exactly one exchange per database —
    for callers that hold a Connection rather than an Engine (the email composer
    runs inside the caller's transaction).

    Not cached: a Connection is short-lived and the query is a single indexed
    read. exchange_of_engine caches on the URL, which is the hot path.
    """
    from sqlalchemy import text as _text

    rows = conn.execute(_text(
        "SELECT DISTINCT exchange FROM tickers WHERE exchange IS NOT NULL"
    ))
    found = sorted(r[0] for r in rows)
    if len(found) != 1:
        raise ValueError(
            f"exchange_of_conn: expected EXACTLY ONE exchange in this database, "
            f"found {found or 'none'}. Under one-database-per-exchange a database "
            f"belongs to a single exchange."
        )
    return found[0]


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
