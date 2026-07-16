# Design: Lead/Lag Analytics

Status: **DESIGN — not yet implemented**
Scope: inter-industry and intra-industry (stock↔industry) lead/lag detection,
surfaced on each stock signal in the email and GUI.
Author's note: this doc supersedes the earlier four-phase sketch
(`industry_correlation_pairs` / `industry_regime_events` /
`ticker_industry_transmission_stats`). That sketch predated the current schema
and substantially overestimated the work — most of the compute already exists.

---

## 1. Goal

For every stock signal in the outgoing email, answer two questions the operator
currently has to eyeball:

1. **Does this stock lead or lag its own industry, and by how much?**
   e.g. *"Tata Motors usually lags Automobiles by 2 weeks at 71% correlation."*

2. **What is that industry's regime doing, and which OTHER industries
   corroborate the move?**
   e.g. *"Automobiles signalled SELL 1 week ago. Direction bolstered by −78%
   correlation at 4-week lag with Oil & Gas (turned positive 5 weeks ago) and
   −84% at 6-week lag with Steel (turned positive 7 weeks ago)."*

The correctness bar is the project invariant: **wrong data → wrong decisions →
loss of money.** A lead/lag number that is subtly miscomputed does not error; it
silently misleads. Every methodology choice below is made with that in mind.

---

## 2. What already exists (audit result)

The compute machinery for the industry↔industry and stock↔industry relationships
is **already built and wired into the backfill**. The design is therefore mostly
*re-parametrisation + a new read/render layer*, not new analytics from scratch.

### 2.1 Industry↔industry — `industry_regime_corr` + `influential_industry_ensemble`

- **Table `industry_regime_corr`** — PK `(industry_i, industry_j, window)`,
  columns `asof_date, corr, best_lag, lead_lag_corr, n_obs`. `corr` is the
  contemporaneous (lag-0) correlation; `best_lag` is the offset in weeks that
  maximised `|ρ|`; `lead_lag_corr` is the signed correlation at that lag.
- **Compute core `compute_regime_matrix()`** (`regime_matrix.py`): takes
  `{industry: {week: value}}`, residualises each industry against the
  cross-industry mean (`resid = g − g_mean`), then for each ordered pair scans
  `for lag in range(-k, k+1)` and keeps the lag with the largest `|ρ|`. This is
  exactly the ±k-offset / max-|ρ| method the spec calls for.
- **Input series is BREADTH, not raw regime.** `_breadth_by_industry()` feeds it
  `avg(display_regime)` across an industry's constituents per week (a continuous
  value in [−1, +1] from `stock_indicator_state`, timeframe `1W`, scoped to each
  industry's constituent membership window). This is a *richer* signal than the
  industry index's own −1/0/+1 regime — see §4.1 for why that changes the
  Spearman decision for this side.
- **Table `influential_industry_ensemble`** — PK `(industry_i, member_j,
  variant)`, columns `corr, best_lag, is_leader, sign, asof_date`. Derived from
  the matrix by `compute_ensembles()`. This is **already** the "which industries
  meaningfully lead/lag industry i, with direction" structure the email needs.
- **Wiring:** `backfill_industry_regime_matrix()` runs as backfill stage
  `regime_matrix` (6c). Default `window = 260` weeks, lag `_K = 4`,
  correlation = Pearson (`np.corrcoef`).

### 2.2 Stock↔industry — `sector_stock_leadlag`

- **Table `sector_stock_leadlag`** — PK `(ticker_id, asof_date, window)`,
  columns `industry_name, rs_rank, insync_corr, best_lag, lead_lag_corr,
  beta_sector, coupling_class, n_obs, is_constituent`. `coupling_class` ∈
  {LEADER, LAGGARD, COUPLED, DECOUPLED}.
