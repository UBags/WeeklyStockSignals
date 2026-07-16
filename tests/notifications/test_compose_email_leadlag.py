"""Unit test for the lead/lag clause hook in build_email_body (leadlag D3).

DB-free: the reader is injected as a stub, so this runs in the normal suite.
"""
from datetime import datetime, timezone

from trip_ema.notifications.compose_email import (
    EnrichedSignalRow,
    build_email_body,
)
from trip_ema.notifications.leadlag_render import (
    Corroborator,
    LeadLagContext,
)

UTC = timezone.utc
_NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)


def _row(ticker_id=1, industry="Automobiles", symbol="TATAMOTORS"):
    return EnrichedSignalRow(
        ticker_id=ticker_id, symbol=symbol, exchange="NSE",
        industry=industry, signal_type="SELL",
        regime_before=1, regime_after=-1, price_at_signal=123.45,
        bar_timestamp=_NOW, computed_at=_NOW, rank_score=0.42,
    )


def _ctx():
    return LeadLagContext(
        -2, 0.71, "LAGGARD", -1, 1,
        (Corroborator("Oil & Gas", -0.78, 4, -1, True, 1, 5),),
    )


def test_clause_appended_when_reader_injected():
    body = build_email_body([_row()], "C1", _NOW, leadlag_fn=lambda r: _ctx())
    assert ("usually lags Automobiles by 2 weeks at 71% correlation"
            in body.plain_text)
    assert "Automobiles signalled SELL 1 week ago" in body.plain_text
    assert 'colspan="4"' in body.html
    assert "usually lags Automobiles" in body.html
    assert "Oil &amp; Gas" in body.html          # escaped in the clause


def test_no_clause_without_reader():
    body = build_email_body([_row()], "C1", _NOW)
    assert "usually" not in body.plain_text
    assert "colspan" not in body.html


def test_no_clause_for_unknown_industry():
    # Even if the reader returns a context, a None industry suppresses it.
    body = build_email_body(
        [_row(industry=None)], "C1", _NOW, leadlag_fn=lambda r: _ctx(),
    )
    assert "usually" not in body.plain_text
    assert "colspan" not in body.html
