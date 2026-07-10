# WeeklyStockSignals — Enrichment Feature Spec (v0.4, AS-BUILT)

Causal feature definitions for the signal-quality model (Bad / Good / Very Good).
All features are computed on **weekly** bars and are **strictly causal** (no
lookahead); forward returns / drawdowns are *labels*, defined separately in §13.

Status: **IMPLEMENTED** — the full 217 are built, populated, and verified on NSE
(`feature_spec_version = 'v0.4'`). This document now has two layers: §1–§19 +
Appendix A are the **original design** (kept verbatim for provenance), and
**Part II — As-Built Record** (after Appendix A) documents what was actually
built, every deviation from this design, the data we saw at each step, and
commentary. Where the two disagree, **Part II is authoritative.** Inline
`> **As-built:**` notes flag the load-bearing deviations at their point of use.

---

## 1. Conventions & discipline

- **Bar index** `t` = current weekly bar; `t−1` prior, etc.
- **EMA**: `EMAₘ(X)_t = EMAₘ(X)_{t−1} + α·(X_t − EMAₘ(X)_{t−1})`, `α = 2/(m+1)`.
- **Wilder**: `Wilderₙ(X)` uses `α_w = 1/n` (the convention ATR and RSI already
  use in this codebase).
- **Normalizer `u ≡ atr_t`** — the state ATR, matching the existing
  `dist_to_*_in_hl_units` columns. Item 12 uses a **separate `atr28`** (§4.6).
- **Causality of pivots**: a peak/trough at bar `p` is only *confirmed* `k`
  bars later (§5). Any feature at bar `t` uses only pivots with `p + k ≤ t`.
- **Feature vs label**: everything here is an input `X` and is causal. Forward
  outcomes (`return_Nw`, `max_dd`, risk-adjusted) are the target `y` (§13),
  computed with lookahead for **training only** — never at inference.
- **Grain**: enrichment is computed **every bar**. The "signal-relative"
  features (§9) are ordinary per-bar features that, when read on a signal bar,
  equal the signal-to-turn lag / displacement.

---

## 2. Base series already in `stock_indicator_state` (reuse)

`med_ema`, `long_ema`, `longest_ema`, `atr`, six bands
(`upper/lower × inner/mid/outer`), `display_regime`. RSI (Wilder) and
`compute_regression_stats` (R²/linreg) helpers already exist and should be
reused.

## 3. Existing enrichment already present (extend, don't duplicate)

Per EMA (`med`/`long`/`longest`): `bars_since_*_turn`, `pct_change_since_*_turn`,
`*_pattern_label` (currently only "successive higher valleys" = HL /
"lower tops" = LH), `*_turn_buy/sell`, `dist_to_*_in_hl_units`,
`mean_dist_to_*_in_hl_units`, `pierces_upper/lower_mid`.

---

## 4. New derived series (causal, recursive, state-carried)

### 4.1 Smoothed RSI (two periods)
For `n ∈ {14, 28}`:
```
up_t     = max(close_t − close_{t−1}, 0)
dn_t     = max(close_{t−1} − close_t, 0)
auₙ_t    = auₙ_{t−1} + (1/n)·(up_t − auₙ_{t−1})          # carry auₙ
adₙ_t    = adₙ_{t−1} + (1/n)·(dn_t − adₙ_{t−1})          # carry adₙ
RSIₙ_t   = 100 − 100/(1 + auₙ_t/adₙ_t)
srsiₙ_t  = EMA₃(RSIₙ)_t                                   # carry srsiₙ
```
`smoothed_rsi ≡ srsi14`.

### 4.2 Smoothed RSI difference (items 1–2)
```
smoothed_RSI_diff_t      = srsi14_t − srsi28_t            # RSI points
ema_smoothed_RSI_diff_t  = EMA₃(smoothed_RSI_diff)_t      # carry
```
Both already on the 0–100 RSI scale → cross-sectionally comparable, no
normalization needed.

### 4.3 Smoothed MACD
```
fast_t  = EMA₁₂(close)_t                                  # carry
slow_t  = EMA₂₆(close)_t                                  # carry
macd_t  = fast_t − slow_t
sig_t   = EMA₉(macd)_t                                    # carry
hist_t  = macd_t − sig_t
nmacd_t = macd_t / u          # normalize — MACD units scale with price
smacd_t = EMA₃(nmacd)_t                                   # carry; structure series
```
(If you prefer structure on the histogram, substitute `hist_t / u` for
`nmacd_t` — see §15.)

### 4.4 Smoothed detrended oscillator (reuses `longest_ema`)
```
dosc_t  = (close_t − longest_ema_t) / u    # ≡ dist_to_longest_in_hl_units
sdosc_t = EMA₅(dosc)_t                                    # carry
```
Causal (no DPO centering shift, which would peek).

### 4.5 EMA spreads (items 4–9)
```
ema_diff_1_t = med_ema_t  − long_ema_t
ema_diff_2_t = med_ema_t  − longest_ema_t
ema_diff_3_t = long_ema_t − longest_ema_t
ema_ema_diff_i_t = EMA₃(ema_diff_i)_t     for i ∈ {1,2,3}   # carry ×3
```
Spreads are in price units. Structure (§6) is scale-free, so no normalization
needed for the pivot logic; if the *level* is used as a feature, divide by `u`.

### 4.6 28-period ATR (new — for item 12)
```
TR_t    = max(high_t − low_t, |high_t − close_{t−1}|, |low_t − close_{t−1}|)
atr28_t = atr28_{t−1} + (1/28)·(TR_t − atr28_{t−1})       # Wilder; carry
```

### 4.7 Relative-strength line vs industry index (§11)
```
rs_t     = close_t / industry_index_value_t   (rebased at series start)
rs_ema_t = EMA₁₃(rs)_t                                    # carry
```

---

## 5. Generic causal pivot detection (the shared engine)

