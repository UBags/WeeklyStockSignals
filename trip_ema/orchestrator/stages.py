"""The compute stages of a market recompute, as data.

`recompute_market` ran six stages by calling six module-level functions by name.
That made the module global the only seam, so a test that wanted to make one
stage explode — or to skip the five it did not care about — had to reach in and
rewrite the module's attributes:

    monkeypatch.setattr(rm_mod, "_stage_stock_state_incremental", lambda *a, **k: (10, 0, 0))
    monkeypatch.setattr(rm_mod, "_stage_stock_enrichment_incremental", ...)
    ... six of these, in every test

Twenty-seven of them in one file. And the seam was a lie in a specific way:
patching `rm_mod._stage_iiv_incremental` only works because `recompute_market`
looks the name up at call time, which is an implementation detail that could
change under the test at any moment.

`MarketStages` makes the six stages an argument. Production passes the default;
a test passes whichever subset it wants to be real. The sequencing, the
error-gating, and the progress emission stay exactly where they were — this
changes who supplies the stage functions, not what the orchestrator does with
them.

The dataclass deliberately imports nothing from the orchestrator: the default
instance is assembled in recompute_market.py, below the stage definitions,
because the other direction would be a cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

Counters = tuple[int, int, int]
"""(succeeded, failed, skipped) — what most stages report."""


@dataclass(frozen=True)
class MarketStages:
    """The six stage functions one `recompute_market` pass runs.

    Frozen, so a caller cannot mutate the shared default by accident. Use
    `.with_(iiv=...)` to derive a variant — that is the whole point of the type.
    """

    stock_state: Callable[..., Counters]
    stock_enrichment: Callable[..., Counters]
    rebuild_cache: Callable[..., bool]
    iiv: Callable[..., Counters]
    industry_state: Callable[..., Counters]
    industry_enrichment: Callable[..., Counters]

    def with_(self, **overrides: Any) -> MarketStages:
        """A copy with some stages swapped. The rest stay real.

        This is the shape most tests want: make ONE stage fail and check that
        the orchestrator halts, reports it, and emits the right progress — with
        the other five still doing their job (or, more often, replaced by
        cheap fakes, which is equally easy)."""
        return replace(self, **overrides)
