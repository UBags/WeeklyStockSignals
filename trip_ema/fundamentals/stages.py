"""The fundamentals runners, as data.

Phase 9 of the de-monkeypatching programme. Same shape as `MarketStages`
(Phase 6) and `BackfillStages` (Phase 8), and it exists for the same reason:
every fundamentals runner was a module-level name, so a test had exactly one
way to stub one — rewrite the *caller's* module attributes.

Two callers, two different holes, one cause:

    scheduler._fire()            an if/elif chain over phase names, resolving
                                 module globals.
    routes/fundamentals.py       function-LOCAL imports inside the executor
                                 wrappers (`from ....fundamentals.runners
                                 import run_shares_refresh`).

Those local imports never had a runtime purpose. There is no cycle to break
here and no cost to import at module top. They exist because a test needed a
patch target inside `trip_ema.fundamentals.runners` rather than inside the
caller. That is a seam being cut by the test suite into production code, which
is the definition of the debt this programme is retiring.

`for_phase()` is the whole point of the registry over a plain dict: the set of
legal phase names is now *derived from the dataclass fields*, so a phase with
no runner behind it cannot be constructed. The scheduler's old defensive
`else: logger.warning("no runner for phase")` branch — and the test that
monkeypatched SCHEDULED_PHASES to reach it — are both gone. A guard against a
state the type makes impossible is theatre.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import Any

from .runners import (
    run_financials_refresh,
    run_industry_index_build,
    run_industry_refresh,
    run_market_cap_backfill,
    run_shares_refresh,
)
from .v1_runners import populate_industry_index_value_for_industry


class UnknownPhaseError(KeyError):
    """A phase name with no runner behind it.

    Raised at CONSTRUCTION time (of the scheduler, or of a derived registry),
    never mid-tick. A fundamentals refresh is a 30-minute, thousand-request job
    against a rate-limited provider; discovering the phase is bogus after the
    loop has started is discovering it too late.
    """


@dataclass(frozen=True)
class FundamentalsRunners:
    """The runners one scheduler tick / one HTTP handler may fire.

    Frozen. `.with_(...)` derives a variant; the rest stay real. Field names
    ARE the phase names — `fundamentals_run.phase` in the DB, the `phase`
    argument to the scheduler, and the key here are the same string.
    """

    industry: Callable[..., Any]
    shares: Callable[..., Any]
    financials: Callable[..., Any]
    market_cap: Callable[..., Any]
    index_build: Callable[..., Any]
    index_value: Callable[..., Any]

    @classmethod
    def phases(cls) -> frozenset[str]:
        return frozenset(f.name for f in fields(cls))

    def with_(self, **overrides: Any) -> "FundamentalsRunners":
        unknown = sorted(set(overrides) - self.phases())
        if unknown:
            raise UnknownPhaseError(
                f"no such fundamentals phase(s): {unknown}. "
                f"Known phases: {sorted(self.phases())}"
            )
        return replace(self, **overrides)

    def for_phase(self, phase: str) -> Callable[..., Any]:
        if phase not in self.phases():
            raise UnknownPhaseError(
                f"no runner for phase {phase!r}. "
                f"Known phases: {sorted(self.phases())}"
            )
        return getattr(self, phase)


DEFAULT_FUNDAMENTALS_RUNNERS = FundamentalsRunners(
    industry=run_industry_refresh,
    shares=run_shares_refresh,
    financials=run_financials_refresh,
    market_cap=run_market_cap_backfill,
    index_build=run_industry_index_build,
    index_value=populate_industry_index_value_for_industry,
)
