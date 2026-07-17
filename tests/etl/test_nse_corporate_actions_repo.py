"""Regression tests for NseCorporateActionsRepository (real DB)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text

from trip_ema.etl.nse_corporate_actions_repo import (
    NseActionRecord,
    NseCorporateActionsRepository,
)
from trip_ema.persistence import transaction


def test_upsert_tolerates_nan_in_raw_payload(db_engine):
    """Regression: the nselib feed uses NaN for missing numeric cells
    (e.g. "faceVal": NaN). json.dumps emits literal NaN which Postgres jsonb
    rejects (InvalidTextRepresentation), failing the whole refresh. The repo
    must map non-finite floats to null before the jsonb cast."""
    rec = NseActionRecord(
        isin="INE028L21018",
        symbol="STAN",
        ex_date=date(2010, 8, 12),
        action_type="dividend",
        subject="Interim Dividend",
        ticker_id=None,
        raw_payload={
            "comp": "Standard Chartered PLC",
            "faceVal": float("nan"),
            "nested": {"x": float("inf")},
            "list": [1.0, float("nan")],
            "ok": "value",
        },
    )
    with transaction(db_engine) as conn:
        inserted = NseCorporateActionsRepository(conn).upsert_many([rec])
    assert inserted == 1  # did not raise DataError

    with db_engine.connect() as conn:
        payload = conn.execute(text(
            "SELECT raw_payload FROM nse_corporate_actions WHERE isin = :i"
        ), {"i": "INE028L21018"}).scalar()
    assert payload["faceVal"] is None
    assert payload["nested"]["x"] is None
    assert payload["list"] == [1.0, None]
    assert payload["ok"] == "value"