- **Compute core `compute_sector_leadlag()`** (`sector_cross_section.py`):
  debiases the industry index by removing the stock's own weight
  (`r_Is = (r_I − w_s·r_s)/(1 − w_s)` — so a large constituent doesn't correlate
  with itself), then the same `range(-k, k+1)` / max-|ρ| scan. Classifies:
  `best_lag>0 → LEADER`, `<0 → LAGGARD`, within noise floor `2/√n_obs →
  DECOUPLED`, else `COUPLED`.
- **Input series is RETURNS, not regime.** `r_s` / `r_I` are weekly returns, not
  the −1/0/+1 regime. This diverges from the spec (see §4.2).
- **Wiring:** `backfill_sector_metrics()` recomputes it per industry inside the
  backfill.

### 2.3 What is NOT built

Confirmed by grep: **nothing** in `compose_email` / `send_signal_report` /
`build_email_body` reads any of the three tables. `influential_industry_ensemble`
is read only by the ML `backfill_signal_ensemble_features` runner — never shown
to a human. So the entire surfacing layer (§5) is new.

---

## 3. Sign conventions (get these right or the email inverts)

These are load-bearing. The renderer's sentence flips if any is misread.

- **`industry_regime_corr.best_lag` for pair (i, j):** the scan aligns
  `xa[:-lag]` (industry i, earlier) with `xb[lag:]` (industry j, later) at
  positive lag. So **`best_lag > 0` ⟹ i's earlier values predict j's later
  values ⟹ industry i LEADS industry j by `best_lag` weeks.** Symmetrically
  `best_lag < 0` ⟹ i lags j.
- **`influential_industry_ensemble.is_leader` for (industry_i, member_j):**
  derived as `is_leader = (best_lag < 0)` — i.e. member_j leads industry_i when
  the (i, j) pair has negative lag (i lags j). Consistent with the above.
- **`influential_industry_ensemble.sign`:** `+1 if corr > 0 else −1` — the
  direction of the relationship, independent of lead/lag.
- **`sector_stock_leadlag.best_lag` for (stock, its industry):** `best_lag > 0
  → coupling_class LEADER` ⟹ the **stock leads the industry**; `< 0 → LAGGARD`
  ⟹ the stock lags the industry.

The renderer must phrase from the SIGNAL's subject. For a Tata Motors signal:
its `sector_stock_leadlag` row is keyed by Tata Motors' ticker_id with
`industry_name = Automobiles`; `coupling_class = LAGGARD` (best_lag < 0) reads as
*"Tata Motors lags Automobiles by |best_lag| weeks."*

---

## 4. Methodology gaps vs the spec, and the decisions

Three deltas separate the existing compute from the spec. Each is a correctness
decision, not a cosmetic knob.

### 4.1 Correlation: Pearson today → Spearman (with a caveat per side)

The spec asks for **Spearman** (rank correlation). The rationale is sound for
ordinal data: regime is −1/0/+1, and Pearson assumes the step 0→+1 equals −1→0
and that spacing is linear, which ordinal codes don't guarantee.

But the two sides feed *different* series, so the recommendation differs:

- **Stock↔industry (Phase C):** the spec's input is the stock's −1/0/+1 regime
  vs the industry's regime — genuinely ordinal. **Spearman is clearly correct
  here.**
- **Industry↔industry (Phase A):** the existing input is **breadth**
  (`avg(display_regime)`, continuous in [−1,+1]), not raw regime. On a continuous
  signal Pearson is defensible. **Decision: still switch to Spearman for
  consistency and robustness to outliers**, but flag that the practical
  difference on breadth will be smaller than on raw regime. If a parity test
  shows Spearman materially changes the industry matrix, revisit — breadth may be
  the better input to keep and Pearson acceptable on it.

**Implementation (no new dependency):** `scipy` is NOT currently a dependency
(verified — no `scipy`/`spearmanr`/`rankdata` anywhere). Implement Spearman as
numpy rank-then-Pearson to keep the dependency surface unchanged on the
correctness-critical signal path:

