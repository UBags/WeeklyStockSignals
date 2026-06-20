"""Pure regime classifier.

One function (`compute_regime`) and the types it needs. No I/O, no DB,
no imports outside `domain/`. The single canonical implementation of
the regime rules.

Rules (per spec):
  (a) definite_green     = close >= upper_inner
  (b) definite_red       = close <= lower_inner
  (c) green_continuation = previous_bar_regime_is_green AND (
          close >= lower_envelope
          OR (close >= lower_inner AND prev_close >= prev_lower_envelope))
  (d) red_continuation   = previous_bar_regime_is_red AND (
          close <= upper_envelope
          OR (close <= upper_inner AND prev_close <= prev_upper_envelope))
  (e) neutral            = otherwise

Rules evaluated in priority order top-to-bottom; (a) and (b) win over
(c) / (d) on a tie.

The envelope values are the max / min of the three EMAs (med, long,
longest) — the upper / lower extents of the ribbon. This matches the
existing band-construction convention in `indicators/bands.py`.
"""

from trip_ema.regime.compute_regime import (
    BarInputs,
    Regime,
    compute_regime,
)

__all__ = ["BarInputs", "Regime", "compute_regime"]
