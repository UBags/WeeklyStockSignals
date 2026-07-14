"""Route-collaborator registry.

The persistence collaborators a route may fire, as data. Same shape as
`MarketStages` (Phase 6), `BackfillStages` (Phase 8), and
`FundamentalsRunners` (Phase 9): a frozen dataclass of callables, a
`DEFAULT_GATEWAYS` bound to the real functions, and `.with_(**overrides)`
to derive a test variant.

Fields are FACTORY callables that take a live Connection (and, for
`tier_counts`, an exchange code) and produce the concrete repo or value.
A route calls `gateways.etl_status(conn)` — never `EtlStatusRepository(conn)`
directly. A test wires a fake by overriding `get_gateways` at the app
level; no more `monkeypatch.setattr(mod, "EtlStatusRepository", FakeRepo)`.

Home:
    The registry lives HERE, under transports/http, not persistence/. The
    `tier_counts` field returns an `EtlTierCounts` — a Pydantic model
    defined in `..schemas`. Placing the registry under persistence/ would
    force a persistence -> transports import, reversing the layering. The
    correct direction is transports -> persistence: transports depends on
    persistence, so the registry lives in transports and imports repos
    from persistence.

Phase 10 of the de-monkeypatching programme.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...etl.status_repo import EtlStatusRepository
from ...persistence.exchange_state_repo import ExchangeStateRepository
from .schemas import EtlTierCounts


def _tier_counts(conn: Connection, exchange: str) -> EtlTierCounts:
    """How many active tickers in each tier, scoped to one exchange.

    Moved here from `routes/admin_etl.py` as part of the Phase 10
    Gateways port. It was module-level in the route file solely so
    tests could `monkeypatch.setattr(mod, "_tier_counts", ...)` — a
    seam cut into production code by the test layer. Now it is a
    field of the registry, and tests derive a variant.
    """
    result = conn.execute(text("""
        SELECT tier, COUNT(*) AS n FROM tickers
        WHERE is_active = TRUE AND delisted_at IS NULL
          AND exchange = :ex
        GROUP BY tier
    """), {"ex": exchange})
    counts = {1: 0, 2: 0, 3: 0}
    for row in result:
        counts[row.tier] = row.n
    return EtlTierCounts(
        tier_1=counts.get(1, 0),
        tier_2=counts.get(2, 0),
        tier_3=counts.get(3, 0),
    )


class UnknownGatewayError(KeyError):
    """A `.with_(**overrides)` call named a field that does not exist.

    Raised at construction time of the derived registry — not at request
    time. A typo in a test setup should fail immediately, not fifteen
    minutes into a route the fake was supposed to intercept.
    """


@dataclass(frozen=True)
class Gateways:
    """The persistence collaborators the /v1/admin/etl/* and
    /v1/admin/exchange-state/* routes may fire.

    Frozen. Derive a variant with `.with_(...)`; the rest stay real.
    Field names are the seam names — the route calls
    `gateways.<field>(...)`.
    """

    etl_status: Callable[..., Any]
    exchange_state: Callable[..., Any]
    tier_counts: Callable[..., EtlTierCounts]

    @classmethod
    def fields_(cls) -> frozenset[str]:
        return frozenset(f.name for f in fields(cls))

    def with_(self, **overrides: Any) -> "Gateways":
        unknown = sorted(set(overrides) - self.fields_())
        if unknown:
            raise UnknownGatewayError(
                f"no such gateway field(s): {unknown}. "
                f"Known: {sorted(self.fields_())}"
            )
        return replace(self, **overrides)


DEFAULT_GATEWAYS = Gateways(
    etl_status=EtlStatusRepository,
    exchange_state=ExchangeStateRepository,
    tier_counts=_tier_counts,
)
