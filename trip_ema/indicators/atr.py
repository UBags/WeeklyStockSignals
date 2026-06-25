"""Average True Range, ported from Pine's `ta.atr`.

Pine uses Wilder's smoothing: alpha = 1 / length (NOT the standard EMA's
alpha = 2 / (length + 1)). The seed is the simple average of the first
`length` true-range values.

True range for a bar:
    tr = max(high - low,
             abs(high - prev_close),
             abs(low  - prev_close))

ATR recurrence (Wilder):
    atr[i] = ((length - 1) * atr[i-1] + tr[i]) / length
           = (1 - alpha) * atr[i-1] + alpha * tr[i]  with alpha = 1/length

For the very first bar, true range is `high - low` (no prev_close).

This module exposes a stateful `ATR` class for O(1) updates and a pure
`compute_series` helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable

from ..domain import OHLCVBar


@dataclass
class ATR:
    """Stateful Wilder ATR. O(1) per update after warmup."""

    length: int

    _prev_close: float | None = field(default=None, init=False)
    _atr: float | None = field(default=None, init=False)
    _tr_buffer: list[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.length < 1:
            raise ValueError(f"ATR length must be >= 1 (got {self.length})")

    def update(self, high: float, low: float, close: float) -> float:
        """Feed one bar; return the ATR value (or NaN during warmup)."""
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close

        if self._atr is None:
            # Warming up — accumulate TR values until we have `length` of them.
            self._tr_buffer.append(tr)
            if len(self._tr_buffer) < self.length:
                return float("nan")
            # Seed: simple average of the first `length` TR values
            self._atr = sum(self._tr_buffer) / self.length
            self._tr_buffer = []  # release memory; not needed again
            return self._atr

        # Steady state — Wilder smoothing
        self._atr = ((self.length - 1) * self._atr + tr) / self.length
        return self._atr

    # -------------------- Snapshot / restore -------------------- #

    def _snapshot(self) -> tuple:
        """Capture all mutable state. The TR buffer is tuple-copied."""
        return (
            self._prev_close,
            self._atr,
            tuple(self._tr_buffer),
        )

    def _restore(self, snapshot: tuple) -> None:
        prev_close, atr_val, tr_tuple = snapshot
        self._prev_close = prev_close
        self._atr = atr_val
        self._tr_buffer = list(tr_tuple)


def compute_series(bars: Iterable[OHLCVBar], length: int) -> list[float]:
    """Pure batch ATR. Returns one value per input bar (NaN during warmup)."""
    atr = ATR(length=length)
    return [atr.update(b.high, b.low, b.close) for b in bars]
