"""Single source of truth for the GUI row/entry background color.

Every place that turns signal + regime state into a background color
MUST route through :meth:`DetermineBackgroundColor.determine_background_color`.
Do not re-implement the rule and do not scatter the hex literals.

The rule (verbatim from the operator spec):

    GREEN  if  current_signal == BUY
           or (current_regime == BULLISH and previous_signal == BUY)
    RED    if  current_signal == SELL
           or (current_regime == BEARISH and previous_signal == SELL)
    YELLOW if  current_signal == EXIT_FROM_BUY
           or (current_regime == NEUTRAL and previous_signal == EXIT_FROM_BUY)
    GRAY   if  current_signal == EXIT_FROM_SELL
           or (current_regime == NEUTRAL and previous_signal == EXIT_FROM_SELL)
    else   -> None  (no color)

Terminology:
    current_signal  -- the signal fired on the CURRENT weekly bar
                       (None if the regime did not change on this bar).
    current_regime  -- the CURRENT weekly bar's display_regime.
    previous_signal -- the signal fired on the PREVIOUS weekly bar
                       (None if no flip on that bar).

Self-contained: owns its own ChangeSignal/CoreRegime <-> DB conversions
(they are native enum operations, not a shared business rule) and its own
color constants. No dependency on DetermineSignal.

Note on ``is`` vs ``==``: ChangeSignal and CoreRegime are both IntEnum
and their integer values collide (ChangeSignal.BUY == 1 == CoreRegime.
BULLISH; SELL == -1 == BEARISH). Every comparison here uses ``is`` so a
cross-enum value can never silently satisfy a branch.
"""
from __future__ import annotations

from ..domain.state import ChangeSignal, CoreRegime


class DetermineBackgroundColor:
    """Namespaced static rule. Stateless -- no instance is required."""

    GREEN = "#c8e6c9"   # light green
    RED = "#ffcdd2"     # light red
    YELLOW = "#fff9c4"  # light yellow (was long, now flat)
    GRAY = "#e0e0e0"    # light gray   (was short, now flat)

    @staticmethod
    def determine_background_color(
        current_signal: ChangeSignal | None,
        current_regime: CoreRegime,
        previous_signal: ChangeSignal | None,
    ) -> str | None:
        S, R = ChangeSignal, CoreRegime
        if (current_signal is S.BUY
                or (current_regime is R.BULLISH and previous_signal is S.BUY)):
            return DetermineBackgroundColor.GREEN
        if (current_signal is S.SELL
                or (current_regime is R.BEARISH and previous_signal is S.SELL)):
            return DetermineBackgroundColor.RED
        if (current_signal is S.EXIT_FROM_BUY
                or (current_regime is R.NEUTRAL
                    and previous_signal is S.EXIT_FROM_BUY)):
            return DetermineBackgroundColor.YELLOW
        if (current_signal is S.EXIT_FROM_SELL
                or (current_regime is R.NEUTRAL
                    and previous_signal is S.EXIT_FROM_SELL)):
            return DetermineBackgroundColor.GRAY
        return None

    # -- self-contained DB conversions (native enum ops) --------------------

    @staticmethod
    def signal_from_db(s: str | None) -> ChangeSignal | None:
        """DB ``signal_type`` string -> ChangeSignal, or None for a
        NULL/absent signal. Unknown strings map to None (never raises)."""
        if s is None:
            return None
        try:
            return ChangeSignal[s]
        except KeyError:
            return None

    @staticmethod
    def regime_from_db(display_regime: int | None) -> CoreRegime | None:
        """``display_regime`` SMALLINT (-1/0/1) -> CoreRegime, or None if
        absent. Out-of-range values map to None (never raises)."""
        if display_regime is None:
            return None
        try:
            return CoreRegime(int(display_regime))
        except ValueError:
            return None
