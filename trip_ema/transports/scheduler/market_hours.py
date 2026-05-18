"""Market-hours service — purely functional 'is exchange X open at time T?'.

Configured from `Config.market_hours`. Each exchange has open/close times
in its local timezone plus a list of ISO weekdays (1=Mon..7=Sun) on which
it trades.

The service is stateless and thread-safe. Callers pass timezone-aware UTC
timestamps; conversions to exchange-local time happen internally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ..config.schema import MarketHoursConfig


@dataclass(frozen=True, slots=True)
class _CompiledHours:
    """Internal form: open/close as `time` objects, tz as ZoneInfo, set of
    open weekdays as ints in 1..7."""
    exchange: str
    open_time: time
    close_time: time
    tz: ZoneInfo
    weekdays: frozenset[int]


def _parse_hhmm(s: str) -> time:
    """Strict 'HH:MM' parser. Pydantic validates upstream that the string
    is well-formed, but we re-parse here so the service is usable in
    isolation (e.g. from tests passing raw configs)."""
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


class MarketHoursService:
    """Look up whether a given exchange is open at a given UTC instant."""

    def __init__(self, hours_config: list[MarketHoursConfig]):
        self._by_exchange: dict[str, _CompiledHours] = {}
        for h in hours_config:
            self._by_exchange[h.exchange] = _CompiledHours(
                exchange=h.exchange,
                open_time=_parse_hhmm(h.open),
                close_time=_parse_hhmm(h.close),
                tz=ZoneInfo(h.tz),
                weekdays=frozenset(h.weekdays),
            )

    def known_exchanges(self) -> list[str]:
        return list(self._by_exchange.keys())

    def is_open(self, exchange: str, when: datetime) -> bool:
        """Return True iff `exchange` is open at the UTC instant `when`.

        Unknown exchanges raise KeyError — silently defaulting to 'closed'
        could hide config typos.

        Times are inclusive of open, exclusive of close: a query exactly
        at close-time returns False. This matches typical exchange
        semantics where the closing bell ends the session.
        """
        if when.tzinfo is None:
            raise ValueError("`when` must be timezone-aware")
        if exchange not in self._by_exchange:
            raise KeyError(f"Unknown exchange: {exchange!r}")

        hours = self._by_exchange[exchange]
        local = when.astimezone(hours.tz)
        # isoweekday: Mon=1..Sun=7, matches our config convention.
        if local.isoweekday() not in hours.weekdays:
            return False
        local_t = local.time()
        return hours.open_time <= local_t < hours.close_time

    def any_open(self, when: datetime) -> bool:
        """True iff at least one configured exchange is currently open.
        Used by the SubscriberDistributor as the 'should we be sending
        anything right now?' gate."""
        return any(
            self.is_open(ex, when) for ex in self._by_exchange
        )
