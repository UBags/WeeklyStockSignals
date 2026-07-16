"""DB-backed test for the §6.1 lead/lag reader (leadlag D2)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.notifications.leadlag_reader import load_leadlag_context
from trip_ema.notifications.leadlag_render import render_leadlag_text
from trip_ema.persistence import transaction
from trip_ema.persistence.tickers_repo import TickersRepository

UTC = timezone.utc
IND = "D2 Test Auto"
OIL = "D2 Oil"
STEEL = "D2 Steel"
DECOY = "D2 Decoy"
BASE = date(2025, 1, 6)


def _seed_breadth(conn, industry: str, values: list[float]) -> None:
    for i, v in enumerate(values):
        conn.execute(
            text("""
                INSERT INTO industry_regime_breadth (
                    industry_name, asof_date, breadth
                ) VALUES (:ind, :d, :b)
                ON CONFLICT (industry_name, asof_date) DO UPDATE
                    SET breadth = EXCLUDED.breadth
            """),
            {"ind": industry, "d": BASE + timedelta(weeks=i), "b": float(v)},
        )


def _seed_ensemble(conn, member, variant, corr, best_lag, sign) -> None:
    conn.execute(
        text("""
            INSERT INTO influential_industry_ensemble (
                industry_i, member_j, variant, corr, best_lag,
                is_leader, sign, asof_date
            ) VALUES (:i, :j, :v, :c, :bl, :isl, :sg, :d)
        """),
        {"i": IND, "j": member, "v": variant, "c": corr, "bl": best_lag,
         "isl": best_lag < 0, "sg": sign, "d": BASE},
    )


def test_reader_assembles_full_context(db_engine):
    now = datetime.now(UTC).replace(microsecond=0)
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            TickerRef("NSE", "D2TATA", "EQ"), created_at=now,
        )

        # Two stock rows, SAME asof: window 52 (LEADER/+9) is a decoy; the
        # reader must pick window 260 (LAGGARD/-2) via the window tiebreak.
        for window, bl, corr, cls in (
            (52, 9, 0.40, "LEADER"),
            (260, -2, 0.71, "LAGGARD"),
        ):
            conn.execute(
                text("""
                    INSERT INTO sector_stock_leadlag (
                        ticker_id, asof_date, "window", industry_name,
                        best_lag, lead_lag_corr, coupling_class,
                        is_constituent, n_obs
                    ) VALUES (:tid, :d, :w, :ind, :bl, :c, :cls, FALSE, 260)
                """),
                {"tid": tid, "d": BASE, "w": window, "ind": IND,
                 "bl": bl, "c": corr, "cls": cls},
            )

        # Corroborators (mid), ordered by |corr|: Steel (0.84) before Oil.
        _seed_ensemble(conn, STEEL, "mid", -0.84, 6, -1)
        _seed_ensemble(conn, OIL, "mid", -0.78, 4, -1)
        # A 'floor' decoy with the largest |corr| — must be filtered out.
        _seed_ensemble(conn, DECOY, "floor", -0.99, 1, -1)

        _seed_breadth(conn, IND, [1, 1, 1, -1, -1])          # SELL, 1 wk ago
        _seed_breadth(conn, OIL, [-1, 1, 1, 1, 1, 1, 1])     # +, 5 wk ago
        _seed_breadth(conn, STEEL, [-1, 1, 1, 1, 1, 1, 1, 1, 1])  # +, 7 wk ago

    with transaction(db_engine) as conn:
        ctx = load_leadlag_context(conn, tid, IND)

    assert ctx.stock_coupling == "LAGGARD"
    assert ctx.stock_best_lag == -2
    assert abs(ctx.stock_corr - 0.71) < 1e-9
    assert ctx.industry_regime_now == -1
    assert ctx.industry_changed_weeks_ago == 1

    members = [c.industry for c in ctx.corroborators]
    assert members == [STEEL, OIL]            # ranked, floor decoy excluded
    assert ctx.corroborators[0].changed_weeks_ago == 7
    assert ctx.corroborators[1].changed_weeks_ago == 5

    class _Row:
        industry = IND
    txt = render_leadlag_text(_Row(), ctx)
    assert "usually lags D2 Test Auto by 2 weeks at 71% correlation" in txt
    assert "D2 Test Auto signalled SELL 1 week ago" in txt
    assert ("\u221284% correlation at 6-week lag with D2 Steel, "
            "which turned positive 7 weeks ago") in txt


def test_reader_degrades_when_tables_empty(db_engine):
    now = datetime.now(UTC).replace(microsecond=0)
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            TickerRef("NSE", "D2EMPTY", "EQ"), created_at=now,
        )
        ctx = load_leadlag_context(conn, tid, "No Such Industry")
    assert ctx.stock_coupling is None
    assert ctx.industry_regime_now == 0
    assert ctx.industry_changed_weeks_ago is None
    assert ctx.corroborators == ()

    class _Row:
        industry = "No Such Industry"
    assert render_leadlag_text(_Row(), ctx) == ""
