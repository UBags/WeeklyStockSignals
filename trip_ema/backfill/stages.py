"""The backfill pipeline's collaborators, as data — and one contract that has to
be enforced rather than commented.

Every runner and populator the pipeline calls was a module-level name, so a test
had only one way to stub them: rewrite `trip_ema.backfill.pipeline`'s attributes.
That is the same hole Phase 6 closed in the orchestrator, and `BackfillStages`
closes it the same way — the stages become an argument.

But there is a second thing here that the orchestrator did not have, and it is
the reason this phase exists at all.

    THE POPULATOR CANNOT CROSS THE PROCESS BOUNDARY.

Stage 2 fans out over SPAWNED child processes when
`config.compute.stock_state_processes > 1` (the production default is 3). Spawned
children re-import `pipeline.py` CLEAN. They never see the parent's patched
module attribute, and they will never see an injected `populate_stock_state_and_
enrichment` either — the parent's object simply does not exist in their memory.

So a test that stubbed the populator and left processes at the default would get
the REAL populator running against its fixture DB, in a child process, silently.
The test would pass. It would be testing something else entirely.

The codebase already knew this. It is written out, at length, in the docstring of
`_stage_stock_state_full` and again in the `_MockConfig` docstring of
tests/backfill/test_pipeline.py, which sets `stock_state_processes=1` and warns
that this "is NOT a workaround; it is a real constraint, and the reason must not
be lost."

A constraint whose only enforcement is a comment is a constraint that gets lost.
`BackfillStages.validate_for(config)` makes it an exception: supply a custom
stock-state populator with processes > 1 and the backfill refuses to start, at
the top, before it has written anything. The silent-wrong-answer failure becomes
a loud-and-immediate one.

Everything else in here is injectable with no such caveat: those stages run in
this process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


class ProcessBoundaryError(RuntimeError):
    """A collaborator was injected that cannot survive `spawn`.

    Raised up-front rather than letting the child processes quietly run the real
    thing. Not a ValueError: this is not a bad value, it is an impossible
    configuration.
    """


@dataclass(frozen=True)
class BackfillStages:
    """The runners and populators one `backfill()` call uses.

    Frozen. `.with_(...)` derives a variant; the rest stay real.
    """

    refresh_listings: Callable[..., Any]
    run_industry_refresh: Callable[..., Any]
    run_shares_refresh: Callable[..., Any]
    run_financials_refresh: Callable[..., Any]
    populate_stock_state_and_enrichment: Callable[..., Any]
    """Stage 2's per-ticker populator. THE ONE that cannot cross into spawned
    children — see validate_for()."""
    populate_industry_index_value_for_all: Callable[..., Any]
    industry_state: Callable[..., Any]
    industry_enrichment: Callable[..., Any]

    def with_(self, **overrides: Any) -> BackfillStages:
        return replace(self, **overrides)

    def validate_for(self, config: Any, default: BackfillStages) -> None:
        """Refuse a configuration whose injection would be silently discarded.

        `default` is DEFAULT_BACKFILL_STAGES; passed in rather than imported to
        keep this module free of a cycle back into pipeline.py.
        """
        processes = config.compute.stock_state_processes
        if processes <= 1:
            return
        if (
            self.populate_stock_state_and_enrichment
            is default.populate_stock_state_and_enrichment
        ):
            return
        raise ProcessBoundaryError(
            "a custom stock-state populator was injected, but "
            f"compute.stock_state_processes is {processes}. Stage 2 fans out to "
            "SPAWNED child processes, which re-import trip_ema.backfill.pipeline "
            "clean and would never see it — they would run the real populator "
            "instead, and this backfill would quietly do something other than "
            "what you asked. Set compute.stock_state_processes=1 to keep stage 2 "
            "in this process."
        )
