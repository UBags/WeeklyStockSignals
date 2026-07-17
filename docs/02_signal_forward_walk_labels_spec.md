# Signal Forward-Walk Labels — Requirements, Design & Implementation Plan

**Project:** WeeklyStockSignals (`trip_ema`)
**Purpose:** produce a rich, per-signal outcome table to train an ML classifier that grades each signal (great / good / bad).
**Status:** design locked; ready to implement. This document is self-contained for a fresh implementation conversation.
**Upstream dependency:** clean, corporate-action-adjusted daily bars — see companion doc *OHLCV Correctness & Corporate-Actions Self-Adjustment*. The walk must run on the corrected/self-adjusted series, not raw yfinance bars.

---

## 1. Goal

For every ticker **BUY** and **SELL** signal, walk the subsequent daily bars and record how the trade would have played out — profit/loss thresholds hit, excursions, realized outcome, and context-relative performance. These become the feature/target set from which a downstream model derives a great/good/bad label. This table stores the *measured facts*; the class label itself is derived elsewhere.

The old `quality_class` column (a weekly, fixed-13-week, close-only rule) is **dropped** — it is not the target `y` and does not match this daily, regime-bounded definition.

---

## 2. Trade model (locked semantics)

- **Which signals get a walk:** every `BUY` and `SELL`. `EXIT_FROM_BUY` / `EXIT_FROM_SELL` do **not** get their own walk rows; they only ever act as a window terminator.
- **Direction:** BUY ⇒ **long**; SELL ⇒ **short**.
- **Bars are DAILY.** The signal engine is a weekly-chart regime recomputed each day (`historical_signals` is daily-grain; a signal fires on `signal_date` when the weekly regime differs from the prior day). The walk is on **daily** `ohlcv_bars` (`timeframe='1D'`), driven off `signal_date`. Never use `bar_timestamp` (it is the IST-Monday week anchor, shared across the week) — always `signal_date`.
- **No lookahead:** the signal is known at the close of `signal_date`; entry is the next day's open.
- **Entry:** the **OPEN** of the first trading bar strictly after `signal_date`.
- **Window end `N`:** the **next signal's `signal_date`** for that ticker (every `historical_signals` row is a regime change by construction, so "next signal" = "next regime change"; `EXIT_FROM_*` only matters when it *is* that next signal). If there is no next signal, the window runs to the last available bar and is **censored**.
- **Exit:** the **CLOSE** of the window-end day `N` (`exit_reason='NEXT_SIGNAL'`), or the last available close when censored (`exit_reason='DATA_END'`, `window_censored=true`).
- **Excursion scan window:** daily bars `[entry_day … N]` inclusive.
- **Bar indexing:** signal day = **bar 0**, entry day = **bar 1**. `bars_to_* = hit_bar_index − 1`, so the entry bar is distance **0**. `-1` = never hit within the window.

**Intrabar, direction-aware touch detection.** A threshold is "hit" when any bar's intraday extreme reaches it:
- long: **gains** checked on each bar's **high**, **losses** on each bar's **low**;
- short: **gains** on each bar's **low**, **losses** on each bar's **high**.

Because each threshold is an independent "ever touched within window" flag, intrabar ordering (did +5% or −5% touch first inside one bar?) is unknowable and does not matter.

**Realized return** (signed, positive = profit):
- long: `exit_close / entry_open − 1`
- short: `entry_open / exit_close − 1`

---

## 3. Data model

