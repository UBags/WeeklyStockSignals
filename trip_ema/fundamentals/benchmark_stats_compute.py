"""Phase 6-3: Pure-function regression math for alpha/beta/RS.

No DB I/O. Repositories handle persistence; runners orchestrate the
fetch → compute → write loop. This module is just the statistics.

The mathematics
---------------
For a stock series r_s and benchmark series r_b of weekly arithmetic
returns over the window:

  β       = Cov(r_s, r_b) / Var(r_b)
            The slope of OLS regression r_s = α + β × r_b + ε. β = 1.3
            means a 1% benchmark move historically pulls the stock
            1.3% on average.

  α/week  = mean(r_s) − β × mean(r_b)
  α/year  = α/week × 52
            The "excess weekly return" the stock generated beyond what
            β alone predicts. Annualization is simple multiplication
            (not compounding), keeping α in the same units as r_s.

  R²      = ρ² where ρ = Cov / (σ_s × σ_b)
            Fraction of the stock's return variance explained by the
            benchmark. Filters out the "high β but noise" case.

  σ_stock = stdev(r_s) × √52
            Stock's own annualized volatility, benchmark-independent.

  RS%     = (total_return_stock − total_return_benchmark) over window
            Operationally the single most predictive momentum metric.
            total_return = close_last / close_first − 1.

  IR      = (α/week / stdev(r_s − β × r_b)) × √52
            Risk-adjusted alpha. The denominator is tracking error.

Minimum 8 returns for the regression to be meaningful. Below that the
regression stats are NULL but n_weeks and rs_pct are still recorded.

Inputs
------
Two parallel close-price lists, ALIGNED on the same date axis, sorted
oldest-first. The caller (a runner) is responsible for aligning before
passing in — use align_series_by_date() for the typical case where the
benchmark series starts earlier than the stock series.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import sqrt
from typing import Optional, Sequence


# Window sizes in weekly-return units. Match the schema's CHECK
# constraint on window_label.
WINDOW_RETURNS = {
    "SHORT": 26,
    "MEDIUM": 52,
    "LONG": 156,
}

# Minimum observation count before regression metrics are considered
# meaningful. Below this the runner stores NULL for β/α/R²/σ/IR but
# still records n_weeks and rs_pct (the latter is just a close ratio,
# valid at any length ≥ 1).
MIN_USEFUL_OBSERVATIONS = 8

# Annualization factor for weekly returns.
WEEKS_PER_YEAR = 52

# Treat anything below this as effectively zero variance. Realistic
# weekly-return variance is ~1e-6 or larger (a stock with 0.1%/wk
# stdev has var = 1e-6); 1e-18 is well below the noise floor of any
# real series but catches the synthetic "perfectly constant returns"
# case where float arithmetic leaves var ≈ 1e-30 — without this,
# β = cov/var explodes into garbage.
_VAR_EPSILON = 1e-18


@dataclass(frozen=True, slots=True)
class RegressionStats:
    """Output of compute_regression_stats.

    All metric fields are Optional. They are populated only when
    n_weeks >= MIN_USEFUL_OBSERVATIONS and the benchmark variance is
    non-zero. n_weeks and rs_pct are populated whenever the input has
    at least 2 closes (1 return).
    """
    n_weeks: int
    beta: Optional[float]
    alpha_annual: Optional[float]
    r_squared: Optional[float]
    sigma_annual: Optional[float]
    rs_pct: Optional[float]
    info_ratio: Optional[float]


def compute_regression_stats(
    stock_closes: Sequence[float],
    bench_closes: Sequence[float],
) -> RegressionStats:
    """Compute regression statistics for one stock vs one benchmark.

    Both inputs are aligned, oldest-first, no missing values. Length
    must match.

    Returns a RegressionStats with NULL metric fields when:
      - either input has fewer than 2 closes (no returns at all);
      - n_weeks < MIN_USEFUL_OBSERVATIONS (regression unstable);
      - Var(benchmark) == 0 (β undefined).

    rs_pct is populated whenever there are ≥ 2 closes — it's just a
    close-to-close ratio, no regression involved.
    """
    if len(stock_closes) != len(bench_closes):
        raise ValueError(
            f"stock_closes and bench_closes must be the same length; "
            f"got {len(stock_closes)} vs {len(bench_closes)}"
        )

    if len(stock_closes) < 2:
        return RegressionStats(
            n_weeks=0, beta=None, alpha_annual=None, r_squared=None,
            sigma_annual=None, rs_pct=None, info_ratio=None,
        )

    # Simple weekly returns.
    r_s = [stock_closes[i] / stock_closes[i - 1] - 1
           for i in range(1, len(stock_closes))]
    r_b = [bench_closes[i] / bench_closes[i - 1] - 1
           for i in range(1, len(bench_closes))]
    n = len(r_s)

    # rs_pct can be computed at any length >= 2: total return of stock
    # minus total return of benchmark over the same window.
    rs_pct: Optional[float] = (
        (stock_closes[-1] / stock_closes[0] - 1)
        - (bench_closes[-1] / bench_closes[0] - 1)
    )

    if n < MIN_USEFUL_OBSERVATIONS:
        return RegressionStats(
            n_weeks=n, beta=None, alpha_annual=None, r_squared=None,
            sigma_annual=None, rs_pct=rs_pct, info_ratio=None,
        )

    # Sample statistics — use n-1 denominator for unbiased estimators.
    mean_s = sum(r_s) / n
    mean_b = sum(r_b) / n
    var_s = sum((x - mean_s) ** 2 for x in r_s) / (n - 1)
    var_b = sum((x - mean_b) ** 2 for x in r_b) / (n - 1)
    cov = sum((r_s[i] - mean_s) * (r_b[i] - mean_b)
              for i in range(n)) / (n - 1)

    sigma_annual = sqrt(var_s) * sqrt(WEEKS_PER_YEAR) if var_s > 0 else 0.0

    if var_b < _VAR_EPSILON:
        # Benchmark is flat over the window. β is mathematically
        # undefined. Skip regression-derived stats but keep σ and RS.
        return RegressionStats(
            n_weeks=n, beta=None, alpha_annual=None, r_squared=None,
            sigma_annual=sigma_annual, rs_pct=rs_pct, info_ratio=None,
        )

    beta = cov / var_b
    alpha_per_week = mean_s - beta * mean_b
    alpha_annual = alpha_per_week * WEEKS_PER_YEAR

    if var_s < _VAR_EPSILON:
        # Stock is flat. R² is technically undefined; report 0.
        r_squared = 0.0
    else:
        rho = cov / (sqrt(var_s) * sqrt(var_b))
        # Clamp to [0, 1] — mathematically valid but FP rounding can
        # push to 1.0000000003 etc.
        r_squared = max(0.0, min(1.0, rho * rho))

    # Information ratio: α / tracking_error, annualized via √52.
    residuals = [r_s[i] - beta * r_b[i] for i in range(n)]
    mean_residual = sum(residuals) / n
    var_residual = sum((x - mean_residual) ** 2 for x in residuals) / (n - 1)
    if var_residual > 0:
        te_per_week = sqrt(var_residual)
        info_ratio: Optional[float] = (
            (alpha_per_week / te_per_week) * sqrt(WEEKS_PER_YEAR)
        )
    else:
        info_ratio = None

    return RegressionStats(
        n_weeks=n,
        beta=beta,
        alpha_annual=alpha_annual,
        r_squared=r_squared,
        sigma_annual=sigma_annual,
        rs_pct=rs_pct,
        info_ratio=info_ratio,
    )


def align_series_by_date(
    stock_pairs: Sequence[tuple],   # iterable of (date, close)
    bench_pairs: Sequence[tuple],
) -> tuple[list[float], list[float]]:
    """Inner-join two (date, close) series on date.

    Returns (stock_closes, bench_closes) — two parallel lists where
    the i-th elements share the same date and both inputs originally
    contained that date. Dates present in only one series are silently
    dropped.

    Typical reasons for date mismatch:
      - Stock newly listed; benchmark started earlier
      - Holiday/non-trading day handling differs between series
      - Industry index is daily-aggregated; ticker is weekly-bar

    The output preserves the date ORDER of stock_pairs (oldest-first
    if that's how the input was sorted).
    """
    bench_map = dict(bench_pairs)
    stock_aligned: list[float] = []
    bench_aligned: list[float] = []
    for d, s_close in stock_pairs:
        b_close = bench_map.get(d)
        if b_close is None:
            continue
        stock_aligned.append(float(s_close))
        bench_aligned.append(float(b_close))
    return stock_aligned, bench_aligned


def slice_window(
    stock_closes: list[float],
    bench_closes: list[float],
    window_returns: int,
) -> tuple[list[float], list[float]]:
    """Trim the trailing window_returns+1 closes from each series.

    Returns aligned suffixes ready to pass to
    compute_regression_stats. If the series is shorter than the
    requested window, returns whatever is available (the resulting
    RegressionStats.n_weeks reflects the actual count).
    """
    n_wanted = window_returns + 1   # N returns need N+1 closes
    return stock_closes[-n_wanted:], bench_closes[-n_wanted:]
