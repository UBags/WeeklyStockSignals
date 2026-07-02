"""Single source of truth for the regime-transition -> signal rule.

Every place that derives a BUY / SELL / EXIT_FROM_BUY / EXIT_FROM_SELL
from a ``(regime_before, regime_after)`` pair MUST route through
:meth:`DetermineSignal.determine_signal`. Do not re-implement the rule.

The rule, with the reversal tie-breaks:

    any -> BULLISH (+1)       -> BUY             (BUY wins over
                                                  EXIT_FROM_SELL on a
                                                  BEARISH->BULLISH reversal)
    any -> BEARISH (-1)       -> SELL            (SELL wins over
                                                  EXIT_FROM_BUY on a
                                                  BULLISH->BEARISH reversal)
    BULLISH -> NEUTRAL        -> EXIT_FROM_BUY
    BEARISH -> NEUTRAL        -> EXIT_FROM_SELL
    equal, or anything else   -> NO_CHANGE

``determine_signal`` is TOTAL: any input returns a ``ChangeSignal`` and
it never raises. ``NO_CHANGE`` is the sink for equal regimes and for any
input that matches none of the four actionable transitions.
"""
from __future__ import annotations

from .state import ChangeSignal, CoreRegime


class DetermineSignal:
    """Namespaced static rule. Stateless -- no instance is required.

    Also owns the ``ChangeSignal`` <-> DB ``signal_type`` string
    conversion so callers never hand-roll it. The four actionable
    members' ``.name`` IS the DB string.
    """

    @staticmethod
    def determine_signal(
        regime_before: CoreRegime | int,
        regime_after: CoreRegime | int,
    ) -> ChangeSignal:
        b, a = int(regime_before), int(regime_after)
        if a == b:
            return ChangeSignal.NO_CHANGE
        if a == 1:
            return ChangeSignal.BUY
        if a == -1:
            return ChangeSignal.SELL
        if b == 1:
            return ChangeSignal.EXIT_FROM_BUY
        if b == -1:
            return ChangeSignal.EXIT_FROM_SELL
        return ChangeSignal.NO_CHANGE

    @staticmethod
    def to_db_str(signal: ChangeSignal) -> str | None:
        """``ChangeSignal`` -> DB ``signal_type`` string.

        Returns ``None`` for ``NO_CHANGE`` (which has no ``signal_type``
        and is never persisted). Never raises.
        """
        if signal is ChangeSignal.NO_CHANGE:
            return None
        return signal.name

    @staticmethod
    def from_db_str(s: str) -> ChangeSignal:
        """DB ``signal_type`` string -> ``ChangeSignal``.

        Unknown strings map to ``NO_CHANGE`` (total, never raises).
        """
        try:
            return ChangeSignal[s]
        except KeyError:
            return ChangeSignal.NO_CHANGE