Extend the existing **`signal_labels`** table (one row per signal, 1:1 with `signal_enrichment` via `signal_enrichment_id`; PK `signal_label_id`). A migration (`0054_signal_label_forward_walk`, already drafted) drops `quality_class` (+ its CHECK + index) and adds the columns below. The weekly `fwd_ret_4w/8w/13w`, `fwd_max_dd`, `fwd_rr` columns are retained for now (feature-only; recommend a later dedicated commit to retire the weekly labeler if they aren't wanted).

Everything the walk writes is gated by **`forward_walk_done`** (boolean, default false): rows predating the walk read false and their `hit_/bars_` columns are placeholders. Consumers filter `WHERE forward_walk_done`.

**Bookkeeping**

| column | type | meaning |
|---|---|---|
| `forward_walk_done` | bool NOT NULL | walk has written this row |
| `side` | varchar(5) | LONG / SHORT |
| `entry_date` | date | first trading day after `signal_date` |
| `entry_open` | float | entry fill (adjusted open of entry bar) |
| `exit_date` | date | window-end day `N` |
| `exit_close` | float | exit fill (adjusted close of `N`) |
| `exit_reason` | varchar(16) | NEXT_SIGNAL / DATA_END |
| `bars_in_window` | int | trading bars in `[entry … N]` |
| `realized_ret` | float | signed directional return |
| `window_censored` | bool NOT NULL | window ran to data end |

**Gain rungs (21)** — for each `n` in `1,3,5,7,9,11,13,15,17,19,21,23,25,27,29,31,33,35,50,75,100`:
`hit_gain_<n>_pct` (bool) + `bars_to_gain_<n>_pct` (int, −1).

**Loss rungs (6)** — for each `n` in `5, 7_and_half, 10, 12_and_half, 15, 20`:
`hit_loss_<n>_pct` (bool) + `bars_to_loss_<n>_pct` (int, −1).

**Excursions & extras**

| column | type | meaning |
|---|---|---|
| `ever_in_profit` | bool | favorable intrabar move > 0 on any bar incl. entry |
| `mae_pct` / `mae_bar` | float / int | max adverse excursion (positive magnitude) + bar |
| `mfe_pct` / `mfe_bar` | float / int | max favorable excursion + bar |
| `mae_before_mfe` | bool | did MAE occur before MFE (heat before run) |
| `excess_ret_industry` | float | `realized_ret` − industry index return over window |
| `excess_ret_market` | float | `realized_ret` − market return over window |
| `mfe_atr20` / `mae_atr20` | float | excursion in ATR(20) units (`pct·entry_open/atr20`) |
| `time_underwater_bars` | int | bars closing adverse vs entry |
| `slope_per_bar` | float | OLS slope of close over window (normalized) |
| `realized_vol` | float | stdev of daily returns in window |
| `sharpe_in_window` | float | mean daily ret / stdev |
| `capture_ratio` | float | `realized_ret / mfe_pct` (≤ 1; NULL if mfe=0) |

Bars/bools are NOT NULL (`-1`/false defaults); float metrics are nullable (NULL = genuinely no value). `atr10..atr40`, `signal_price`, `industry_id` are already on `signal_enrichment`; industry/market series come from `industry_index_value` / `market_overview_daily`.

**Data-quality gate.** Any walk window containing a `step_suspect` / demerger / dropped-bad-tick region (from the OHLCV corrector) is marked `window_censored` rather than emitting fabricated excursions. Bad-tick/phantom bars are already absent (dropped upstream); demerger windows are censored.

---

## 4. Design

**Window derivation.** Per ticker, load `signal_enrichment` rows ordered by `signal_date`. For each BUY/SELL row, window end = the immediately following row's `signal_date`; last signal ⇒ open window ⇒ data end / censored. (This mirrors the proven per-ticker consecutive-pair pattern in `bulk_daywalk.py`; note the historical `bar_timestamp` week-anchor trap that once silently dropped 79.5% of signals — always key on `signal_date`.)

**Pure walk function** — `walk_signal(entry_open, bars, side, ...) -> ForwardWalkResult`. Inputs: entry open, the adjusted daily bars for `[entry … N]`, side. Contains all threshold/MFE/MAE/excess-return/slope logic. Pure, no I/O — the correctness core, most heavily tested.

**Per-ticker runner.** Loads the ticker's signals + its adjusted 1D bar series once, pairs consecutive signals, slices each window in memory, calls `walk_signal`, returns rows. Reads via `trip_gui_user`. Does **not** write.

**Orchestration.** `ProcessPoolExecutor(max_workers = round(2/3 × physical_cores))` = 4 on the NUC (CPU-bound numpy per ticker → processes over threads, matching the project's process-boundary convention). `imap_unordered` over `sorted(ticker_ids)` — each worker pulls one ticker at a time. **Single writer:** the main process collects returned rows and bulk-upserts into `signal_labels` on `signal_enrichment_id` via `trip_etl_user`. Clean role split (workers read, main writes), no cross-process session sharing, re-runnable, `computed_at` bumped.

**Timezone/bars.** Fetch `ohlcv_bars WHERE timeframe='1D'`; derive the trading day via `AT TIME ZONE 'Asia/Kolkata'`; drive off `signal_date`.

---

## 5. Implementation plan

1. **Migration `0054`** (drafted): drop `quality_class` + CHECK + index; add the ~79 columns above (rung lists generated in the migration). Regenerate `DDL.txt` via the standard `pg_dump` after `alembic upgrade`. *Ordering:* a preceding commit must stop the code writing `quality_class` (domain dataclass field, `compute_label`, runner, repo SQL, tests) before the column is dropped, else the labeler INSERT breaks.
2. **Pure `walk_signal`** + exhaustive unit tests: long & short; the "3/5/7/9 all hit on bar 2 ⇒ distance 1" case; `-1` defaults; censored window; MAE/MFE bars; `mae_before_mfe`; capture ratio; a window abutting a censored/demerger region.
3. **Per-ticker runner** (window pairing + single bulk bar fetch + in-memory slicing).
4. **Orchestration** (ProcessPool, ticker sharding, single writer) + integration tests.
5. **CLI / wiring:** run after signal generation (and after the OHLCV correction pipeline has produced adjusted bars). Admin route optional.

Discipline: compile → import-test → run affected suite; small green commits, one logical change each.

---

## 6. Locked decisions (do not re-litigate)

- Entry = open of first bar after `signal_date`; exit = **close of window-end day `N`**.
- Window = consecutive signals; only BUY/SELL get walk rows.
- Bar indexing: signal=bar 0, entry=bar 1, distance = bar_index − 1, entry distance 0, `-1` = miss.
- Intrabar, direction-aware touch (long: gains on high / losses on low; short: mirrored).
- Gain rungs (21) and loss rungs (6: incl. `7_and_half`, `12_and_half`) as listed; bool + `bars_to_*` per rung.
- `quality_class` **dropped**; the target `y` is derived downstream from these metrics.
- Extras approved: excess-return vs industry & market, ATR-normalized MFE/MAE, `mae_before_mfe`, time-underwater, in-window slope/vol/Sharpe, capture ratio. Triple-barrier **dropped**.
- Walk runs on corrected, self-adjusted daily bars; demerger/step-suspect windows are `window_censored`.

## 7. Open / downstream

- The great/good/bad label definition (thresholds on `realized_ret` / `mfe` / excess-return, or a learned model) is a **separate** downstream task consuming this table.
- Weekly `fwd_ret_*`/`fwd_rr` columns are retained as optional features; retiring the weekly labeler is a separate commit.
- Demerger windows remain censored until scheme ratios are sourced (see companion OHLCV doc).