For any series `S`, store the last `k` values.
```
peak   at p ⟺ S_p ≥ S_{p−i} and S_p ≥ S_{p+i}  ∀ i∈[1,k]
trough at p ⟺ S_p ≤ S_{p−i} and S_p ≤ S_{p+i}  ∀ i∈[1,k]
→ confirmed at bar p+k; usable only from t ≥ p+k.
```
`k = 1` for the 3 EMAs (slope flip = today's `turn_buy/sell`).
`k = 2` proposed for the oscillators/spreads; `k = 1` for `sdosc` (pre-smoothed).

Carry per structure series: `last_peak_{val,bar}`, `prev_peak_val`,
`last_trough_{val,bar}`, `prev_trough_val`, `peak_rel`, `trough_rel`,
`n_successive`, and the rolling `k`-value buffer.

---

## 6. HH / HL / LH / LL + composite structure (items 3, 10; all of §7 set)

```
on new confirmed peak:    peak_rel   = HH if last_peak_val   > prev_peak_val   else LH
on new confirmed trough:  trough_rel = HL if last_trough_val > prev_trough_val else LL

structure =
    UPTREND     if HH ∧ HL
    DOWNTREND   if LH ∧ LL
    BROADENING  if HH ∧ LL         # unstable / volatile
    CONTRACTING if LH ∧ HL         # coil — often precedes the move
n_successive = run length of consecutive same-direction pivots
```

### 6.1 The structure set 𝒮 (series that get HH/HL/LH/LL)
```
med_ema, long_ema, longest_ema,
srsi14 (smoothed_rsi), smacd, sdosc,
smoothed_RSI_diff, ema_smoothed_RSI_diff,
ema_ema_diff_1, ema_ema_diff_2, ema_ema_diff_3
   (+ rs_ema, optional)
```
Every feature in §7–§9 is produced for **each** member of 𝒮.

---

## 7. Distance-to-turn + leg stats (per 𝒮 member `X`)

```
bars_since_peak_X   = t − last_peak_bar_X
bars_since_trough_X = t − last_trough_bar_X
bars_since_turn_X   = min(bars_since_peak_X, bars_since_trough_X)   # nearest turn
value_since_turn_X:
    price/EMA/spread:  (X_t − origin_pivot_val)/|origin_pivot_val|  (pct) or /u
    oscillator:        X_t − origin_pivot_val                        (native units)
leg_amplitude_X = |last_peak_val_X − last_trough_val_X|
leg_velocity_X  = leg_amplitude_X / |last_peak_bar_X − last_trough_bar_X|
leg_maturity_X  = value_since_turn_X / median_52(past leg_amplitude_X)   # >1 = late
```

---

## 8. Signal-relative confluence — items 11 & 12 (per 𝒮 member `X`)

Computed every bar; on a signal bar these equal the signal-to-turn lag /
displacement. "Nearest" pivot = most recent **confirmed** peak or trough.

### 8.1 Item 11 — turn-to-bar lag (integer weekly bars)
```
nearest_pivot_bar_X = max(last_peak_bar_X, last_trough_bar_X)   # most recent
lag_X   = t − nearest_pivot_bar_X            # 1,2,3,... (≥ k_X)
# also expose the type of that pivot (HH/HL/LH/LL) and, cheaply, the split:
lag_peak_X   = t − last_peak_bar_X
lag_trough_X = t − last_trough_bar_X
```
Tight clustering of small `lag_X` across many members of 𝒮 at a signal =
high temporal confluence = higher-quality signal.

### 8.2 Item 12 — price displacement since that turn, in ATR₂₈ units
Always measured on **price** (the `close` at the pivot bar), regardless of which
indicator the pivot came from — puts every indicator on one comparable axis:
```
pdisp_X = (close_t − close_{nearest_pivot_bar_X}) / atr28_t     # signed
```
Interpretation: "price has moved `pdisp_X` ATRs since `X` last turned."
`(lag_X, pdisp_X)` together = the time+price displacement vector from each
indicator's turn to the current (signal) bar.

---

## 9. Normalization (causal, 52-week rolling)

```
z_S_t       = (S_t − mean_52(S)) / std_52(S)
pctrank_S_t = |{ S_{t−51..t} ≤ S_t }| / 52
```
Apply to oscillator **levels** (`srsi14`, `smacd`, `sdosc`, `smoothed_RSI_diff`)
and to `value_since_turn_X`. (State-cost note: either carry a 52-value ring
buffer per series, or approximate with O(1) EMA-based mean/mean-abs-dev — §14.)

---

## 10. Divergence (price/EMA structure × oscillator structure — the payoff)

Compare the price (or `med_ema`) pivot relation to the oscillator's relation at
its corresponding pivot. This is the reason to run structure on oscillators.
```
reg_bear = (price.peak_rel = HH) ∧ (osc.peak_rel = LH)      # new high, weaker momo
reg_bull = (price.trough_rel = LL) ∧ (osc.trough_rel = HL)  # reversal up
hid_bear = (price.peak_rel = LH) ∧ (osc.peak_rel = HH)      # continuation down
hid_bull = (price.trough_rel = HL) ∧ (osc.trough_rel = LL)  # continuation up

div_{osc}     ∈ {none, reg_bull, reg_bear, hid_bull, hid_bear}
div_strength  = | slope(price leg) − slope(osc leg) |  over the paired pivots
div_age       = t − bar the divergence completed        # fresh > stale
```
Compute for `osc ∈ {srsi14, smacd, smoothed_RSI_diff}`; price side = `close` or
`med_ema`.

---

## 11. Relative strength vs the stock's own industry index (cheap, high value)

`industry_index_value` and `industry_indicator_state` already exist per industry.
```
run §5–§8 structure on rs_t (§4.7)  → rs_pattern, rs bars/pct-since-turn, rs lag/pdisp
rs_rank_t = pctrank of the stock's N-week return among its industry's constituents
```
A stock making HH+HL on `rs` (structurally outperforming its sector) is a strong
Very-Good filter.

> **As-built (§20.2.a):** only the two manifest columns shipped — `rs_ema`
> (Tier-2, merged into the JSONB at the walk) and `rs_rank` (Tier-3, in
> `sector_stock_leadlag`). "Run §5–§8 structure on `rs`" was **deferred** — the
> manifest never counted those columns. `rs_rank` uses an **N=4-week** return
> window and, by an explicit coverage decision, ranks **every** stock mapped to
> the industry against the **constituent** reference set (not just constituents
> among themselves) — see §20.3.b for why and the coverage payoff.

---

## 12. Other curated features

```
extension_z    = z_52(dist_to_longest_in_hl_units)   ; overextended = extension_z > 2
frac_above_med = |{ last N bars: close > med_ema }| / N
chop           = std_N(dist_to_med_in_hl_units)               # low = clean trend
trend_r2       = R² of close ~ bar over last N   (reuse compute_regression_stats)
bw_t           = (upper_outer_t − lower_outer_t)/close_t
squeeze        = bw_t < pctile_20(bw, 52)
squeeze_release= squeeze_{t−1} ∧ ¬squeeze_t                   # coil → fire
vol_at_pivot   = volume_{last_trough_bar} / SMA(volume,20)_{last_trough_bar}
accumulation   = vol_at_pivot > 1.5
ema_agreement  = Σ_{med,long,longest} 1[structure = UPTREND]  ∈ {0..3}
weeks_in_regime= t − last_regime_change_bar                   # carry regime + bar
```

---

## 13. Labels (target `y` — training only, NOT features)

```
fwd_ret_Nw   = close_{t+N}/close_t − 1                 (N ∈ {4, 8, 13})
fwd_max_dd   = max peak-to-trough drawdown over t+1..t+H
fwd_rr       = fwd_ret_Nw / atr_t
class:  Bad       = fwd_ret < 0  or  fwd_max_dd large
        Good      = positive fwd_ret, moderate dd
        VeryGood  = high fwd_ret ∧ low fwd_max_dd ∧ fwd_rr above threshold
```
Thresholds tuned on data. These use future bars — computed offline for labeling
only; never present in the live feature vector.

> **As-built (§20.2.c):** labels were moved **out of the 217 vector entirely**
> into a dedicated `signal_labels` table (FK to `signal_enrichment`, cascade),
> with a `horizon_complete` flag so incomplete-horizon signals are excluded from
> training. They are targets, not features, and keeping them physically separate
> from the causal vector removes any risk of leakage. Thresholds shipped as
> **untuned placeholders** (see §20.2.c); the observed class split is in §20.3.a.

---

## 14. State the engine must carry (scope check)

New per-(ticker, timeframe) state in `StatefulIndicatorEngine`:
- RSI chains: `au14, ad14, srsi14, au28, ad28, srsi28`
- `ema_smoothed_RSI_diff`
- MACD: `fast12, slow26, macd_sig9, smacd`
- `sdosc`
- `ema_ema_diff_1/2/3`
- `atr28`
- `rs_ema` (if RS included) + `last_regime_change_bar`
- Pivot state per 𝒮 member (§5): 6 scalars + 2 relations + `n_successive` +
  a `k`-value ring buffer.
- Normalization (§9): either a 52-ring buffer per normalized series, or O(1)
  EMA mean / mean-abs-dev surrogate (recommended to bound state).

All bounded and O(1)-per-bar — fits the engine's existing carry pattern.

## 15. Placement, schema, open decisions

- **Series** (§4) → `stock_indicator_state` (they're stateful indicator series).
- **Structure + distance + confluence + divergence + RS** (§6–§11) → a **new
  companion table `stock_structure_enrichment`** rather than widening
  `stock_indicator_enrichment` by ~30–40 columns (cleaner migration + indexing;
  same daily-grain PK `(ticker_id, timeframe, date)`).
- **Build order**: (a) extend `pattern_rules` to emit full HH/HL/LH/LL +
  composite (self-contained, no schema); (b) add the §4 series + pivot state to
  `StatefulIndicatorEngine` + `atr28`; (c) migration + companion-table
  population + divergence/RS/confluence.

**Open decisions**
1. Confirmation window `k` for oscillators — proposed `k=2` (srsi/smacd/diffs),
   `k=1` (sdosc). Larger `k` = cleaner pivots but +1 week lag on a weekly bar.
2. MACD structure on the **line** (`nmacd`, §4.3, conventional for divergence)
   vs the **histogram** (`hist/u`).
3. Normalization state: 52-ring buffer (exact) vs O(1) EMA surrogate (bounded).
4. Whether `rs_ema` / `rs_rank` land in this phase or a follow-up (needs the
   industry cross-section join).

---

## 16. Sector-index ↔ stock correlation & lead/lag (item 1)

Per (sector index `I`, stock `s`): how `s` co-moves with and leads/lags its
sector — tracked separately for constituents and non-constituents.

### 16.1 Inputs — stationary (returns, NEVER levels)
```
r^s_t = ln(close_t / close_{t−1})          # stock weekly log return
r^I_t = ln(iiv_t  / iiv_{t−1})             # industry_index_value return
```

### 16.2 Remove constituent self-inclusion bias
A constituent `s` (index weight `w_s`) is mechanically correlated with `I` via
itself. Use the leave-one-out index return (cheap, no index recompute):
```
r^{I\s}_t = (r^I_t − w_s·r^s_t) / (1 − w_s)     # constituents (w_s>0)
r^{I\s}_t = r^I_t                                # non-constituents (w_s=0)
```
Carry `is_constituent` so the model can weight the two groups differently.

>  **As-built (§20.2.d):** both windows shipped as **separate rows**
> (`window=52` and `window=260`) in `sector_stock_leadlag`, sharing the table
> with `rs_rank` (`window=4`). `is_constituent` was added (migration 0043). The
> at-signal features (§16.5) read the `window=52` row. Data + commentary: §20.3.c.

### 16.3 Measures (rolling 52w AND full 5y)
```
insync_corr   = corr(r^s_t, r^{I\s}_t)                       # lag 0
ρ_L           = corr(r^s_t, r^{I\s}_{t+L}),  L ∈ [−K,+K], K=4
best_lag      = argmax_L |ρ_L|
lead_lag_corr = ρ_{best_lag}
      best_lag > 0 → stock LEADS the sector ; < 0 → LAGS ; 0 → in-sync
beta_sector   = cov(r^s, r^{I\s}) / var(r^{I\s})
```
Significance floor: keep only `|corr| > 2/√n_obs`, else treat as decoupled.

### 16.4 Foundational table — `sector_stock_leadlag` (weekly recompute)
```
(industry_id, ticker_id, is_constituent, window, asof_date,
 insync_corr, best_lag, lead_lag_corr, beta_sector, n_obs)
```

### 16.5 Enriched (at signal, per stock)
```
sector_insync_corr, sector_lead_lag (int bars ± sign), sector_beta
coupling_class:
   LEADER    leads, |corr| high   → signal is early (high value)
   LAGGARD   lags,  |corr| high   → sector already moved (confirmation / late)
   COUPLED   in-sync, |corr| high
   DECOUPLED |corr| low           → sector context uninformative for s
```

---

## 17. Industry × industry regime correlation matrix + influential ensemble (item 2)

>  **As-built (§20.2.e):** shipped with **breadth** as the regime proxy
> (the spec's *best* option), the **cross-industry mean** as the market factor,
> and **three ensemble variants** (`floor`/`mid`/`strict`) instead of a single
> (θ, M). A **single current matrix snapshot** is stored (per §17.3), which
> means historical signals read current correlations — a known, bounded
> look-ahead (§20.6). Data + the payoff: §20.3.d–f.

### 17.1 Regime proxy (continuous ≫ discrete)
Step-function `{−1,0,+1}` labels correlate poorly. Prefer a continuous regime
strength, in order of preference:
```
g^I_t = breadth = frac(constituents bull) − frac(constituents bear)      (best)
      | normalized EMA-stack spread of the industry index
      | display_regime ∈ {−1,0,+1}                                    (fallback)
```

### 17.2 Kill the common-market factor (critical)
Raw regime over 5y → matrix uniformly +ive ("all bullish in a bull market"),
uninformative. Correlate the market-adjusted RESIDUAL:
```
g̃^I_t = g^I_t − g^M_t     # g^M = broad-market (NIFTY) regime, or cross-industry mean
```
(Alternative: correlate regime CHANGES Δg^I_t = g^I_t − g^I_{t−1}; also stationary.)

### 17.3 The matrix (trailing 5y, refreshed every Sunday)
```
C(I,J)     = corr(g̃^I_t, g̃^J_t)                     # contemporaneous
C(I,J,L)   = corr(g̃^I_t, g̃^J_{t+L}),  L ∈ [−K,+K]
best_lag(I,J), lead_lag_corr(I,J)
      best_lag < 0  ⟹  J at t predicts I later  ⟹  J LEADS I   (predictive — the prize)
```
~93² pairs over ~260 weeks → trivial compute; store the whole matrix.
Keep only `|C| > 2/√n_obs`.

### 17.4 `influential_industry_ensemble(I)`
```
ens⁺(I) = { J : C(I,J) ≥ +θ }      # co-movers
ens⁻(I) = { J : C(I,J) ≤ −θ }      # counter-movers
prefer members that LEAD I (best_lag < 0) — predictive of I's regime.
store top-M by |C| above θ.
```

Foundational tables (weekly Sunday refresh):
```
industry_regime_corr          (industry_i, industry_j, window, asof,
                               corr, best_lag, lead_lag_corr, n_obs)
influential_industry_ensemble (industry_id, member_id, corr, best_lag,
                               is_leader, sign, asof)
```

### 17.5 Signal-time lookup / enriched (the described flow)
`stock s → industry I → ensemble`; aggregate the ensemble's CURRENT regime,
correlation-weighted (counter-movers flip in naturally since `C<0`):
```
ensemble_score(I,t) = Σ_{J∈ens} C(I,J)·g^J_t  /  Σ_{J∈ens} |C(I,J)|
ensemble_confirms   = sign(ensemble_score) == signal_direction
ensemble_agreement  = frac of ens members whose current regime aligns w/ signal
n_leaders_bull, n_leaders_bear    # restrict to leading (predictive) members
```
A BUY whose influential ensemble — especially its *leading* members — is bullish
is higher quality; a bearish or split ensemble downgrades it.

---

## 18. Foundational jobs, tables & consolidated build order (to-do)

New foundational DB, recomputed on a schedule. The in-app scheduler was removed,
so run as a **weekly CLI/cron job** mirroring the backfill "compute-and-store"
pattern:
```
T1  sector_stock_leadlag           §16.4   weekly
T2  industry_regime_corr           §17.3   Sunday
T3  influential_industry_ensemble  §17.4   Sunday (derived from T2)
```

These refine the already-queued lead/lag epic:
```
§17 (T2/T3)  ≈  Phase A  industry_correlation_pairs (specialized to regime)
§16 (T1)     ≈  Phase C  ticker_industry_transmission_stats (stock↔industry)
```
Grounding: Lo–MacKinlay 1990 (lead/lag, leader/follower), Hong–Torous–Valkanov
2007 (sector→market predictability), MacKinlay 1997 (event study).

Enriched columns fed from the above:
```
sector_insync_corr, sector_lead_lag, sector_beta, coupling_class          (§16.5)
ensemble_score, ensemble_confirms, ensemble_agreement, n_leaders_bull/bear (§17.5)
```

**Consolidated build order (whole spec):**
```
(a) pattern_rules → full HH/HL/LH/LL + composite            (self-contained, no schema)
(b) §4 series + pivot/normalization state in StatefulIndicatorEngine + atr28
(c) migration: stock_structure_enrichment (§6–§10) + divergence
(d) T1 sector_stock_leadlag job + §16 enrichment
(e) T2/T3 regime matrix + ensemble (Sunday job) + §17 enrichment
(f) RS vs industry (§11) if not folded into (d)
```

**Cross-cutting caveats (apply to §16 & §17):**
- Correlate **stationary** series (returns / regime residuals), never levels.
- Significance floor `|corr| > 2/√n_obs`; drop the rest as noise.
- **As-of discipline (backtest leakage)**: tables are stamped with their refresh
  `asof`. A live signal on date `d` reads the snapshot with `asof ≤ d`. For
  *historical* backtest/training, either recompute the matrix as-of each past
  date (rolling) or accept that these stats are slow-moving — but do NOT train on
  a matrix built from data after the signal date.

---

## 19. Persistence grain — SIGNAL-grain, not daily (decisive)

The full 217-vector is persisted **only on bars where a signal fires**, not every
day. Rationale:

- **Size.** Daily = ~2,655 tickers × ~5,000 trading days ≈ 13.3M rows × 217 cols
  ≈ 40–60 GB with indexes — a blow-up on the NUC, and 99%+ of rows carry no
  signal. Signal-grain = ~45k historical signals × 217 ≈ <100 MB, +few hundred
  rows/week. ~300× smaller.
- **It's the natural training grain.** The label (Bad/Good/VeryGood) is one per
  *signal* (its forward outcome). A daily no-signal row has no label — it isn't a
  training example. Signal-grain IS the dataset shape.

**Carry ≠ persist.** The `StatefulIndicatorEngine` must still walk *every* bar —
the features are stateful/causal (EMAs, pivots, correlations carried forward), so
they can't be computed at signals in isolation. It walks every bar carrying
state, and **snapshots the 217-vector only at signal bars.**

Three-tier persistence:
```
stock_indicator_state          DAILY    raw series: GUI charts, resume, carry
                                        (add new levels here only if GUI plots them)
sector_stock_leadlag           WEEKLY   §16 — looked up at signal time
industry_regime_corr           SUNDAY   §17 — looked up at signal time
influential_industry_ensemble  SUNDAY   §17 — derived from the matrix
signal_enrichment  (NEW)       SIGNAL   the 217-vector, ONE ROW PER SIGNAL  ← ML table
     PK (ticker_id, timeframe, signal_date, signal_type)  or  FK historical_signals
```
The sector/ensemble columns (§16.5, §17.5) are **looked up** from the weekly
tables at snapshot time and copied into the signal row (self-contained vector,
respecting the as-of rule in §18).

---

## Appendix A — Full column manifest (217)

Grain: one row per signal in `signal_enrichment` (§19). `[core]` = build first;
`[prune]` = phase-2 widening, keep only if importance earns it (§ prior note).
Meta columns (`ticker_id, timeframe, signal_date, signal_type, computed_at`) are
excluded from the 217 feature count.

### S — State levels snapshotted at the signal (26)
Existing (11):
```
1 med_ema   2 long_ema   3 longest_ema   4 atr
5 upper_inner  6 upper_mid  7 upper_outer
8 lower_inner  9 lower_mid 10 lower_outer   11 display_regime
```
New (15):
```
12 srsi14            13 srsi28              14 smoothed_RSI_diff
15 ema_smoothed_RSI_diff   16 smacd         17 macd_hist
18 sdosc             19 ema_diff_1          20 ema_diff_2
21 ema_diff_3        22 ema_ema_diff_1      23 ema_ema_diff_2
24 ema_ema_diff_3    25 atr28               26 rs_ema
```

### T — Structure template × 11 members (143)  [core: 6/member · prune: 7/member]
Naming: each column is `{stem}` applied to member `X`. The 13 per member:
```
core   → {X}_pattern_label, {X}_structure, bars_since_{X}_turn,
         move_since_{X}_turn, {X}_leg_maturity, pdisp_{X}
prune  → {X}_n_successive, {X}_turn_buy, {X}_turn_sell,
         bars_since_{X}_peak, bars_since_{X}_trough,
         {X}_leg_amplitude, {X}_leg_velocity
```
The 11 members `X` (→ 11 × 13 = 143 columns, #27–#169):
```
med_ema, long_ema, longest_ema,
srsi14, smacd, sdosc,
smoothed_RSI_diff, ema_smoothed_RSI_diff,
ema_ema_diff_1, ema_ema_diff_2, ema_ema_diff_3
```
(`bars_since_{X}_turn` = item-11 lag; `pdisp_{X}` = item-12 price move in ATR₂₈.)

### D — EMA distance (6)  [core]
```
170 dist_to_med_in_hl_units       173 mean_dist_to_med_in_hl_units
171 dist_to_long_in_hl_units      174 mean_dist_to_long_in_hl_units
172 dist_to_longest_in_hl_units   175 mean_dist_to_longest_in_hl_units
```

### N — Oscillator normalization (10)  [prune]
```
176 z_srsi14        177 pctrank_srsi14
178 z_smacd         179 pctrank_smacd
180 z_sdosc         181 pctrank_sdosc
182 z_rsidiff       183 pctrank_rsidiff
184 z_ema_rsidiff   185 pctrank_ema_rsidiff
```

### V — Divergence (9)  [core]
```
186 div_srsi14_class   187 div_srsi14_strength   188 div_srsi14_age
189 div_smacd_class    190 div_smacd_strength    191 div_smacd_age
192 div_rsidiff_class  193 div_rsidiff_strength  194 div_rsidiff_age
```

### C — Curated (11)  [core: 8 · prune: 3]
```
core   195 extension_z   196 frac_above_med   197 trend_r2   198 bw
       199 squeeze       200 squeeze_release  201 accumulation
       202 ema_agreement
prune  203 chop          204 vol_at_pivot     205 weeks_in_regime
```

### R — Relative strength (1)  [core]
```
206 rs_rank
```

### L — Sector lead/lag (4)  [core]
```
207 sector_insync_corr  208 sector_lead_lag  209 sector_beta  210 coupling_class
```

### E — Influential ensemble (5)  [core]
```
211 ensemble_score   212 ensemble_confirms   213 ensemble_agreement
214 n_leaders_bull   215 n_leaders_bear
```

### X — Existing enrichment kept (2)  [core]
```
216 pierces_upper_mid   217 pierces_lower_mid
```

### Count reconciliation
```
S 26 + T 143 + D 6 + N 10 + V 9 + C 11 + R 1 + L 4 + E 5 + X 2  =  217
core  ≈ 114   (T 66 + S 26 + D 6 + V 9 + C 8 + R 1 + L 4 + E 5 + X 2 − S-prune…)
prune ≈ 103   (T 77 + N 10 + C 3 + …)   ← widen only on proven importance
```
If you want the lean first cut, drop groups T-prune + N + C-prune → **~114 core**;
the full **217** is the ceiling as requested.


---
---

# Part II — As-Built Record (implementation, deviations, data & commentary)

> Everything below reflects what was actually built and measured on the NSE
> universe (~2,673 active tickers, 93 indexed industries, ~45.5k historical
> signals). Numbers are from the live `trip_ema` PostgreSQL 18 database at build
> time. Commentary is the engineer's read of each result — flagged **‹comment›**.

## 20.1 The architecture that emerged — three tiers

The single biggest design decision (not spelled out in §1–§19) was **where each
feature physically lives and computes**, driven by a hard rule: *keep the
per-signal causal engine pure single-entity.* Features were sorted into three
tiers by their data dependency and cadence:

| Tier | What | Where it lives | Cadence |
|---|---|---|---|
| 1 | Single-entity causal (the stock's own series) | `signal_enrichment.features` JSONB | per signal |
| 2 | Per-stock cross-entity **causal series** (`rs_ema`) | merged into the same JSONB at the walk | per signal |
| 3 | Cross-sectional / cross-industry **materialized analytics** | dedicated tables, joined at read-time | weekly |

**The load-bearing decision.** The `SignalFeatureAssembler` has one invariant —
*given one stock's bar stream, emit its causal per-bar vector.* Cross-entity
inputs (`rs_ema` needs the industry index; `rs_rank` needs all constituents; §16
needs the index return series; §17 needs every industry) were **not** threaded
through `update()`. Instead:

- **Tier 2** (`rs_ema`) is computed in a separate `signal_cross_entity` layer and
  **merged into the feature dict at the walk** as-of the last converged week —
  the assembler never sees the industry index.
- **Tier 3** (`rs_rank`, §16, §17) is **materialized in weekly-recompute tables**
  and joined at training-load time, exactly as §16.4/§17.3 designed.

**‹comment›** This is the decision I'd defend hardest. Smuggling a second
entity's data through the single-entity `update()` works for one scalar
(`rs_ema`) but poisons the abstraction: it doesn't generalize to `rs_rank`
(N constituents), §16 (cross-industry series), or §17 (a 93×93 matrix), and it
couples a weekly-cadence value into a signal-cadence engine. Two clean layers —
a pure assembler and a materialized analytics layer joined by key — cost more
plumbing but keep every future cross-sectional feature a *table + join*, not a
fresh hack in the hot path. "Complete" therefore means **all 217 reachable**
(194 in the JSONB + 13 already-joined + 10 in Tier-3 tables), not all 217 in one
column.

## 20.2 Deviations from the design (§1–§19), by section

**(a) §4.7 / §11 — relative strength.** Only `rs_ema` (Tier 2) and `rs_rank`
(Tier 3) shipped. "Run §5–§8 structure on `rs`" (an `rs_pattern` / `rs`
lag/pdisp member) was **deferred** — the 217 manifest only ever counted the two
scalar columns. `rs_ema = EMA₁₃(close/industry_index)`, rebased to 1.0 at the
first week both series exist, merged at the walk as-of the last converged week
(the slow EMA makes the ≤1-week forming lag immaterial).

**(b) §11 — `rs_rank` coverage.** The spec phrase "pctrank of the stock's N-week
return among its industry's constituents" was implemented as **coverage (b)**:
rank *every* stock mapped to the industry against the **constituent reference
set**, rather than ranking constituents among themselves. This changed coverage
of index-era signals from ~19% to ~90% (§20.3.b). `N = 4` weeks (monthly
momentum). Active `[effective_from, effective_to]` windows gate which
constituents form each week's reference distribution.

**(c) §13 — labels out of the vector.** Labels live in a separate `signal_labels`
table (migration 0041), never in the 217 JSONB. Directional convention:
`BUY`/`EXIT_FROM_SELL` → +1, `SELL`/`EXIT_FROM_BUY` → −1; the quality class is
judged on the directional return. `horizon_complete` gates training rows.
**Thresholds are untuned placeholders** (`RET_VERYGOOD=0.10`, `DD_VERYGOOD=0.08`,
`RR_VERYGOOD=1.5`, `DD_BAD=0.15`) — deliberately parked for a separate
results-table tuning pass, so the split in §20.3.a should be read as a sanity
check, not a calibrated target.

**(d) §16 — sector lead/lag.** Built as **column-fills on `sector_stock_leadlag`**
(not a new table), with `is_constituent` added (migration 0043). Both windows
(52w, 260w) stored as **separate rows**; the `window` column therefore does
double duty (`4` = `rs_rank`'s return window, `52`/`260` = §16's correlation
window). The §16.5 at-signal features read the `window=52` row via read-time
join — they are **not** copied into the JSONB (deviates from §19's "copied into
the signal row" — see (g)). Debias, ±4 lag scan, `2/√n_obs` floor, and
LEADER/LAGGARD/COUPLED/DECOUPLED all per spec.

**(e) §17 — regime matrix + ensemble.** Regime proxy = **breadth** (mean
constituent `display_regime`; the spec's *best* option). Market factor = the
**cross-industry mean** (not NIFTY). The ensemble was materialized under **three
(θ, M) variants** — `floor` (2/√n, M=10), `mid` (max(2/√n, 0.15), M=10),
`strict` (0.25, M=8) — rather than one, so a model can learn which coupling
strictness is most predictive. A **single current matrix snapshot** is stored.
The four §17.5 features are materialized per signal **per variant** in
`signal_ensemble_features`.

**(f) §19 — persistence grain.** Kept signal-grain, but the 194 causal features
live in a **JSONB `features` column** rather than 194 physical columns (cleaner
migrations; the manifest is logical, not a table DDL). Tier-3 features are **not
copied into the signal row** — they stay in their weekly tables and are joined
at read-time. This is a deliberate departure from §19's "looked up and copied
into the signal row": copying would re-introduce the weekly→signal cadence
coupling the tiering exists to avoid, and duplicate ~10 columns × 3 variants
across 45k rows for no benefit over a keyed join.

**(g) Manifest reconciliation, as-built.** 217 = **194** JSONB (191 assembler +
`vol_at_pivot` + `accumulation` + `rs_ema`) **+ 13** already-joined (11 from
`stock_indicator_state`, 2 pierces from `stock_indicator_enrichment`) **+ 10**
Tier-3 (`rs_rank` ×1, §16 ×4, §17.5 ×5). The manifest's group boundaries (S/T/…)
are preserved; only the physical home changed.

## 20.3 Data findings & commentary

### 20.3.a Labels — the training target's shape
Full NSE, `horizon_complete`:

| class | count | pct |
|---|---|---|
| BAD | 27,477 | 62.6% |
| GOOD | 12,339 | 28.1% |
| VERY_GOOD | 4,047 | 9.2% |

By signal type (BAD / GOOD / VERY_GOOD): BUY 11,827 / 5,025 / 2,227 ·
SELL 12,329 / 5,822 / 1,337 · EXIT_FROM_BUY 1,656 / 761 / 202 ·
EXIT_FROM_SELL 1,665 / 731 / 281.

**‹comment›** The ~63/28/9 split is healthy for an untuned labeller — most
signals *don't* lead to a large clean directional move over 13 weeks, which is
exactly what you'd expect and why a quality model is worth building. The
BUY-vs-SELL asymmetry in VERY_GOOD (2,227 vs 1,337) is real economics, not a
bug: over 2004–2026 (a broadly rising market) long setups produced clean moves
more often than shorts. This is a *sanity* distribution — with thresholds
untuned, don't read 9.2% VERY_GOOD as a calibrated base rate.

### 20.3.b `rs_rank` — the coverage decision paid off
Constituents-only first cut: 264,623 rows / 1,143 tickers / 93 industries,
mean rank 0.551, but only **~19%** of index-era signals joined (constituents are
a minority of tradeable stocks). After coverage (b) — ranking all industry
stocks against the constituent set — the at-signal join reached **18,503 signals,
89.5% of index-era, mean rank 0.525.**

**‹comment›** A relative-strength feature present on only a fifth of signals is
near-useless to a model; 90% coverage makes it a first-class input. The mean
sitting slightly above 0.5 (0.525–0.551) is expected, not a bug: it's a
percentile, so it *must* center near 0.5, and the small upward bias comes from
survivorship (tickers with full history skew mildly positive) plus ties
resolving upward (`≤` in the rank). The ~10% still-uncovered are correct NULLs —
pre-2020 (before the index `base_date`) or industries with no reference set.

### 20.3.c §16 — the debias signature is unmistakable
`sector_stock_leadlag` by window:

| window | rows | has_corr | mean_insync | constituent_rows |
|---|---|---|---|---|
| 4 (rs_rank) | 579,885 | 0 | — | 0 |
| 52 | 462,987 | 462,987 | 0.376 | 207,087 |
| 260 | 96,309 | 96,309 | 0.373 | 40,536 |

Coupling (window 52): COUPLED 300,382 · DECOUPLED 90,590 · LAGGARD 38,519 ·
LEADER 33,496. At-signal join (window 52): 15,026 signals, **72.3%** of
index-era, mean insync 0.383.

**‹comment›** `mean_insync ≈ 0.376` is the tell that the leave-one-out debias is
*working*. Without it you'd see ~0.7+ (a stock is mechanically correlated with an
index it's part of); with it you get the genuine co-movement after the
self-inclusion is removed — positive (stocks do follow their sector) but not
degenerate. The coupling mix (~65% COUPLED, ~20% DECOUPLED, LEADER≈LAGGARD) is
economically sane and, crucially, LEADER and LAGGARD are near-symmetric, which is
what an unbiased lead/lag estimator should produce. The 72.3% coverage (vs
`rs_rank`'s 89.5%) is the price of a 52-week correlation warm-up — signals in a
stock's first index-era year legitimately have no correlation history yet.

### 20.3.d §17 — the matrix is textbook-clean
`industry_regime_corr` (window 260): **8,556 directed pairs** (= 93×92, complete),
93 industries, **mean_corr = −0.012**, **mean_abs_corr = 0.196**, and
**leads = lags = 3,885** with 786 contemporaneous.

**‹comment›** Three things here are the good kind of boring. `mean_corr ≈ 0`
proves the §17.2 market-factor removal centered the distribution — the
"everything's bullish in a bull market" factor is gone. `mean_abs_corr ≈ 0.20`
says real structure survives without a common factor dominating. And
`leads == lags` *exactly* (3,885 = 3,885) is the strongest possible check on the
sign convention: the matrix must be anti-symmetric in `best_lag` (if j leads i,
then i lags j), and it is, to the row. If that had been asymmetric I'd have
suspected a shift-direction bug immediately.

### 20.3.e §17 — ensembles, and a redundancy worth noting
`influential_industry_ensemble`: `floor` 922 members, `mid` 922, `strict` 743;
leaders 326/326/270; counter-movers 491/491/400. Breadth series: 26,850 rows,
93 industries, mean 0.032, spanning 2020-03-30 … 2026-07-06.

**‹comment›** `floor` and `mid` came out **identical** (922 each). Not a bug: the
M=10 cap selects each industry's top-10 strongest couplings, which all clear both
thresholds (0.124 and 0.15), so the [0.124, 0.15) band only holds rank-11+
members that are capped out anyway. The variants would diverge only if M rose or
an industry were sparsely coupled. Harmless to keep, but if trimming, drop `mid`
or raise its M. More interesting: **counter-movers (491) outnumber co-movers
(431)**. That's a feature of *residualized* regime correlation — once the common
market factor is removed, sector rotation shows up as anti-correlation (money
leaving one sector for another), so negative couplings are expected to be common.
Breadth mean ≈ 0.03 (near zero, slight bull tilt) confirms the regime proxy is
balanced over time rather than stuck.

### 20.3.f §17.5 — the payoff: the ensemble carries real signal
`signal_ensemble_features` per variant: rows 18,808 (floor=mid), 18,808 (strict);
confirm_rate ≈ 0.506/0.503; mean_score ≈ 0.02/0.05; mean_agreement ≈ 0.457.
The discriminating test (`mid`, horizon-complete signals joined to labels):

| `ensemble_confirms` | n | GOOD+VERY_GOOD | VERY_GOOD | BAD |
|---|---|---|---|---|
| false | 8,660 | 36.9% | 9.0% | 63.1% |
| true | 8,845 | **40.4%** | **10.7%** | **59.6%** |

**‹comment›** This is the whole point of §11→§17 justifying itself. A confirming
industry-regime ensemble lifts the good-outcome rate by **+3.5pp** and VERY_GOOD
by **+1.7pp**. With ~8.8k signals per group the standard error on a ~40% rate is
~0.5pp, so a 3.5pp separation is roughly **4–5σ** — not noise. The
`confirm_rate ≈ 0.50` and `mean_score ≈ 0` confirm the feature is *balanced*
(it's not trivially predicting "everything up"), so the lift is genuine
discrimination, not a base-rate artifact. It's a modest edge in absolute terms —
this is one feature family among ~217, not a standalone strategy — but it's a
real, measurable, independent signal, which is exactly what you want feeding a
quality model.

## 20.4 Bugs found & fixed (hard-won, worth remembering)

- **`window` is a reserved word in PostgreSQL.** Unquoted in raw SQL it's a
  parser error. Bit us **twice** — first in the `rs_rank` INSERT, then again when
  the §16a repo overwrite silently reverted the fix (the overwrite was rebased
  from a stale local copy that predated the quote-fix). Lesson: *quote reserved
  identifiers* (`"window"`, and screen `order`/`user`/`value`), and *rebase every
  file overwrite from the live tree, never a local canonical.*
- **tz-boundary `.date()` → Sunday.** Weekly bar timestamps are Monday-00:00-IST
  stored as UTC (Sunday 18:30 UTC), so `.date()` on the raw instant yields
  **Sunday**, one day off from `date_trunc('week')`'s Monday — every read-time
  join returned 0 rows. Fix: `ts.astimezone(session.zone).date()` before storing
  any "week date." This hazard recurs anywhere a week key is derived from these
  timestamps (§16/§17 all convert to IST before `.date()`).
- **`run_phase` marked FAILED-as-COMPLETE.** A phase whose runner returned a
  `FanOutOutcome` with failed items was still recorded COMPLETE (it returned
  without raising), so a resume *skipped* it — which is precisely how the first
  `window` bug "silently completed." Fix: duck-type on `.failed`; non-empty →
  record FAILED (resume retries) without raising (backfill continues). The
  pipeline is now fail-loud across every fan-out phase.

## 20.5 Physical layout & the training-matrix join

```
signal_enrichment.features   JSONB, 194, feature_spec_version='v0.4'
  191 assembler  +  vol_at_pivot, accumulation (C3)  +  rs_ema (§11a merge)

sector_stock_leadlag         (ticker_id, asof_date, "window")
  window=4  : rs_rank                                        (§11b)
  window=52 : insync_corr, best_lag, lead_lag_corr,
              beta_sector, coupling_class, is_constituent    (§16, primary)
  window=260: same, 5y                                       (§16)
  join: ticker_id + asof_date=date_trunc('week',signal_date)::date + "window"

signal_ensemble_features     (signal_enrichment_id, variant)
  ensemble_score, ensemble_confirms, ensemble_agreement,
  n_leaders_bull, n_leaders_bear                             (§17.5)
  join: signal_enrichment_id + variant

industry_regime_corr / industry_regime_breadth /
influential_industry_ensemble  — FEED §17.5; not joined to signals directly

signal_labels                (signal_enrichment_id)          ← targets, not features
  fwd_ret_4w/8w/13w, fwd_max_dd, fwd_rr, quality_class, horizon_complete
```

Full training matrix (note the **two** `sector_stock_leadlag` joins — `rs_rank`
at window 4, §16 at window 52):

```sql
SELECT e.signal_enrichment_id, e.ticker_id, e.signal_date, e.signal_type,
       e.features,                                          -- 194 (JSONB)
       rr.rs_rank,                                          -- §11b
       s16.insync_corr, s16.best_lag AS sector_lead_lag,
       s16.beta_sector, s16.coupling_class, s16.is_constituent,   -- §16
       sef.ensemble_score, sef.ensemble_confirms, sef.ensemble_agreement,
       sef.n_leaders_bull, sef.n_leaders_bear,              -- §17.5
       l.quality_class, l.fwd_ret_13w                       -- targets
FROM signal_enrichment e
LEFT JOIN sector_stock_leadlag rr
  ON rr.ticker_id=e.ticker_id
 AND rr.asof_date=date_trunc('week',e.signal_date)::date AND rr."window"=4
LEFT JOIN sector_stock_leadlag s16
  ON s16.ticker_id=e.ticker_id
 AND s16.asof_date=date_trunc('week',e.signal_date)::date AND s16."window"=52
LEFT JOIN signal_ensemble_features sef
  ON sef.signal_enrichment_id=e.signal_enrichment_id AND sef.variant='mid'
LEFT JOIN signal_labels l
  ON l.signal_enrichment_id=e.signal_enrichment_id;
```

## 20.6 Known caveats & future work (none blocking)

1. **§17 look-ahead.** A single *current* matrix snapshot is stored, so historical
   signals read present-day correlations. Regime correlations are slow-moving so
   the bias is mild, but for clean point-in-time training the matrix should be
   recomputed per-week-historical (≈260× the compute; the spec's "store the whole
   matrix" chose the snapshot). Until then, treat §17.5 features on old signals as
   mildly optimistic.
2. **§16 recompute cost.** The 52w+260w correlations over full history × all
   industry stocks is the heaviest phase (~17 min). An incremental mode (recompute
   only recent weeks; old correlations don't change) would cut it sharply.
3. **Significance floor under a lag scan.** `2/√n_obs` is the single-correlation
   threshold; scanning 9 lags and taking the max inflates false positives
   (multiple comparisons). Spec-literal for now; a Bonferroni/√(scan) adjustment
   would tighten DECOUPLED classification.
4. **`floor` ≡ `mid` ensemble variants** (§20.3.e) — currently identical; drop one
   or re-tune M if the storage/compute matters.
5. **Label thresholds untuned** (§20.2.c) — parked for the results-table tuning
   pass; the §20.3.a split is a placeholder, not a calibrated target.
6. **`rs` structure member deferred** (§20.2.a) — if `rs_pattern`/`rs` lag/pdisp
   prove wanted, they slot into the Tier-2 cross-entity layer via the existing
   structure engine on the `rs` series.