```python
def _rankdata(a):
    # average ranks for ties; a is 1-D float
    order = a.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(len(a), dtype=float)
    # average-tie correction
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    starts = csum - counts
    avg = (starts + csum - 1) / 2.0
    return avg[inv]

def _spearman(a, b):
    if len(a) < 3 or a.std() == 0.0 or b.std() == 0.0:
        return None
    ra, rb = _rankdata(a), _rankdata(b)
    return float(np.corrcoef(ra, rb)[0, 1])
```

Swap `_corr` → `_spearman` in **both** cores so the two sides agree.

### 4.2 Stock↔industry input: returns today → REGIME

The spec's Phase C is explicit: offset the **regime** and Spearman it against the
industry's **regime**. The existing `compute_sector_leadlag` correlates weekly
**returns**. This is a genuine semantic change:

- Replace the `r_s` (stock weekly return) input with the stock's weekly
  `display_regime` series from `stock_indicator_state` (timeframe `1W`).
- Replace the `r_I` (industry index return) input with the industry's regime
  series. **Decision needed:** use the industry index's own `display_regime`
  (`industry_indicator_state`) OR the breadth series the industry matrix already
  uses. Recommend **breadth**, for consistency with Phase A and because it's a
  smoother, less-quantised industry regime proxy.
- The self-weight debiasing (`r_Is`) still applies conceptually — a stock that is
  a big constituent will move the breadth — so keep the debiasing but recompute
  it against the regime/breadth series rather than returns.

This is the riskiest change and needs **parity tests**: compute both the old
(returns) and new (regime) versions on a fixture industry and assert the new one
produces sane best_lag/coupling for a known leader (e.g. a mega-cap that the
industry visibly follows).

### 4.3 Lag window: ±4 today (`_K = 4`) → ±10

Both cores hardcode `_K = 4`. Widen to 10 (or make it a config knob
`leadlag_max_weeks`). Cost: `n_obs` shrinks by `|lag|` at each offset, so a ±10
scan needs ≥ ~13 usable weeks before lag 10 is computable. The existing `< 3
obs → None` guard already degrades gracefully; no special handling needed, but
the noise-floor `2/√n_obs` will be looser at the far lags — acceptable, and the
max-|ρ| selection naturally prefers the better-supported lag when correlations
tie.

---

## 5. Phase B — probably DROP

The original Phase B was `industry_regime_events` — a materialised log of regime
transitions ("Automobiles flipped BULLISH→BEARISH on week W"), intended to power
the *"industry signalled SELL N weeks ago"* clause of the email.

**That fact is derivable on the fly** from `industry_indicator_state.display_regime`
(or the breadth series): the most recent week where the industry's regime last
changed sign is a single windowed query. Materialising an events table adds a
write path, a migration, and a consistency burden for data that a ~10-row lookup
produces at email time.

**Decision: drop Phase B.** Derive the "changed N weeks ago" clause on demand in
the read layer (§6). Reintroduce a materialised table ONLY if profiling shows the
on-the-fly derivation is too slow for the email path (it won't be — the email
already does several per-signal queries).

---

## 6. The new build — read layer + renderer (Phases D–E)

This is where the genuinely new code is.

### 6.1 Read layer

A single function, given a signal's `(ticker_id, industry)`, returns a
`LeadLagContext`:

```
LeadLagContext:
  # stock vs its own industry
  stock_best_lag:       int | None      # weeks; sign per §3
  stock_corr:           float | None    # lead_lag_corr
  stock_coupling:       str | None      # LEADER/LAGGARD/COUPLED/DECOUPLED
  # the industry's own current regime state
  industry_regime_now:  int             # -1/0/+1 (sign of latest breadth)
  industry_changed_weeks_ago: int | None # derived, §5
  # corroborating industries (top-K by |corr|, from the ensemble)
  corroborators: list[Corroborator]     # each: industry, corr, best_lag, sign,
                                         #       is_leader, changed_weeks_ago
```

Queries (all indexed reads, latest `asof_date`):
- `sector_stock_leadlag` WHERE `ticker_id = :tid` ORDER BY `asof_date DESC`
  LIMIT 1 (pick the canonical `window`).
