"""Unit tests for the pure lead/lag renderer (leadlag D1)."""
from dataclasses import dataclass

from trip_ema.notifications.leadlag_render import (
    Corroborator,
    LeadLagContext,
    changed_weeks_ago,
    regime_sign,
    render_leadlag_html,
    render_leadlag_lines,
    render_leadlag_text,
)

MINUS = "\u2212"


@dataclass
class Row:
    industry: "str | None"
    symbol: str = "TATAMOTORS"


def test_leader_reads_leads():
    ctx = LeadLagContext(2, 0.71, "LEADER", 1, 3, ())
    lines = render_leadlag_lines(Row("Automobiles"), ctx)
    assert lines[0] == "usually leads Automobiles by 2 weeks at 71% correlation"
    assert lines[1] == "Automobiles signalled BUY 3 weeks ago"


def test_laggard_reads_lags_and_sell():
    # The invert guard: negative best_lag must read "lags" + SELL.
    ctx = LeadLagContext(-2, 0.71, "LAGGARD", -1, 1, ())
    lines = render_leadlag_lines(Row("Automobiles"), ctx)
    assert lines[0] == "usually lags Automobiles by 2 weeks at 71% correlation"
    assert lines[1] == "Automobiles signalled SELL 1 week ago"


def test_decoupled_and_missing_stock_suppressed():
    ctx = LeadLagContext(3, 0.1, "DECOUPLED", 1, 2, ())
    assert all("usually" not in x
               for x in render_leadlag_lines(Row("Auto"), ctx))
    ctx2 = LeadLagContext(None, None, None, -1, 0, ())
    assert render_leadlag_lines(Row("Steel"), ctx2) == [
        "Steel signalled SELL this week",
    ]


def test_corroborators_capped_at_three_with_signs():
    cs = (
        Corroborator("Oil & Gas", -0.78, 4, -1, True, 1, 5),
        Corroborator("Steel", -0.84, 6, -1, True, 1, 7),
        Corroborator("Cement", 0.40, 1, 1, False, -1, 2),
        Corroborator("Extra", 0.90, 1, 1, False, 1, 1),
    )
    ctx = LeadLagContext(-1, 0.6, "LAGGARD", -1, 1, cs)
    line = render_leadlag_lines(Row("Automobiles"), ctx)[1]
    assert "Direction bolstered by " in line
    assert (MINUS + "78% correlation at 4-week lag with Oil & Gas, "
            "which turned positive 5 weeks ago") in line
    assert "Cement" in line
    assert "Extra" not in line          # 4th dropped by the top-3 cap


def test_neutral_industry_line_suppressed():
    ctx = LeadLagContext(2, 0.50, "LEADER", 0, None, ())
    lines = render_leadlag_lines(Row("Automobiles"), ctx)
    assert lines == ["usually leads Automobiles by 2 weeks at 50% correlation"]


def test_changed_weeks_ago_derivation():
    assert changed_weeks_ago([1, 1, -1, -1]) == 1
    assert changed_weeks_ago([-1, -1, -1]) is None    # never changed
    assert changed_weeks_ago([1, -1]) == 0
    assert changed_weeks_ago([]) is None
    assert regime_sign(0.3) == 1 and regime_sign(-0.3) == -1
    assert regime_sign(0.0) == 0


def test_text_and_html_empty_on_no_context():
    assert render_leadlag_text(Row(None), None) == ""
    assert render_leadlag_html(Row("X"), None) == ""


def test_html_escapes_and_wraps():
    cs = (Corroborator("Oil & Gas", -0.78, 4, -1, True, 1, 5),)
    ctx = LeadLagContext(2, 0.71, "LEADER", 1, 2, cs)
    html = render_leadlag_html(Row("Automobiles"), ctx)
    assert html.startswith("<div")
    assert "Oil &amp; Gas" in html            # escaped
    assert "Oil & Gas" not in html
    txt = render_leadlag_text(Row("Automobiles"), ctx)
    assert txt.startswith("  [usually leads Automobiles")
