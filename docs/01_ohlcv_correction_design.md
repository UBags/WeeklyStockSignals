# OHLCV Correctness & Corporate-Actions Handling

**Project:** WeeklyStockSignals (`trip_ema`)
**Status:** design agreed; implementation pending
**Governing invariant:** wrong data → wrong decisions → loss of money. Correctness beats speed on the price path, always.

---

## 1. Why this exists

The signal service and the forthcoming ML label table both read `ohlcv_bars`. An investigation into anomalous overnight price jumps found the daily bars unreliable for NSE in several ways, each of which silently corrupts excursions, returns, and regime signals. This document defines how we source prices, verify corporate-action alignment, correct the defects, and keep the series stable across rebuilds.

---

## 2. What we found

A twelve-name deep dive plus a full-universe reconciliation (2,688 active NSE tickers) against the authoritative NSE corporate-actions feed exposed three independent failure modes:

**A. Corporate-action adjustment is mostly right, wrong on a minority.** yfinance always split-adjusts NSE prices at source (verified — holds for `auto_adjust` in both modes). It adjusts the majority correctly (~1,292 of ~1,364 splits `ADJUSTED_OK`). The defects:
- **~68 `BOTCHED`** — mis-adjusted: reverse seams (HTMEDIA shows a 5× *up*-step at a 5:1 split; SUVEN's 2007 split×bonus over-scaled the pre-event leg), demergers mistaken for splits (HGS ~16× drop).
- **~4 `SEAM_PRESENT`** — metadata missed the event, leaving a raw unadjusted step (DABUR 2007 3:2, IVC, NBIFIN).
- **231 demergers** — no yfinance data at all (SKFINDIA, STAR, TMPV 2025); appear as unexplained persistent steps.
- **~125 `NO_BARS`** — splits predating stored history; irrelevant unless signal history reaches them.

**B. Bad ticks.** Single-bar values wildly off and reverting next bar — THYROCARE `482` between `157`s, TIL `148/525` flip-flop, TEXRAIL `156.28` above its own high (an OHLC-invariant violation). Source errors, unrelated to corporate actions.

**C. Phantom bars.** Fabricated/stale rows — SWANDEF `632.50` on zero volume, SILVERTUC bars on market holidays.

---

## 3. Decisions

### 3.1 Source: yfinance stays the single source of truth (Option A)
Sourcing raw prices from NSE bhavcopy (via `nselib`) and self-adjusting was **rejected on cost** — ~12 min per ticker-year, untenable for 2,688 tickers × 20 years, twice a month. yfinance also carries adjacent data we rely on. So yfinance stays the price source; we correct its defects rather than replace it.

### 3.2 `auto_adjust=False`
yfinance applies **split** adjustment in both modes; `auto_adjust` only toggles **dividend** adjustment and the `Adj Close` column. We use **`auto_adjust=False`**: `close` = split-adjusted only; `adj_close` = dividend-adjusted, parked. Rationale: **Pine/TradingView fidelity** (the ported strategy's basis is split-adjusted, dividends off); **stability** (dividend back-adjustment re-scales all history on every dividend → signal churn between rebuilds; `auto_adjust=False` moves only on splits); **faithful excursions** (dividend-adjusted series fabricate an ex-date dip the holder never suffered); **recoverable** (`Adj Close`, `Dividends`, `Stock Splits` still returned → total-return remains buildable later). Schema already fits: `ohlcv_bars.close` = split-adjusted (signals + walk), `ohlcv_bars.adj_close` = dividend-adjusted (parked).

*Consequence for corrections:* under `auto_adjust=False`, `close` carries **split** seams only — dividends never create a step — so every seam the corrector sees is a split/bonus scale event or a data error, never a dividend artifact.

### 3.3 Correction, not self-adjustment
yfinance already split-adjusts correctly ~95% of the time and no raw series is available, so we do not re-adjust splits wholesale. We **reconcile** against the authoritative nselib actions, then apply the **exact-factor correction** of §6.1 to the defective minority, and **censor** only what remains genuinely ambiguous.

---

## 4. The nselib corporate-actions table

`nselib.capital_market.corporate_actions_for_equity(from_date, to_date)` → the full NSE feed (40,198 rows / 2,960 symbols for 2006→2026). Parsed `subject` free-text:

| action_type | count | ratio | price factor `f` (applied to pre-boundary bars) |
|---|---|---|---|
| `split` | 710 | `Fv old→new` | `new/old` (10→2 ⇒ 0.2) |
| `bonus` | 725 | `Bonus a:b` | `b/(a+b)` (1:1 ⇒ 0.5, 1:2 ⇒ 0.667) |
| `demerger` | 231 | — | — |
| `dividend` | ~23,900 | `Div-N%` / `Rs N` | n/a |
| `bonus_other` | 4 | DVR/pref/debenture | flagged, not an equity factor |

Parser: 6 unparsed of 40,198. Key on **`isin` → `tickers.isin`** (symbols get renamed/reused). Store in `corporate_actions` (`action_type`, `effective_date`, `ratio_numerator/denominator`, `amount`, `source='nselib'`, `status`, `raw_payload=subject`). Add UNIQUE `(ticker_id, action_type, effective_date, md5(raw_payload))` for idempotent monthly upserts (NSE revises — payload hash separates revision from duplicate).

**Three jobs:** (1) reconciliation & correction key (§5–6), (2) demerger date list for censoring, (3) dividend payouts + `adj_close` for GUI/total-return.

---

## 5. Reconciliation (authoritative = nselib)

Per nselib split/bonus, compare the yfinance-adjusted series across the boundary:

```
R = level_after / level_before            (robust medians, §6.1)
|R − 1|   ≤ tol → ADJUSTED_OK
|R − f|   ≤ tol → SEAM_PRESENT
|R − 1/f| or |R − f²| or |R − 1/f²| ≤ tol → BOTCHED (a scale-error signature)
matches no f-signature → UNEXPLAINED (real move / demerger / misdate) → censor
before/after missing → NO_BARS
```

The earlier full run (keyed off yfinance's own `.splits`) gave `{ADJUSTED_OK: 1292, BOTCHED: 68, SEAM_PRESENT: 4, NO_BARS: 125}`. The **authoritative** reconcile keys off **nselib** (adds the bonuses yfinance mislabels and the demergers it lacks) and runs on the `auto_adjust=False` series — re-run it at implementation; split verdicts carry over since both `auto_adjust` modes split-adjust identically.

---

## 6. Correction policy

Per ticker, in order:

| situation | detection | action |
|---|---|---|
| **Bad tick** | structural invalidity or spike-revert vs a stable local level | **drop** |
| **Phantom** | zero/NaN volume, or bar on a non-trading day | **drop** |
| **ADJUSTED_OK** | R ≈ 1 | leave |
| **SEAM_PRESENT / BOTCHED (scale-error signature)** | R matches an `f`-derived signature | **correct** — snap-to-factor splice (§6.1) |
| **UNEXPLAINED step** | R matches no `f`-signature; nselib=demerger or no action | **censor** the crossing window (real value transfer or real move) |

Non-negotiable money rules: **never interpolate a dropped bar** (fabricated H/L manufacture false MFE/MAE); **never apply a multiplier the known factor cannot explain** (that is the line between fixing a scale error and inventing a price).

### 6.1 Correction methodology (detail)

**Principle.** A pure split/bonus is value-neutral: a correctly adjusted price series is **continuous** across it. So for every split/bonus boundary the target is known a priori — continuity — and the exact factor `f` is known from nselib. We exploit both.

**Step 1 — locate the true boundary `B`.** Use yfinance's own split marker (`Stock Splits` column / `.splits`) nearest the nselib `effective_date`; fall back to `effective_date` if absent. This avoids false positives from `effective_date` (ex/record/announcement) vs the date yfinance actually applied the adjustment.

**Step 2 — measure the seam `R`.** `R = median(close, [B+1 … B+w]) / median(close, [B−w … B−1])`, with a short window `w` (e.g. 3) that **excludes the boundary bar itself**. Medians dampen single-bar bad ticks and the split-day's own real return; excluding `B` keeps `R` a pure scale ratio, not a one-day return. Require both windows to be internally stable (low dispersion, no dropped bars) — if not quiet, censor.

**Step 3 — classify against `f`-signatures.** Build the candidate set `{1, f, 1/f, f², 1/f²}` (extend if triple-application is ever seen). Snap `R` to the nearest signature within tolerance `tol` (default 10% relative):
- nearest is `1` → **ADJUSTED_OK**, no-op.
- nearest is `f` → **SEAM_PRESENT** (yfinance skipped it; pre-leg at old scale).
- nearest is `1/f`, `f²`, or `1/f²` → **BOTCHED** scale error.
- **no** signature within `tol`, or **two** signatures within `tol` (ambiguous) → **UNEXPLAINED** → censor.

**Step 4 — apply the exact correction.** Multiply every bar with `date < B` by the **snapped signature value `s`** (the exact `f`-derived number, *not* the measured `R`):

```
close(d), open(d), high(d), low(d)  ×= s     for all d < B
volume(d)                           ×= 1/s   (optional, for share-count consistency)
```

Because `s` is the exact `f`-derived value, measurement noise and small real-move drift in `R` do not enter the correction — `R` only selects *which* signature; `f` supplies the magnitude. Continuity at `B` is restored by construction. (Worked: HTMEDIA `R≈5.04`, `f=0.2` ⇒ nearest signature `1/f=5.00` ⇒ pre-leg `×5.00`; the botched `27.52` pre-leg lands on the `138.71` post-leg — continuous. SEAM_PRESENT DABUR `R≈0.705`, `f=0.667` ⇒ pre-leg `×0.667`.)

**Step 5 — multiple actions.** Process a ticker's boundaries **newest → oldest**; each boundary's multiplier applies to all bars before it, so multipliers compose multiplicatively down the history (standard cumulative back-adjust to the latest scale).

**Robustness caveat — small factors.** When `f` is near 1 (small bonuses: 1:5 ⇒ `f=0.833`, signatures `{1, 0.833, 1.2}` bunch together), a genuine ~15–20% move can impersonate a botch. For `f ∈ (~0.7, ~1.4)` require a **tighter tolerance** (e.g. 4%) or route to **manual review**. Large splits/bonuses (5:1, 10:1, 1:1) have widely separated signatures and auto-correct unambiguously.

**Idempotency.** Corrections are **never** written back in place. The twice-monthly rebuild re-fetches from yfinance and re-derives every correction deterministically from `corporate_actions`, so re-runs cannot double-apply.

**Verification.** After correction, the seam ratio at each `B` must be `≈ 1` (medians); and a ticker's cumulative applied multiplier should equal the product of its snapped signatures — any residual unexplained step is a bug or a missed action.

**What still gets censored (never spliced):** demerger boundaries (real value transfer — continuity is *wrong* there), steps matching no `f`-signature (real moves like SUZLON's 2008 crash), ambiguous small-`f` matches pending review, and windows around dropped bad-tick regions.

**Corrector module.** The current `ohlcv_corrector` prototype implements only the blind `pre × (den/num)` path — correct for `SEAM_PRESENT` but wrong if applied to an already-adjusted or reverse-adjusted seam (double-adjust). It must be upgraded to the measure→classify(snap)→apply pipeline above: a `reconcile_and_correct(series, actions)` that, per action, measures `R`, snaps to an `f`-signature, and either applies the exact multiplier, no-ops, or emits `censor` for the crossing window. Bad-tick/phantom dropping (already correct in the prototype) is unchanged and runs first.

---

## 7. Pipeline integration

**Cadence:** full rebuild on the **1st and 3rd Saturday** of each month, self-triggered (GUI ad-hoc button later).

**Each run:** (1) refresh `corporate_actions` from nselib (2-month overlap upsert, or full re-fetch); (2) clear derived data; (3) fetch `auto_adjust=False` bars (`close`=split-adj, `adj_close`=div-adj); (4) drop bad ticks/phantoms; (5) nselib reconcile → snap-to-factor correct → censor the unexplained/demerger windows; (6) recompute signals → enrichment → labels (walk windows crossing a censored region → `window_censored`).

`auto_adjust=False` keeps this stable: only splits move history between rebuilds. **Cutover:** current data is `auto_adjust=True`; switching removes dividend adjustment (expected to move signals *closer* to TradingView). Gate it — backfill a sample with `auto_adjust=False`, confirm signals track TradingView better, then let the next full rebuild carry the cutover.

---

## 8. Code changes

- **Archive, don't delete** the `auto_adjust=True` fetch (`trip_ema/etl/yfinance_source.py`) to `archive/` (cf. `archive/backfill_daywalk_perday.py`) with an `ARCHIVED — silently dividend-adjusts` header; replace with the `auto_adjust=False` source (keep batching/throttle/repair; store `adj_close`).
- **New/promoted modules:** nselib actions loader + parser + monthly refresh; nselib reconcile (promote `reconcile_splits.py`, re-key to nselib); `ohlcv_corrector` → `trip_ema/domain/ohlcv_corrector.py`, upgraded to the §6.1 measure→classify→apply pipeline.
- **Scheduler:** twice-monthly rebuild (1st & 3rd Saturday).
- **Backfill/GUI:** wire correct → reconcile → correct/censor into `trip_ema.backfill.pipeline.backfill()`; surface dividend payouts (from `corporate_actions`) in the GUI.

---

## 9. Verification & tests

- **Done:** yfinance split-adjusts in both modes (`verify_auto_adjust_false.py`); Option B too slow; full reconcile characterises the backlog.
- Corrector unit tests on the real dirty data: THYROCARE bad tick dropped, TEXRAIL structural drop, SWANDEF phantom drop, **SUZLON real crash preserved (unexplained → censor, not spliced)**.
- §6.1 correction tests: SEAM_PRESENT (`R≈f` → pre×`f`, seam→1), reverse BOTCHED (HTMEDIA `R≈1/f` → pre×`1/f`, seam→1), multi-action composition, small-`f` ambiguity routed to review, UNEXPLAINED censored.
- Idempotent monthly upsert; reproducible full rebuilds (identical `ohlcv_bars`); cutover validation vs TradingView on a sample.

---

## 10. Open items / roadmap

- **Demerger ratios** (231) absent from the feed → censored until sourced from scheme documents.
- **Small-`f` bonus review queue** — the ambiguous-signature cases needing manual confirmation.
- **Illiquid pathological names** (TIL-class) — optional absolute-min-volume guard for the corrector.
- **Cross-sectional bad-day detection** (market-wide) — out of scope for the per-ticker corrector.
- **`bonus_other`** (DVR/preference/debenture) — bespoke handling if any affect tracked equities.
