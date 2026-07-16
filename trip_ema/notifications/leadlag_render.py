"""Pure lead/lag context + email clause renderer (design §3, §6.2).

No DB, no email machinery — takes a LeadLagContext (built by the D2 reader) and
a signal row, returns the clause as text or HTML. Kept dependency-free so the
compose_email hook (D3) can import it without a cycle.

SIGN CONVENTIONS (design §3 — a misread inverts the sentence):
  * sector_stock_leadlag.best_lag > 0  => coupling LEADER => stock LEADS its
    industry; best_lag < 0 => LAGGARD => stock LAGS its industry. The clause is
    phrased from the SIGNAL's subject (the stock).
  * A corroborator's `sign` is the direction of the relationship (+1/-1); its
    `best_lag` magnitude is the lead/lag offset in weeks; `regime_now` is that
    industry's current breadth sign, used for "turned positive/negative".
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Corroborator:
    industry: str
    corr: float
    best_lag: int
    sign: int                       # +1/-1 direction of the relationship
    is_leader: bool
    regime_now: int                 # -1/0/+1 current breadth sign
    changed_weeks_ago: int | None


@dataclass(frozen=True, slots=True)
class LeadLagContext:
    # stock vs its own industry
    stock_best_lag: int | None
    stock_corr: float | None
    stock_coupling: str | None      # LEADER / LAGGARD / COUPLED / DECOUPLED
    # the industry's own current regime
    industry_regime_now: int        # -1/0/+1 (sign of latest breadth)
    industry_changed_weeks_ago: int | None
    corroborators: tuple[Corroborator, ...] = ()


# ---------------- pure helpers ----------------

def regime_sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def changed_weeks_ago(signs: list[int]) -> int | None:
    """Weeks since the regime last changed sign, from an ascending series
    (latest last). Returns None if the sign never changes within the window
    (no known change point — we don't fabricate one)."""
    if not signs:
        return None
    cur = signs[-1]
    k = 0
    for s in reversed(signs):
        if s == cur:
            k += 1
        else:
            break
    if k == len(signs):
        return None
    return k - 1


def _pct(c: float) -> int:
    return round(abs(c) * 100)


def _wk(n: int) -> str:
    return "week" if abs(n) == 1 else "weeks"


def _ago(n: int) -> str:
    return "this week" if n == 0 else f"{n} {_wk(n)} ago"


def _dir_word(sign: int) -> str:
    return "BUY" if sign > 0 else "SELL"


def _turned(sign: int) -> str:
    return "positive" if sign > 0 else "negative"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------- phrase builders ----------------

def _stock_phrase(industry: str, ctx: LeadLagContext) -> str | None:
    if ctx.stock_coupling not in ("LEADER", "LAGGARD"):
        return None
    if ctx.stock_best_lag is None or ctx.stock_corr is None:
        return None
    verb = "leads" if ctx.stock_coupling == "LEADER" else "lags"
    n = abs(ctx.stock_best_lag)
    return (f"usually {verb} {industry} by {n} {_wk(n)} "
            f"at {_pct(ctx.stock_corr)}% correlation")


def _corro_phrase(c: Corroborator) -> str:
    sign_str = "+" if c.sign > 0 else "\u2212"
    lag = abs(c.best_lag)
    base = (f"{sign_str}{_pct(c.corr)}% correlation at {lag}-week lag "
            f"with {c.industry}")
    if c.regime_now != 0 and c.changed_weeks_ago is not None:
        base += f", which turned {_turned(c.regime_now)} {_ago(c.changed_weeks_ago)}"
    return base


def _industry_phrase(industry: str, ctx: LeadLagContext) -> str | None:
    if ctx.industry_regime_now == 0 or ctx.industry_changed_weeks_ago is None:
        return None
    line = (f"{industry} signalled {_dir_word(ctx.industry_regime_now)} "
            f"{_ago(ctx.industry_changed_weeks_ago)}")
    corr = [_corro_phrase(c) for c in ctx.corroborators]
    if corr:
        line += ". Direction bolstered by " + "; ".join(corr)
    return line


def render_leadlag_lines(row, ctx: LeadLagContext | None) -> list[str]:
    """Return the clause as a list of plain lines (may be empty). `row` must
    have an `industry` attribute; anything falsy suppresses the clause."""
    industry = getattr(row, "industry", None)
    if industry is None or ctx is None:
        return []
    out: list[str] = []
    stock = _stock_phrase(industry, ctx)
    if stock:
        out.append(stock)
    ind = _industry_phrase(industry, ctx)
    if ind:
        out.append(ind)
    return out


def render_leadlag_text(row, ctx: LeadLagContext | None) -> str:
    lines = render_leadlag_lines(row, ctx)
    if not lines:
        return ""
    return "  " + "  ".join(f"[{line}]" for line in lines)


def render_leadlag_html(row, ctx: LeadLagContext | None) -> str:
    lines = render_leadlag_lines(row, ctx)
    if not lines:
        return ""
    inner = "".join(
        f'<div>{_esc(line)}</div>' for line in lines
    )
    return (
        '<div style="font-size: 12px; color: #888; '
        'margin: 2px 0 8px 0;">' + inner + '</div>'
    )
