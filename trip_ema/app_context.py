"""The application context — what a command needs to do its job.

Every `cmd_*` handler in both CLIs used to open its own database engine:

    def cmd_backfill(args, config):
        db_engine = create_engine_from_config(config)

Which meant a test could only get a test engine in there by patching
`trip_ema.etl.cli.create_engine_from_config` — sixteen times in one file, once
per command, plus once more for each command a test wanted to reach. The seam
was a module global; the collaborator was invisible in the signature.

It also meant a `backfill` invocation opened an engine, used it, and left it to
the garbage collector — while a `cycle` invocation opened a second one. One
process, several pools, no disposal.

`AppContext` is built once, in `main()`, and threaded through. It is frozen: a
command receives its collaborators, it does not go shopping for them.

This is deliberately small. It holds what the commands actually share today.
Collaborators extracted in later phases (a clock, the data-source ports) belong
here too, and the reason to add them is that a command needs them — not that a
context object ought to have them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine

from .config import Config
from .fundamentals.stages import (
    DEFAULT_FUNDAMENTALS_RUNNERS,
    FundamentalsRunners,
)
from .persistence.connection import create_engine_from_config


@dataclass(frozen=True)
class AppContext:
    """Everything a CLI command is given. Constructed by `main()`."""

    config: Config
    """The validated config. Commands read it; they never reload it."""

    db_engine: Engine
    """ONE engine for the process, not one per command. Its pool is sized from
    `config.database.pool_*` and it is disposed in `main()`'s finally."""

    runners: FundamentalsRunners = field(
        default_factory=lambda: DEFAULT_FUNDAMENTALS_RUNNERS,
    )
    """The fundamentals runner registry — the seven callables a command may
    fire (industry / shares / shares_backfill / financials / market_cap /
    index_build / index_value). Default is the production wiring; tests
    derive a variant with `DEFAULT_FUNDAMENTALS_RUNNERS.with_(shares=spy)`.
    Phase 9 of the de-monkeypatching programme — matches MarketStages
    (Phase 6) and BackfillStages (Phase 8)."""

    @classmethod
    def from_config(cls, config: Config) -> AppContext:
        """The production wiring. Tests construct AppContext directly with a
        fixture engine — which is the whole point of this class existing."""
        return cls(config=config, db_engine=create_engine_from_config(config))

    def dispose(self) -> None:
        """Release the connection pool. Called from `main()`'s finally, so a
        command that raises still gives its connections back."""
        self.db_engine.dispose()
