"""Regression: NaN/Inf features persist as JSON null in signal_enrichment.

Feature vectors legitimately contain NaN (warm-up bars, no-pivot pdisp).
The repo coerces non-finite floats to None so the JSONB write succeeds.
"""
from datetime import date

from sqlalchemy import text

from trip_ema.domain.signal_enrichment import SignalEnrichment
from trip_ema.persistence import transaction
from trip_ema.persistence.signal_enrichment_repo import (
    SignalEnrichmentRepository,
)


def test_nan_and_inf_features_persist_as_null(db_engine):
    tid = 990002
    with transaction(db_engine) as conn:
        conn.execute(text(
            "INSERT INTO tickers (ticker_id, exchange, symbol, tier, "
            "is_active, delisted_at) VALUES (:t,'NSE','NANTEST',2,TRUE,"
            "NULL) ON CONFLICT (ticker_id) DO NOTHING"), {"t": tid})
    row = SignalEnrichment(
        ticker_id=tid, timeframe="1W", signal_date=date(2024, 1, 8),
        signal_type="BUY",
        features={"finite": 1.5, "not_a_number": float("nan"),
                  "pos_inf": float("inf"), "label": "UPTREND"},
        feature_spec_version="v0.3")
    with transaction(db_engine) as conn:
        SignalEnrichmentRepository(conn).upsert_one(row)

    with db_engine.connect() as conn:
        r = conn.execute(text(
            "SELECT features->>'finite' AS finite, "
            "jsonb_typeof(features->'not_a_number') AS nan_type, "
            "jsonb_typeof(features->'pos_inf') AS inf_type, "
            "features->>'label' AS label "
            "FROM signal_enrichment "
            "WHERE ticker_id=:t AND signal_date=:d AND signal_type='BUY'"),
            {"t": tid, "d": date(2024, 1, 8)}).one()
    assert r.finite == "1.5"
    assert r.nan_type == "null"      # NaN -> JSON null
    assert r.inf_type == "null"      # Inf -> JSON null
    assert r.label == "UPTREND"
