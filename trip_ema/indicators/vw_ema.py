"""Volume-weighted triple-stage EMA, ported from the Pine indicator.

The Pine construction is:

    vw1 = ema(close * volume, base) / ema(volume, base)
    vw2 = ema(vw1, s1)
    polished = wma(2 * wma(vw2, floor(s2/2)) - wma(vw2, s2), floor(sqrt(s2)))
    result = ema(polished, final)  if final is not None else polished

Where `ema(x, n)` is the standard exponential moving average with
alpha = 2 / (n + 1), seeded from the first value.

This module exposes the EMA as a stateful class supporting O(1) updates
per bar after construction. The state carried between bars:
    - two parallel EMA accumulators for vw1's numerator and denominator
    - one EMA accumulator for vw2
    - a rolling window of the last `s2` vw2 values (for the two WMA stages)
    - an optional final EMA accumulator

A pure-function `compute_series` helper is provided for batch use and as
a reference implementation in tests.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from collections.abc import Iterable


def _ema_alpha(length: int) -> float:
    """Pine's `ta.ema` uses alpha = 2 / (length + 1)."""
    return 2.0 / (length + 1.0)


def _wma(values: list[float], length: int) -> float:
    """Weighted moving average over the *last* `length` values.

    Weights are 1, 2, ..., length (most recent gets the highest weight).
    Returns NaN if fewer than `length` values are available — caller must
    have provided enough warmup.
    """
    if len(values) < length:
        return float("nan")
    window = values[-length:]
    weight_sum = length * (length + 1) / 2.0
    weighted = sum((i + 1) * v for i, v in enumerate(window))
    return weighted / weight_sum


