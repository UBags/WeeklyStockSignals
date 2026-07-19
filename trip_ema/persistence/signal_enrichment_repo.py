"""Persistence for signal_enrichment (per-signal ML feature store, §19/§20).

Mirrors the stock_indicator_state_repo pattern: explicit-column
INSERT ... ON CONFLICT (natural key) DO UPDATE. `features` is bound as a
json.dumps string and CAST to JSONB in SQL (the codebase's JSONB write
convention). Idempotent on (ticker_id, timeframe, signal_date,
signal_type).

§20 added 99 real columns beside the jsonb: 75 band booleans, 15 signed
distances, 5 ATRs, 2 sequence numbers, the signal price and the polarity.
Their names are DERIVED here from signal_bands.band_keys() rather than
retyped, so this file cannot drift from the computer that produces them.
Migration 0052 spells the same names out literally (a migration must not
import live code), and two tests hold the copies together: 0052's own
code-vs-migration test, and test_signal_enrichment_bands.py's
code-vs-information_schema test.

NaN NEVER REACHES A FLOAT8 COLUMN. Postgres accepts it; everything
downstream does not. One NaN makes AVG(dist_med_atr20) NaN for the whole
column, silently excludes the row from `WHERE dist > 0`, and poisons any
model that reads it. `_num()` maps every non-finite float to NULL at this
boundary — the same job `_json_safe()` already does for the jsonb.
"""
from __future__ import annotations

import json
import math

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.signal_enrichment import SignalEnrichment
from ..fundamentals.signal_bands import ATR_PERIODS, band_keys


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN/Inf) with None so the
    result is valid JSON for a JSONB column (Postgres rejects NaN)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj

def _num(x) -> float | None:
    """Every non-finite float -> NULL. See the module docstring: this is the
    single line standing between a warm-up NaN and a poisoned column."""
    if x is None:
        return None
    f = float(x)
    return f if math.isfinite(f) else None


# The §20 columns, in one place, derived from the one place that names them.
_BAND_KEYS: tuple[str, ...] = band_keys()                    # 75 + 15
_ATR_COLS: tuple[str, ...] = tuple(f"atr{n}" for n in ATR_PERIODS)   # 5
_SCALAR_COLS: tuple[str, ...] = _ATR_COLS + (
    "seq_in_week", "seq_in_consec_weeks", "signal_price", "band_polarity",
)
_BAND_COLS: tuple[str, ...] = _BAND_KEYS + _SCALAR_COLS      # 99

# The columns whose NULL-ness decides data_is_valid (strict literal, by
# request): every band boolean, every distance, every ATR. Deliberately the
# 90 band keys + 5 ATRs — NOT signal_price / seq / polarity, which are always
# present on a real signal and whose absence means "broken", handled below.
_VALIDITY_COLS: tuple[str, ...] = _BAND_KEYS + _ATR_COLS     # 95

# Written by the walk, not derived from bands. day_of_week is NOT NULL in the
# schema, so it must always be supplied.
_EXTRA_COLS: tuple[str, ...] = (
    "bars_available", "day_of_week", "data_is_valid",
)
_ALL_NEW_COLS: tuple[str, ...] = _BAND_COLS + _EXTRA_COLS    # 102

# Bind parameter names are the column names — they are already valid
# identifiers and cannot collide with the four short names below.
_BASE_COLS = (
    "ticker_id", "timeframe", "signal_date", "signal_type",
    "industry_id", "features", "feature_spec_version", "computed_at",
)
_BASE_VALUES = ":tid, :tf, :sd, :st, :ind, CAST(:feat AS JSONB), :ver, NOW()"

_COLUMNS = ", ".join(_BASE_COLS + _ALL_NEW_COLS)
_VALUES = ", ".join([_BASE_VALUES] + [f":{c}" for c in _ALL_NEW_COLS])
_UPDATES = ",\n        ".join(
    [
        "industry_id = EXCLUDED.industry_id",
        "features = EXCLUDED.features",
        "feature_spec_version = EXCLUDED.feature_spec_version",
        "computed_at = NOW()",
    ]
    + [f"{c} = EXCLUDED.{c}" for c in _ALL_NEW_COLS]
)
_ON_CONFLICT = f"""
    ON CONFLICT (ticker_id, timeframe, signal_date, signal_type)
    DO UPDATE SET
        {_UPDATES}
"""
_INSERT = (
    f"INSERT INTO signal_enrichment ({_COLUMNS}) "
    f"VALUES ({_VALUES}) {_ON_CONFLICT}"
)
_SELECT = (
    "SELECT ticker_id, timeframe, signal_date, signal_type, "
    "industry_id, features, feature_spec_version, "
    + ", ".join(_ALL_NEW_COLS)
    + " FROM signal_enrichment "
    "WHERE ticker_id = :tid AND timeframe = :tf "
    "AND signal_date = :sd AND signal_type = :st"
)


class SignalEnrichmentRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_one(self, row: SignalEnrichment) -> None:
        self._conn.execute(text(_INSERT), self._params(row))

    def upsert_many(self, rows: list[SignalEnrichment]) -> int:
        if not rows:
            return 0
        self._conn.execute(text(_INSERT), [self._params(r) for r in rows])
        return len(rows)

    def get_for_signal(
        self, ticker_id: int, timeframe: str, signal_date, signal_type: str,
    ) -> SignalEnrichment | None:
        row = self._conn.execute(
            text(_SELECT),
            {"tid": ticker_id, "tf": timeframe, "sd": signal_date,
             "st": signal_type},
        ).fetchone()
        if row is None:
            return None
        feats = row.features
        if isinstance(feats, str):
            feats = json.loads(feats)
        m = row._mapping

        # A row written before spec v0.5 has every band column NULL. That is
        # NOT the same as a computed all-False row (which means "price was on
        # the wrong side of the EMA"), so it comes back as bands=None rather
        # than a dict of Nones.
        bands = (
            None if all(m[k] is None for k in _BAND_KEYS)
            else {k: m[k] for k in _BAND_KEYS}
        )
        atrs = (
            None if all(m[c] is None for c in _ATR_COLS)
            else {n: m[f"atr{n}"] for n in ATR_PERIODS}
        )
        return SignalEnrichment(
            ticker_id=row.ticker_id, timeframe=row.timeframe,
            signal_date=row.signal_date, signal_type=row.signal_type,
            features=feats, feature_spec_version=row.feature_spec_version,
            industry_id=row.industry_id,
            bands=bands, atrs=atrs,
            seq_in_week=m["seq_in_week"], seq_in_consec_weeks=m["seq_in_consec_weeks"],
            signal_price=m["signal_price"],
            band_polarity=m["band_polarity"],
            bars_available=m["bars_available"],
            day_of_week=m["day_of_week"],
            data_is_valid=m["data_is_valid"],
        )

    def count(self) -> int:
        return int(self._conn.execute(
            text("SELECT COUNT(*) FROM signal_enrichment")
        ).scalar())

    @staticmethod
    def _params(row: SignalEnrichment) -> dict:
        p = {
            "tid": row.ticker_id, "tf": row.timeframe,
            "sd": row.signal_date, "st": row.signal_type,
            "ind": row.industry_id,
            "feat": json.dumps(_json_safe(row.features)),
            "ver": row.feature_spec_version,
        }

        bands = row.bands
        if bands is not None:
            keys = set(bands)
            if keys != set(_BAND_KEYS):
                missing = sorted(set(_BAND_KEYS) - keys)
                extra = sorted(keys - set(_BAND_KEYS))
                raise ValueError(
                    "SignalEnrichment.bands does not match "
                    "signal_bands.band_keys(). This is schema drift, not a "
                    "typo to paper over: "
                    f"missing={missing[:5]}{'...' if len(missing) > 5 else ''} "
                    f"extra={extra[:5]}{'...' if len(extra) > 5 else ''}"
                )

        for k in _BAND_KEYS:
            v = None if bands is None else bands[k]
            # booleans pass through; distances go through the NaN gate.
            p[k] = v if (v is None or isinstance(v, bool)) else _num(v)

        atrs = row.atrs or {}
        for n in ATR_PERIODS:
            p[f"atr{n}"] = _num(atrs.get(n))

        p["seq_in_week"] = row.seq_in_week
        p["seq_in_consec_weeks"] = row.seq_in_consec_weeks
        p["signal_price"] = _num(row.signal_price)
        p["band_polarity"] = row.band_polarity

        p["bars_available"] = row.bars_available
        # day_of_week is NOT NULL in the schema and can never be None here:
        # SignalEnrichment.__post_init__ derives it from signal_date for any
        # caller that did not set it. The domain object owns that invariant.
        p["day_of_week"] = row.day_of_week

        # STRICT LITERAL data_is_valid, computed HERE because this is the one
        # place the final NULL-mapped values exist: _num() has already turned
        # every warm-up NaN into None above. True iff every validity column is
        # non-None. The row.data_is_valid a caller passed in is ignored.
        p["data_is_valid"] = all(p[c] is not None for c in _VALIDITY_COLS)
        return p
