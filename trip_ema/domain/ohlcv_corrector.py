"""Corporate-action seam correction for the daily price series (§6.1).

This is the analytical core of the OHLCV-correctness work: given a
ticker's ordered daily bars plus the authoritative nselib split/bonus
actions, decide, per action, whether yfinance adjusted the seam
correctly, skipped it (SEAM_PRESENT), mis-scaled it (BOTCHED), or
whether the step at that boundary has no split explanation at all
(UNEXPLAINED → censor). Corrections are applied by the exact
``f``-derived factor, never by the measured ratio.

Governing invariant: wrong data → wrong decisions → loss of money.
Two money rules are enforced structurally here:

  1. We never apply a multiplier the known factor cannot explain. The
     measured seam ratio ``R`` only *selects* which ``f``-signature we
     are on; the magnitude applied is always the exact signature value
     ``s`` derived from the nselib factor ``f``. Measurement noise and
     any real intraday move on the boundary bar therefore cannot leak
     into the correction.
  2. A step that matches no ``f``-signature (a real move, a demerger,
     a mis-dated action) is CENSORED, not spliced. Continuity is the
     right target only across a value-neutral event; forcing it across
     a real value transfer would invent a price.

This module is pure domain: it imports nothing from the rest of the
package and has no I/O. The ETL/backfill layer adapts pandas frames /
DB rows to :class:`PriceBar` / :class:`SplitAction` and consumes
:class:`CorrectionResult`.

Bad-tick and phantom-bar dropping is a SEPARATE, upstream stage (the
existing ``ohlcv_corrector`` prototype path) and runs BEFORE this
reconcile; it is intentionally not duplicated here. This module only
touches split/bonus seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from statistics import median

__all__ = [
    "Verdict",
    "PriceBar",
    "SplitAction",
    "CorrectionConfig",
    "BoundaryReport",
    "CensorWindow",
    "CorrectionResult",
    "reconcile_and_correct",
]


class Verdict(str, Enum):
    """Per-boundary classification outcome."""

    ADJUSTED_OK = "ADJUSTED_OK"          # R ≈ 1 — yfinance got it right, no-op
    SEAM_PRESENT = "SEAM_PRESENT"        # R ≈ f — yfinance skipped it; splice by f
    BOTCHED = "BOTCHED"                  # R ≈ 1/f, f², or 1/f² — scale error; splice
    UNEXPLAINED = "UNEXPLAINED"          # matches no signature — real move → censor
    AMBIGUOUS_REVIEW = "AMBIGUOUS_REVIEW"  # small-f, >1 signature in tol → review+censor
    NO_BARS = "NO_BARS"                  # not enough quiet bars either side to measure


@dataclass(frozen=True, slots=True)
class PriceBar:
    """One daily bar, keyed by calendar day in the exchange's own zone.

    ``day`` is a plain ``date`` on purpose: seam windows are measured in
    trading days, and the split boundary is a calendar boundary, not an
    instant. The ETL adapter converts to/from the tz-aware storage form.
    """

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class SplitAction:
    """An authoritative nselib split/bonus.

    ``factor`` is ``f``: the value-neutral PRICE multiplier of the event
    (post-scale / pre-scale). Split ``Fv old→new`` ⇒ ``f = new/old``
    (10→2 ⇒ 0.2). Bonus ``a:b`` ⇒ ``f = b/(a+b)`` (1:1 ⇒ 0.5).

    ``marker_date`` is the date yfinance actually applied the adjustment
    (from its own ``Stock Splits`` column), used to locate the true
    boundary. When absent we fall back to ``effective_date`` — which may
    be an ex/record/announcement date and off by a bar or two.
    """

    effective_date: date
    factor: float
    kind: str = "split"           # "split" | "bonus"
    marker_date: date | None = None


@dataclass(frozen=True, slots=True)
class CorrectionConfig:
    window: int = 3               # bars each side of the boundary bar
    min_window: int = 2           # fewer quiet bars than this → cannot measure
    tol: float = 0.10             # relative snap tolerance for large f
    tol_tight: float = 0.04       # relative snap tolerance in the small-f band
    small_f_lo: float = 0.7       # f in (lo, hi) ⇒ signatures bunch ⇒ tight tol
    small_f_hi: float = 1.4
    max_window_dispersion: float = 0.15  # (max−min)/median cap for a "quiet" window
    adjust_volume: bool = True    # scale volume by 1/s for share-count consistency


@dataclass(frozen=True, slots=True)
class BoundaryReport:
    effective_date: date
    boundary: date | None
    factor: float
    measured_r: float | None
    snapped_s: float | None
    verdict: Verdict
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CensorWindow:
    start: date
    end: date
    reason: str


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    bars: tuple[PriceBar, ...]
    reports: tuple[BoundaryReport, ...]
    censored: tuple[CensorWindow, ...]

    @property
    def cumulative_multiplier(self) -> float:
        """Product of the snapped signatures actually applied.

        A ticker's earliest bar should have moved by exactly this factor
        relative to its raw input — the standard verification identity.
        """
        m = 1.0
        for r in self.reports:
            if r.verdict in (Verdict.SEAM_PRESENT, Verdict.BOTCHED) and r.snapped_s:
                m *= r.snapped_s
        return m


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _rel_dispersion(values: list[float]) -> float:
    """(max − min) / |median|. Cheap, robust-enough quietness gauge."""
    med = median(values)
    if med == 0:
        return float("inf")
    return (max(values) - min(values)) / abs(med)


def _signatures(f: float) -> dict[str, float]:
    """The candidate seam ratios for factor ``f``.

    ``1`` (already adjusted), ``f`` (skipped), and the scale-error
    signatures ``1/f``, ``f²``, ``1/f²``. Deduped by value so an ``f``
    that makes two signatures coincide (e.g. f=1) does not double-count.
    """
    raw = {
        "1": 1.0,
        "f": f,
        "1/f": 1.0 / f,
        "f2": f * f,
        "1/f2": 1.0 / (f * f),
    }
    # Collapse numerically-coincident signatures onto the "simplest" label.
    order = ["1", "f", "1/f", "f2", "1/f2"]
    seen: dict[float, str] = {}
    out: dict[str, float] = {}
    for label in order:
        val = raw[label]
        key = round(val, 9)
        if key in seen:
            continue
        seen[key] = label
        out[label] = val
    return out


def _rel_dist(r: float, s: float) -> float:
    return abs(r / s - 1.0)


def _boundary_pos(bars: list[PriceBar], b_day: date) -> int:
    """Index of the first bar with ``day >= b_day`` (the boundary bar)."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].day < b_day:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _resolve_boundary(action: SplitAction, bars: list[PriceBar]) -> date:
    """Prefer yfinance's own applied-adjustment marker when it lands
    inside the stored history; otherwise the nselib effective_date."""
    if action.marker_date is not None:
        first, last = bars[0].day, bars[-1].day
        if first <= action.marker_date <= last:
            return action.marker_date
    return action.effective_date