@dataclass
class VolumeWeightedTripleEMA:
    """Stateful, O(1)-per-update implementation of the Pine triple-stage EMA.

    Construct with the four length parameters. Feed each new bar by calling
    `update(close, volume)`; the return is the polished EMA value (or NaN
    during the initial warmup before enough samples are available).
    """

    base: int       # length of the inner volume-weighted EMA pair
    s1: int         # length of the second smoothing EMA
    s2: int         # length used by the WMA polish stage
    final: int | None = None  # optional final smoothing EMA
    volume_sma_length: int = 1
    """Length of the SMA applied to raw volume before it enters the VW-EMA.
    1 = no smoothing (use volume directly). 2 = Pine's
    Weekly_Triple_EMA convention (`ta.sma(volume, 2)`)."""

    # Internal carried state — initialized lazily on first update.
    _vw1_num: float | None = field(default=None, init=False)
    _vw1_den: float | None = field(default=None, init=False)
    _vw2: float | None = field(default=None, init=False)
    _vw2_window: deque[float] = field(default_factory=deque, init=False)
    _final_ema: float | None = field(default=None, init=False)
    _n_bars_seen: int = field(default=0, init=False)
    # Recent raw volumes for the smoothing SMA. Length is bounded by
    # `volume_sma_length` so the deque stays tiny.
    _volume_window: deque[float] = field(default_factory=deque, init=False)

    # Pre-computed alphas
    _alpha_base: float = field(init=False)
    _alpha_s1: float = field(init=False)
    _alpha_final: float | None = field(init=False)
    # Effective lengths used inside the WMA polish stage
    _wma_inner_len: int = field(init=False)  # floor(s2 / 2)
    _wma_outer_len: int = field(init=False)  # floor(sqrt(s2))

    def __post_init__(self) -> None:
        if self.base < 2 or self.s1 < 2 or self.s2 < 2:
            raise ValueError(
                f"EMA lengths must be >= 2 (got base={self.base}, "
                f"s1={self.s1}, s2={self.s2})"
            )
        if self.final is not None and self.final < 2:
            raise ValueError(f"Final EMA length must be >= 2 (got {self.final})")
        if self.volume_sma_length < 1:
            raise ValueError(
                f"volume_sma_length must be >= 1 (got {self.volume_sma_length})"
            )

        self._alpha_base = _ema_alpha(self.base)
        self._alpha_s1 = _ema_alpha(self.s1)
        self._alpha_final = _ema_alpha(self.final) if self.final is not None else None

        self._wma_inner_len = max(1, self.s2 // 2)
        self._wma_outer_len = max(1, int(math.floor(math.sqrt(self.s2))))

        # Window must hold enough vw2 values to compute the outer WMA on the
        # diff series. The diff series needs s2 vw2 values to produce one
        # diff value; the outer WMA then needs `_wma_outer_len` diff values.
        # So we hold `s2 + _wma_outer_len - 1` vw2 values at maximum.
        self._vw2_window = deque(maxlen=self.s2 + self._wma_outer_len - 1)

    def update(self, close: float, volume: float) -> float:
        """Feed one bar; return the polished EMA value (or NaN during warmup).

        O(s2) per call due to the WMA stages — but s2 is a small constant,
        so this is effectively O(1). The volume smoothing buffer is bounded
        by `volume_sma_length` (typically 2), so it adds a negligible
        constant.
        """
        self._n_bars_seen += 1

        # ---- Stage 0: volume smoothing ----
        # Append raw volume; keep the buffer bounded.
        #
        # B3 (Phase 9) Pine-fidelity: Pine's `ta.sma(volume, N)` returns
        # NaN until N samples are available. The Python port previously
        # computed sum/len from the first bar (so bar 1 SMA = volume[0],
        # bar 2 SMA = (volume[0]+volume[1])/2, etc.), which shifted the
        # entire EMA series one bar earlier than Pine. To match Pine
        # exactly, we return NaN until the volume window is full. The
        # EMA recurrence below sees its first non-NaN seed one bar later,
        # which aligns with Pine's behavior.
        self._volume_window.append(volume)
        while len(self._volume_window) > self.volume_sma_length:
            self._volume_window.popleft()
        if len(self._volume_window) < self.volume_sma_length:
            return float("nan")
        smoothed_volume = sum(self._volume_window) / len(self._volume_window)

        # ---- Stage 1: volume-weighted EMA pair → vw1 ----
        # vw1 = ema(close * smoothed_volume, base) / ema(smoothed_volume, base)
        cv = close * smoothed_volume
        if self._vw1_num is None:
            self._vw1_num = cv
            self._vw1_den = smoothed_volume
        else:
            # Snapshot to locals so mypy keeps the narrowing from
            # the `if self._vw1_num is None` check above. Attribute
            # narrowing doesn't survive across statements; locals do.
            # The class invariant is that _vw1_num and _vw1_den are
            # paired (both None or both set).
            prev_num = self._vw1_num
            prev_den = self._vw1_den
            assert prev_num is not None and prev_den is not None
            a = self._alpha_base
            self._vw1_num = a * cv + (1.0 - a) * prev_num
            self._vw1_den = a * smoothed_volume + (1.0 - a) * prev_den

        # Guard against zero-volume periods which would NaN out the chain
        if self._vw1_den == 0.0:
            return float("nan")
        vw1 = self._vw1_num / self._vw1_den

        # ---- Stage 2: smooth vw1 → vw2 ----
        if self._vw2 is None:
            self._vw2 = vw1
        else:
            a = self._alpha_s1
            self._vw2 = a * vw1 + (1.0 - a) * self._vw2

        # ---- Stage 3: WMA polish ----
        # polished = wma(2*wma(vw2, s2/2) - wma(vw2, s2), sqrt(s2))
        self._vw2_window.append(self._vw2)

        # The outer WMA needs `_wma_outer_len` consecutive diff values.
        # Each diff value needs s2 consecutive vw2 values. So we need at
        # least s2 + _wma_outer_len - 1 vw2 values to produce one polished
        # output (the same condition as the deque's maxlen).
        if len(self._vw2_window) < self.s2 + self._wma_outer_len - 1:
            return float("nan")

        # Build the diff series for the last `_wma_outer_len` positions.
        diffs: list[float] = []
        window_list = list(self._vw2_window)
        for offset in range(self._wma_outer_len):
            # The window at this offset:
            #   - inner WMA: last `_wma_inner_len` values ending at position
            #     len(window) - 1 - (_wma_outer_len - 1 - offset)
            #   - outer WMA: last `s2` values, same ending position
            end_idx = len(window_list) - (self._wma_outer_len - 1 - offset)
            inner_window = window_list[end_idx - self._wma_inner_len:end_idx]
            outer_window = window_list[end_idx - self.s2:end_idx]
            inner_wma = _wma(inner_window, self._wma_inner_len)
            outer_wma = _wma(outer_window, self.s2)
            diffs.append(2.0 * inner_wma - outer_wma)
        polished = _wma(diffs, self._wma_outer_len)

        # ---- Stage 4 (optional): final EMA ----
        if self._alpha_final is None:
            return polished
        if self._final_ema is None:
            self._final_ema = polished
        else:
            a = self._alpha_final
            self._final_ema = a * polished + (1.0 - a) * self._final_ema
        return self._final_ema

    # -------------------- Snapshot / restore -------------------- #
    #
    # Snapshots capture every bit of mutable carrying state as an
    # immutable tuple. Used by TickerEngine.ingest_provisional /
    # _rollback so the same provisional bar can be re-applied across
    # multiple compute cycles without permanently advancing the
    # EMA recurrence on top of itself.

    def _snapshot(self) -> tuple:
        """Capture all mutable carrying state. The deques are converted
        to tuples for true copy semantics."""
        return (
            self._vw1_num,
            self._vw1_den,
            self._vw2,
            tuple(self._vw2_window),
            self._final_ema,
            self._n_bars_seen,
            tuple(self._volume_window),
        )

    def _restore(self, snapshot: tuple) -> None:
        """Restore state from a snapshot taken by _snapshot()."""
        (self._vw1_num, self._vw1_den, self._vw2,
         window_tuple, self._final_ema, self._n_bars_seen,
         volume_tuple) = snapshot
        self._vw2_window.clear()
        self._vw2_window.extend(window_tuple)
        self._volume_window.clear()
        self._volume_window.extend(volume_tuple)


def compute_series(
    closes: Iterable[float],
    volumes: Iterable[float],
    base: int,
    s1: int,
    s2: int,
    final: int | None,
) -> list[float]:
    """Pure batch computation. Useful for tests and one-shot calls.

    Returns one EMA value per input bar (NaN during warmup).
    """
    ema = VolumeWeightedTripleEMA(base=base, s1=s1, s2=s2, final=final)
    out: list[float] = []
    closes_list = list(closes)
    volumes_list = list(volumes)
    if len(closes_list) != len(volumes_list):
        raise ValueError("closes and volumes must have equal length")
    for c, v in zip(closes_list, volumes_list, strict=True):
        out.append(ema.update(c, v))
    return out
