"""Offscreen widget test for the lead/lag signal-detail card (leadlag E1)."""
from __future__ import annotations

from trip_ema.gui.views.widgets.leadlag_card import LeadLagCard
from trip_ema.notifications.leadlag_render import Corroborator, LeadLagContext


def _ctx():
    return LeadLagContext(
        -2, 0.71, "LAGGARD", -1, 1,
        (Corroborator("Oil & Gas", -0.78, 4, -1, True, 1, 5),),
    )


def test_placeholder_when_no_context(qtbot):
    card = LeadLagCard()
    qtbot.addWidget(card)
    card.set_context(None, "Automobiles")
    assert "no lead/lag data" in card._label.text()
    card.set_context(_ctx(), None)
    assert "no lead/lag data" in card._label.text()


def test_renders_lines_from_context(qtbot):
    card = LeadLagCard()
    qtbot.addWidget(card)
    card.set_context(_ctx(), "Automobiles")
    txt = card._label.text()
    assert "usually lags Automobiles by 2 weeks at 71% correlation" in txt
    assert "Automobiles signalled SELL 1 week ago" in txt
    assert "Oil &amp; Gas" in txt          # escaped for RichText
    assert "Oil & Gas" not in txt


def test_empty_lines_show_placeholder(qtbot):
    card = LeadLagCard()
    qtbot.addWidget(card)
    # DECOUPLED stock + neutral industry -> renderer yields nothing.
    ctx = LeadLagContext(3, 0.1, "DECOUPLED", 0, None, ())
    card.set_context(ctx, "Automobiles")
    assert "no lead/lag data" in card._label.text()


def test_clear_resets_to_placeholder(qtbot):
    card = LeadLagCard()
    qtbot.addWidget(card)
    card.set_context(_ctx(), "Automobiles")
    assert "no lead/lag data" not in card._label.text()
    card.clear()
    assert "no lead/lag data" in card._label.text()