def _classify(
    r: float, f: float, cfg: CorrectionConfig
) -> tuple[Verdict, float | None, str]:
    """Snap ``r`` to the nearest ``f``-signature and name the verdict.

    Returns (verdict, snapped_s_or_None, reason).
    """
    sigs = _signatures(f)
    small_f = cfg.small_f_lo < f < cfg.small_f_hi
    tol = cfg.tol_tight if small_f else cfg.tol

    within = [
        (label, val, _rel_dist(r, val))
        for label, val in sigs.items()
        if _rel_dist(r, val) <= tol
    ]
    within.sort(key=lambda t: t[2])

    if not within:
        return (
            Verdict.UNEXPLAINED,
            None,
            f"R={r:.4f} matches no f-signature within tol={tol:.0%} "
            f"(f={f:.4f}); real move / demerger / misdate → censor",
        )

    if len(within) > 1:
        # Two signatures within tolerance — genuinely ambiguous. In the
        # small-f band this is expected (signatures bunch); route to review.
        # Outside it, treat as unexplained and censor.
        labels = ", ".join(f"{lbl}={val:.4f}" for lbl, val, _ in within)
        if small_f:
            return (
                Verdict.AMBIGUOUS_REVIEW,
                None,
                f"R={r:.4f} within tight tol of multiple signatures "
                f"[{labels}] in small-f band → manual review",
            )
        return (
            Verdict.UNEXPLAINED,
            None,
            f"R={r:.4f} ambiguous between [{labels}] → censor",
        )

    label, val, _ = within[0]
    if label == "1":
        # Matching "1" means no correction is needed. That is safe to accept
        # even in the small-f band — we are not modifying any price.
        return (Verdict.ADJUSTED_OK, None, f"R={r:.4f} ≈ 1 — already adjusted")

    # A single non-1 match WOULD modify prices. In the small-f band the
    # signatures bunch tightly enough that a genuine ~15-20% move can sit on
    # f or 1/f; we refuse to auto-apply a price change on that evidence and
    # route to manual review instead (censored meanwhile). Outside the band
    # the signatures are well separated, so the match is trustworthy.
    if small_f:
        return (
            Verdict.AMBIGUOUS_REVIEW,
            None,
            f"R={r:.4f} ≈ {label}={val:.4f} in small-f band (f={f:.4f}); a real "
            f"move can impersonate this factor → manual review, not auto-splice",
        )

    if label == "f":
        return (
            Verdict.SEAM_PRESENT,
            val,
            f"R={r:.4f} ≈ f={val:.4f} — yfinance skipped it; splice pre-leg ×{val:.4f}",
        )
    # 1/f, f2, 1/f2
    return (
        Verdict.BOTCHED,
        val,
        f"R={r:.4f} ≈ {label}={val:.4f} — scale error; splice pre-leg ×{val:.4f}",
    )


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #


