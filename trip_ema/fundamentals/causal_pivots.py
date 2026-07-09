"""Causal streaming pivot detector (spec v0.3 §5). Pure, no I/O.

Confirms a local peak/trough only `k` bars AFTER it forms (it needs the
full ±k window), so it never looks ahead: a pivot at bar `p` becomes
visible only at bar `p+k`. One instance per series. Tracks the two most
recent confirmed peaks and the two most recent confirmed troughs —
exactly what pattern_rules.structure_labels + the §8 distance features
need.

Rules:
  - strict extrema (center strictly above / below every neighbour in the
    ±k window) — no plateau double-counting, and a bar can't be both;
  - a fully-clean ±k window (no NaN anywhere) is required to confirm, so
    nothing fires during warm-up;
  - peaks and troughs are tracked independently (two peaks may occur
    without an intervening trough — that's fine for peak-to-peak HH/LH).

`snapshot()`/`restore()` mirror the engine resume pattern.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


def _isnan(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


@dataclass
class CausalPivotTracker:
    """Feed one `(bar_idx, value)` per bar via `update`; read the tracked
    peak/trough state after each call."""

    k: int = 2

    last_peak_value: float | None = None
    last_peak_bar: int | None = None
    prev_peak_value: float | None = None
    prev_peak_bar: int | None = None
    last_trough_value: float | None = None
    last_trough_bar: int | None = None
    prev_trough_value: float | None = None
    prev_trough_bar: int | None = None

    _buf: deque = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")
        self._buf = deque(maxlen=2 * self.k + 1)

    # -------------------- snapshot / restore -------------------- #

    def snapshot(self) -> tuple:
        return (
            self.k,
            self.last_peak_value, self.last_peak_bar,
            self.prev_peak_value, self.prev_peak_bar,
            self.last_trough_value, self.last_trough_bar,
            self.prev_trough_value, self.prev_trough_bar,
            tuple(self._buf),
        )

    def restore(self, s: tuple) -> None:
        (self.k,
         self.last_peak_value, self.last_peak_bar,
         self.prev_peak_value, self.prev_peak_bar,
         self.last_trough_value, self.last_trough_bar,
         self.prev_trough_value, self.prev_trough_bar,
         buf) = s
        self._buf = deque(buf, maxlen=2 * self.k + 1)

    # -------------------- per-bar advance -------------------- #

    def update(self, bar_idx: int, value: float | None) -> dict:
        """Advance one bar. Returns
        {peak_confirmed, trough_confirmed, pivot_bar} where pivot_bar is
        the bar of the just-confirmed pivot (= bar_idx - k) or None."""
        self._buf.append((bar_idx, value))
        peak_confirmed = trough_confirmed = False
        pivot_bar: int | None = None

        if len(self._buf) == 2 * self.k + 1:
            items = list(self._buf)
            c_bar, c_val = items[self.k]
            neighbours = [v for _, v in items[: self.k] + items[self.k + 1:]]
            window_clean = not _isnan(c_val) and not any(
                _isnan(v) for v in neighbours
            )
            if window_clean:
                if all(c_val > v for v in neighbours):
                    self.prev_peak_value = self.last_peak_value
                    self.prev_peak_bar = self.last_peak_bar
                    self.last_peak_value = c_val
                    self.last_peak_bar = c_bar
                    peak_confirmed = True
                    pivot_bar = c_bar
                elif all(c_val < v for v in neighbours):
                    self.prev_trough_value = self.last_trough_value
                    self.prev_trough_bar = self.last_trough_bar
                    self.last_trough_value = c_val
                    self.last_trough_bar = c_bar
                    trough_confirmed = True
                    pivot_bar = c_bar

        return {
            "peak_confirmed": peak_confirmed,
            "trough_confirmed": trough_confirmed,
            "pivot_bar": pivot_bar,
        }

    def nearest_pivot_bar(self) -> int | None:
        """Bar of the most recent confirmed pivot (peak or trough), or
        None if none confirmed yet."""
        bars = [b for b in (self.last_peak_bar, self.last_trough_bar)
                if b is not None]
        return max(bars) if bars else None
