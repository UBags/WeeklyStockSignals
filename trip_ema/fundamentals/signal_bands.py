"""Signal-bar band features (spec §20). Pure — no DB, no I/O.

At a signal bar, where does the price sit relative to each EMA, measured in
ATR slabs? For each EMA E in {med, long, longest} and each ATR period N in
{20, 30, 40}, the four levels

    E1 = E ± 0.25·ATR_N   E2 = E ± 0.50·ATR_N
    E3 = E ± 0.75·ATR_N   E4 = E ± 1.00·ATR_N

cut the half-line above (BUY) or below (SELL) E into five slabs. Exactly one
of the five booleans is true when the price is on the signal's side of E;
ALL FIVE ARE FALSE when it is on the wrong side, which is a real state and
not an error — a BUY can fire with price under the med EMA.

That all-false hole is why `dist_<ema>_atr<n>` exists beside the flags: a
signed, polarity-adjusted (price − E)/ATR_N. Positive means "in the signal's
favour"; negative means the price is behind the EMA the signal crossed. The
45 booleans are re-derivable from the 9 distances; the distances are not
re-derivable from the booleans. Store both, trust the distances.

POLARITY, and the whole point of it (spec §20.3): an EXIT_FROM_BUY is a
downward event and is scored with SELL geometry; an EXIT_FROM_SELL is scored
with BUY geometry. So two rows on the same bar, same price, same EMAs, can
and will carry different flags. Feature dicts are therefore PER SIGNAL TYPE,
never shared across the types that fired on one day.

Boundaries are half-open and lower-inclusive: slab k is [ (k-1)·0.25 , k·0.25 )
of an ATR, and slab 5 is [ 1.00 , ∞ ). A price exactly on E4 is slab 5, not
slab 4.
"""
from __future__ import annotations

import math

NAN = float("nan")

# The four multipliers of ATR that cut the slabs (spec §20.1).
BAND_FRACTIONS: tuple[float, ...] = (0.25, 0.50, 0.75, 1.00)

# The three EMAs, keyed as they are named in the feature dict.
EMAS: tuple[str, ...] = ("med", "long", "longest")

# The five ATR periods this module scores against. NOT atr28 — that one is
# the structure engine's internal normaliser and stays where it is.
ATR_PERIODS: tuple[int, ...] = (10, 15, 20, 30, 40)

N_SLABS = len(BAND_FRACTIONS) + 1          # 4 cuts -> 5 slabs

POLARITY_UP = "UP"
POLARITY_DOWN = "DOWN"

# BUY and EXIT_FROM_SELL are upward events; SELL and EXIT_FROM_BUY downward.
_POLARITY_BY_SIGNAL: dict[str, str] = {
    "BUY": POLARITY_UP,
    "EXIT_FROM_SELL": POLARITY_UP,
    "SELL": POLARITY_DOWN,
    "EXIT_FROM_BUY": POLARITY_DOWN,
}


def polarity_for(signal_type: str) -> str:
    """UP or DOWN for a signal type. Raises on anything else — a typo'd
    signal type must not silently score as BUY geometry."""
    try:
        return _POLARITY_BY_SIGNAL[signal_type]
    except KeyError:
        raise ValueError(
            f"unknown signal_type {signal_type!r}; expected one of "
            f"{sorted(_POLARITY_BY_SIGNAL)}"
        ) from None


def _bad(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def band_key(ema: str, period: int, slab: int) -> str:
    """slab is 1..5; 5 is the open-ended one."""
    return f"band_{ema}_atr{period}_{slab}"


def dist_key(ema: str, period: int) -> str:
    return f"dist_{ema}_atr{period}"


def band_keys() -> tuple[str, ...]:
    """Every key band_flags() emits, in a stable order. The contract test
    asserts the shape of a real feature dict against this, so a typo in a
    key name fails at pytest rather than in a jsonb column nobody reads for
    six months."""
    out: list[str] = []
    for ema in EMAS:
        for n in ATR_PERIODS:
            for k in range(1, N_SLABS + 1):
                out.append(band_key(ema, n, k))
    for ema in EMAS:
        for n in ATR_PERIODS:
            out.append(dist_key(ema, n))
    return tuple(out)


def _slab_of(d: float) -> int | None:
    """Which slab a polarity-adjusted ATR-distance falls in, or None when
    the price is on the wrong side of the EMA (d < 0) or unknowable."""
    if _bad(d) or d < 0.0:
        return None
    for i, f in enumerate(BAND_FRACTIONS):
        if d < f:
            return i + 1                    # [0,.25) -> 1, [.25,.5) -> 2 ...
    return N_SLABS                          # >= 1.00


def band_flags(
    *,
    price: float | None,
    med_ema: float | None,
    long_ema: float | None,
    longest_ema: float | None,
    atr_by_period: dict[int, float | None],
    polarity: str,
) -> dict:
    """The 75 booleans + 15 signed distances for one signal.

    `polarity` comes from polarity_for(signal_type) — pass it explicitly
    rather than the signal type, so the caller has to have looked at the
    EXIT mapping rather than assuming it.

    Two OUTCOMES that must never be confused (spec §20.4):

      * UNCOMPUTABLE pair — missing/NaN price, EMA, or ATR, or a non-positive
        ATR. The distance is NaN and all five booleans are None. The repo
        writes None as SQL NULL, so the column reads NULL = "not computed".

      * COMPUTED, price behind the EMA — a real d < 0. The distance is a real
        negative number and all five booleans are False. That is an
        informative state (a BUY that fired with price under the med EMA),
        not an absence, and it must survive as False rather than collapse
        into the NULL above.

    The degradation is per (ema, period): one missing ATR nulls fifteen
    booleans, not the row. A zero ATR is not a divide-by-zero; it is a bar
    with no range, and the honest answer is "unknown" -> None.

    The repo maps every non-finite distance to SQL NULL. It has to: a NaN in
    a double precision column is legal Postgres and silently poisons AVG,
    every `> 0`, and anything downstream that trains on it.
    """
    if polarity not in (POLARITY_UP, POLARITY_DOWN):
        raise ValueError(f"polarity must be UP or DOWN, got {polarity!r}")
    sign = 1.0 if polarity == POLARITY_UP else -1.0

    emas: dict[str, float | None] = {
        "med": med_ema, "long": long_ema, "longest": longest_ema,
    }

    out: dict = {}
    for ema in EMAS:
        e = emas[ema]
        for n in ATR_PERIODS:
            atr = atr_by_period.get(n)
            # UNCOMPUTABLE: NaN distance, None flags -> SQL NULL. This is the
            # one place "we don't know" is separated from "we computed a
            # below-EMA price". Do not fold the two back together.
            if _bad(price) or _bad(e) or _bad(atr) or atr <= 0.0:
                out[dist_key(ema, n)] = NAN
                for k in range(1, N_SLABS + 1):
                    out[band_key(ema, n, k)] = None
                continue
            # COMPUTED: real distance, real booleans. A below-EMA price gives
            # d < 0, slab None, and five honest Falses.
            d = sign * (price - e) / atr
            out[dist_key(ema, n)] = d
            slab = _slab_of(d)
            for k in range(1, N_SLABS + 1):
                out[band_key(ema, n, k)] = (slab == k)
    return out