def reconcile_and_correct(
    bars: list[PriceBar] | tuple[PriceBar, ...],
    actions: list[SplitAction] | tuple[SplitAction, ...],
    config: CorrectionConfig | None = None,
) -> CorrectionResult:
    """Measure → classify(snap) → apply, per split/bonus, newest→oldest.

    Bars are assumed already cleaned of bad ticks / phantoms upstream.
    Returns the corrected series (a new tuple; input is not mutated), a
    per-boundary report, and the list of censored windows for downstream
    walk invalidation.
    """
    cfg = config or CorrectionConfig()

    ordered = sorted(bars, key=lambda b: b.day)
    n = len(ordered)
    # Mutable working columns — we rebuild frozen PriceBars at the end.
    days = [b.day for b in ordered]
    o = [float(b.open) for b in ordered]
    h = [float(b.high) for b in ordered]
    lo = [float(b.low) for b in ordered]
    c = [float(b.close) for b in ordered]
    v = [float(b.volume) for b in ordered]

    reports: list[BoundaryReport] = []
    censored: list[CensorWindow] = []

    # Newest → oldest so each correction (applied to all bars before its
    # boundary) composes multiplicatively down history. Boundaries already
    # processed are all NEWER, so an older action's measurement windows lie
    # entirely below them and R (a ratio) is unaffected by their scaling —
    # provided a window does not reach across a newer boundary, which we
    # clip against below.
    acts = sorted(actions, key=lambda a: a.effective_date, reverse=True)
    processed_boundaries: list[date] = []

    for action in acts:
        f = action.factor
        if f <= 0:
            reports.append(
                BoundaryReport(
                    action.effective_date, None, f, None, None,
                    Verdict.UNEXPLAINED, f"non-positive factor f={f}",
                )
            )
            continue

        if n == 0:
            reports.append(
                BoundaryReport(
                    action.effective_date, None, f, None, None,
                    Verdict.NO_BARS, "no bars",
                )
            )
            continue

        b_day = _resolve_boundary(action, ordered)
        pos = _boundary_pos(ordered, b_day)  # first bar with day >= b_day

        # Nearest NEWER processed boundary bounds the after-window.
        newer = [pb for pb in processed_boundaries if pb > b_day]
        next_newer = min(newer) if newer else None

        # before = last `window` bars strictly before the boundary bar.
        before_hi = pos                       # exclusive
        before_lo = max(0, pos - cfg.window)
        # after = up to `window` bars strictly AFTER the boundary bar,
        # excluding the boundary bar itself and stopping before any newer
        # boundary.
        after_lo = pos + 1
        after_hi = min(n, pos + 1 + cfg.window)
        if next_newer is not None:
            npos = _boundary_pos(ordered, next_newer)
            after_hi = min(after_hi, npos)

        before_c = c[before_lo:before_hi]
        after_c = c[after_lo:after_hi]

        if len(before_c) < cfg.min_window or len(after_c) < cfg.min_window:
            reports.append(
                BoundaryReport(
                    action.effective_date, b_day, f, None, None,
                    Verdict.NO_BARS,
                    f"insufficient quiet bars (before={len(before_c)}, "
                    f"after={len(after_c)}, need ≥{cfg.min_window})",
                )
            )
            continue

        disp_before = _rel_dispersion(before_c)
        disp_after = _rel_dispersion(after_c)
        if (
            disp_before > cfg.max_window_dispersion
            or disp_after > cfg.max_window_dispersion
        ):
            win_start = days[before_lo]
            win_end = days[min(after_hi, n) - 1]
            reason = (
                f"noisy seam window (disp before={disp_before:.2%}, "
                f"after={disp_after:.2%}) — cannot measure a clean ratio"
            )
            censored.append(CensorWindow(win_start, win_end, reason))
            reports.append(
                BoundaryReport(
                    action.effective_date, b_day, f, None, None,
                    Verdict.UNEXPLAINED, reason,
                )
            )
            continue

        r = median(after_c) / median(before_c)
        verdict, s, reason = _classify(r, f, cfg)

        if verdict in (Verdict.SEAM_PRESENT, Verdict.BOTCHED) and s is not None:
            # Apply the EXACT signature to every bar before the boundary.
            for i in range(pos):
                o[i] *= s
                h[i] *= s
                lo[i] *= s
                c[i] *= s
                if cfg.adjust_volume:
                    v[i] /= s
            processed_boundaries.append(b_day)
        elif verdict in (Verdict.UNEXPLAINED, Verdict.AMBIGUOUS_REVIEW):
            win_start = days[before_lo]
            win_end = days[min(after_hi, n) - 1]
            censored.append(CensorWindow(win_start, win_end, reason))
        # ADJUSTED_OK / NO_BARS: no correction, no censor.

        reports.append(
            BoundaryReport(
                action.effective_date, b_day, f, r, s, verdict, reason,
            )
        )

    corrected = tuple(
        PriceBar(days[i], o[i], h[i], lo[i], c[i], v[i]) for i in range(n)
    )
    # reports were appended newest→oldest; hand them back oldest→newest.
    reports.reverse()
    censored.sort(key=lambda w: w.start)
    return CorrectionResult(corrected, tuple(reports), tuple(censored))