- `influential_industry_ensemble` WHERE `industry_i = :industry` — the
  corroborators, already ranked; take top-K by `|corr|`.
- For each corroborator + the industry itself: the "changed N weeks ago"
  derivation over the breadth series.

**Scope discipline:** this read layer is separate from `build_email_body` so it
can be unit-tested against fixtures without the email machinery, and reused by
the GUI.

### 6.2 Renderer

Given `EnrichedSignalRow` + its `LeadLagContext`, produce the clause. Phrasing
rules:
- Stock line: `coupling_class` + `|best_lag|` + `lead_lag_corr` →
  *"usually {leads|lags} {industry} by {N} weeks at {m}% correlation"*.
  Suppress if `coupling_class = DECOUPLED` or the row is missing (don't fabricate
  a relationship that isn't there — a missing/decoupled stock says nothing).
- Industry line: *"{industry} signalled {DIR} {N} weeks ago"* from the industry
  regime + change derivation.
- Corroborator lines: for each, *"{sign}{corr}% correlation at {best_lag}-week
  lag with {industry_j}, which turned {direction} {N} weeks ago"*. Cap at top-2
  or top-3 by |corr| so the email stays legible.

**Degradation:** every field is optional. On a fresh cold-start where the
lead/lag tables are thin, the renderer emits nothing rather than a broken
sentence — same discipline as the rank_score `LEFT JOIN` / default-0.0 in the
existing body build. A signal must never be dropped or delayed because its
lead/lag context is absent.

### 6.3 Insertion point

`build_email_body(rows, cycle_id, now)` already iterates `EnrichedSignalRow`s,
each carrying `industry`. Hook: for each row, call the read layer, pass the
`LeadLagContext` to the renderer, append the clause under the signal. No change
to the signal query itself.

### 6.4 GUI (Phase E)

A card/section on the signal detail view showing the same `LeadLagContext` —
the stock's coupling, the industry's regime, and the corroborator list — reusing
the §6.1 read layer verbatim. Follows the existing health-snapshot card pattern.

---

## 7. Implementation plan (revised sizing)

Ordered; each a small green commit or two, behind the existing test suite.

- **A′ — regime matrix re-parametrise.** Swap `_corr`→`_spearman` and `_K` 4→10
  (or config knob) in `regime_matrix.py`. Parity test: assert the matrix still
  populates and spot-check a known leader pair. ~1 session.
- **C′ — sector leadlag on regime.** Swap `compute_sector_leadlag` input from
  returns to the stock/industry **regime (breadth)** series; Spearman; ±10.
  Keep the self-weight debiasing. Parity tests against a known mega-cap leader.
  The riskiest change — ~1–2 sessions.
- **B — dropped** (derive on the fly; §5).
- **D — read layer + renderer + email hook.** New `LeadLagContext` reader, the
  renderer, and the `build_email_body` hook. Unit-tested against fixtures.
  ~2–3 sessions.
- **E — GUI card.** Reuse the reader. ~1 session.

Net: roughly **half** the original estimate — A and C are re-parametrisations of
existing, wired compute; B is unnecessary; the real new work is the read/render
layer (D) and the GUI card (E).

---

## 8. Open decisions to confirm before coding

1. **Spearman on breadth (Phase A):** accept the small practical change, or keep
   Pearson on the continuous breadth series and apply Spearman only to the
   ordinal stock↔industry side? (§4.1) — recommend Spearman both sides, revisit
   if the parity test shows a material industry-matrix shift.
2. **Industry regime proxy for Phase C:** correlate the stock's regime against
   the industry **breadth** series (recommended, consistent with A) or against
   the industry index's own `industry_indicator_state.display_regime`? (§4.2)
3. **best_lag sign phrasing:** confirm the §3 conventions match the intended
   email wording so "leads/lags" is not inverted.
4. **Corroborator cap:** top-2 or top-3 industries in the email? (legibility vs
   completeness — §6.2)
5. **Phase B:** confirm the on-the-fly "changed N weeks ago" derivation is
   acceptable and the materialised events table is dropped. (§5)
