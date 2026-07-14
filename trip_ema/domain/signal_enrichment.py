"""Domain object for a per-signal enrichment feature-store row (§19)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SignalEnrichment:
    """One materialised signal's feature vector + (later) its label.

    `features` is the evolving Appendix-A vector (stored as JSONB).
    Natural key: (ticker_id, timeframe, signal_date, signal_type).
    The forward-return labels are the training target y and stay None
    until an offline labelling pass fills them.

    THE §20 BAND FIELDS ARE COLUMNS, NOT FEATURES. They are typed, indexable
    and joinable, because the ML work reads them alongside
    stock_indicator_enrichment and industry_indicator_enrichment. Nothing in
    here is also written into `features` — a number stored in two places is a
    number that will eventually disagree with itself.

    All six default to None, which is what a row written before spec v0.5
    looks like. That is a real distinction and not a formality: an all-False
    `bands` dict MEANS "the price was on the wrong side of the EMA when the
    signal fired", so defaulting old rows to False would make them
    indistinguishable from genuine, unusual signals. None means not computed.
    """

    ticker_id: int
    timeframe: str
    signal_date: date
    signal_type: str
    features: dict
    feature_spec_version: str
    industry_id: int | None = None

    # The 90 keys of signal_bands.band_keys(): 75 booleans + 15 signed
    # distances. The repo rejects a dict whose key set is not exactly that.
    bands: dict | None = None
    # {period: atr} for signal_bands.ATR_PERIODS. NaN is allowed here and
    # lands as NULL — the repo does that conversion.
    atrs: dict | None = None
    # Causal, per signal type (signal_sequence.SignalSequence).
    seq_in_week: int | None = None
    seq_in_2w: int | None = None
    # The close the bands were cut against.
    signal_price: float | None = None
    # "UP" | "DOWN" — the geometry the flags were computed with. Read the
    # flags THROUGH this: an EXIT_FROM_BUY carries SELL geometry.
    band_polarity: str | None = None
