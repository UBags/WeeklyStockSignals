# Daily-Grain `stock_indicator_state` — Refactor Signpost

**Branch base:** `architecture/clean-pipeline-v1`
**Head migration at start:** `0033_current_stage` → new work starts at `0034`
**Status legend:** ☐ not started · ◧ in progress · ☑ green+pushed

---

## 0. The one-paragraph statement of intent

`stock_indicator_state` (and its 1:1 `stock_indicator_enrichment`) today hold **one row
per completed weekly bar**, keyed `(ticker_id, timeframe, bar_timestamp)`. We are moving
to **one row per trading day**, keyed `(ticker_id, timeframe, date)`, where each day's row
holds the **weekly-chart indicator values as they stood at the close of that day** — i.e.
the still-forming weekly bar sampled daily. `bar_timestamp` stays on the row as the
IST-Monday week-anchor attribute (no longer unique). The weekly math is untouched; we
change only *what array we feed it per row* and *the persistence grain*.

---

## 1. The decisive architecture call (do NOT relitigate)

**Per-day recompute over a windowed weekly array — NOT a daily-anchored array.**

Why the daily-anchored array is wrong: `compute_bar_states` reads `series.emas.*.iloc[i-1]`
(indicator_primitives.py:903/907/911) for turn detection, and the VW-EMA/ATR recurrences
smooth across whatever bars they're handed. Interleaving 5 daily snapshots per week would
make the EMAs smooth day-over-day and fire `bars_since_turn`/`turn_buy` on daily deltas —
a *different, faster indicator*, not the weekly one sampled daily.

**Correct shape, per (ticker, day D):**
```
array = [completed weekly bars strictly before week(D)]  +  [one forming-week bar Mon→D]
run compute_state_primitives / compute_enrichment_primitives over array
keep ONLY the last row → that is day D's row (date = D, bar_timestamp = week_anchor(D))
```
Consequence that settles the FK question: the forming bar is bar N, its predecessor is the
last *completed week* = bar N-1, so enrichment's inter-bar quantities (`bars_since_*_turn`,
`pct_change_since_*_turn`, turn flags) stay **week-counted**. Enrichment therefore also goes
daily-grain and keeps weekly semantics → **both tables move together, FK re-points to
`(ticker_id, timeframe, date)`.**

Zero edits to `indicator_primitives.py` / `v1_compute_primitives.py` math. The change lives
entirely in (a) the runner's input construction and (b) the persistence grain.

---

## 2. Volume proration (the forming bar's only subtlety)

Proration is a property of the **synthesized forming-week bar only**. VW-EMA weights each
bar's price by that bar's volume (`vw_ema.py:145` `cv = close * smoothed_volume`), so a raw
partial week under-weights the current close vs how it lands on Friday.

- **Open week, day D (Mon..Thu):** `volume = sum(vol Mon..D) × expected_sessions / elapsed_sessions`.
  - Prorate on **trading-session count**, holiday-adjusted — pull `expected_sessions` from the
    actual session calendar for that week, else short weeks (Diwali etc.) over-project.
- **Last trading day of a *completed* week:** forming bar Mon→last-session with **real full
  volume, NO proration**. This bar *is* the canonical weekly bar → that day's row equals the
  Pine weekly value byte-for-byte → parity holds. Proration touches Mon..(last-1) only.
- Intra-week rows are a **new artifact with no TradingView reference** (TV shows raw partial
  volume mid-week). This is a deliberate product choice for a stable EMA; document it, don't
  chase TV mid-week behavior.

Helper to add: `prorate_week_volume(vol_so_far, elapsed_sessions, expected_sessions) -> float`.

---

## 3. Efficiency posture (correctness first, optimize only if it hurts)

Naive per-day recompute is O(days × weeks) per ticker. Mitigations, in order:
1. **Windowed array** — feed only the last ~60 weekly bars into each day's recompute (enough
   for longest-EMA + ATR warmup). Ship this. Bounds per-day cost to O(window).
2. **Snapshot/restore** — `vw_ema.py` has `snapshot`/`restore` (229/236); ATR mirrors it. The
   fast version primes to end-of-prior-week once, snapshots, replays the ≤5 forming bars with
   restore between. This is the incremental-object path; the V1 runner uses batch `compute_*`.
   **Do NOT do this now** — measure regenerate runtime first, reach for it only if painful.

Row-count / storage: ~5× growth (≈2,667 NSE × ~5 days/wk × history). Regenerate runtime ~5×.
Cleanest data path is **drop + full regenerate** via the existing recompute path (table is
fully derived) — no in-place `date` backfill.

---

## 4. Commit sequence

> Discipline: small green commits, one logical change each. Full `git add`/`commit -F -`/`push`
> block per step. Run `pytest` before starting new work (baseline was ~2,479 passed / 14 skipped
> / 0 failed). Commits **4 and 5 are the only non-mechanical ones** — everything else is
> grain-plumbing.

### ☐ Step 1 — Migration `0034` (schema only)
**Files:** `alembic_pkg/versions/0034_daily_grain_indicator_state.py`, `tests/persistence/test_migration_0034.py`, `alembic_pkg/versions/README.md` (catalogue line).
- `down_revision = "0033_current_stage"`. Match 0020/0021 idiom: raw `op.execute` DDL, string revision id, `upgrade()`+`downgrade()` both present.
- On **both** `stock_indicator_state` and `stock_indicator_enrichment`:
  - `ADD COLUMN date DATE NOT NULL` (on an empty/regenerated table; downgrade drops).
  - Drop PK; re-add `PRIMARY KEY (ticker_id, timeframe, date)`.
  - Keep `bar_timestamp TIMESTAMPTZ NOT NULL` as week-anchor attribute.
- Drop + recreate `stock_indicator_enrichment_state_fk` → `REFERENCES stock_indicator_state (ticker_id, timeframe, date)` ON DELETE CASCADE.
- Indexes: add `(ticker_id, date DESC)` and `(date DESC)`; keep/rename the old `bar_ts` ones as needed for week-grouping reads.
- Keep all three CHECKs (atr≥0, display_regime ∈ {-1,0,1}, EMAs>0).
- **Industry tables untouched** (`industry_indicator_state` / `_enrichment` stay weekly).
- Downgrade = drop-and-regenerate (irreversible-in-data; comment why).
- **Gate:** `test_migration_0034` green.

### ☐ Step 2 — Domain + repo, grain-agnostic (no behavior change)
**Files:** `trip_ema/domain/stock_indicator_state.py`, `trip_ema/domain/stock_indicator_enrichment.py`, `trip_ema/persistence/stock_indicator_state_repo.py`, `trip_ema/persistence/stock_indicator_enrichment_repo.py`, their repo tests.
- Add `date: date` to both dataclasses (update docstrings: PK is now `(ticker_id, timeframe, date)`).
- Repo (state): both `ON CONFLICT` targets → `(ticker_id, timeframe, date)`; add `date` to every INSERT column list + `:dt` bind in `_params`; add `date` to `_row_to_state` and **every** SELECT column list; flip `get_latest_for_ticker` / `get_at_or_before` / `get_history_for_ticker` `ORDER BY bar_timestamp DESC` → `ORDER BY date DESC`.
  - ⚠️ Bug spotted at `stock_indicator_state_repo.py:264` — `_validate(state)` is missing `@staticmethod` / `self`. Don't fix opportunistically here unless it's already broken; note it.
  - ⚠️ `delete_for_ticker` (129) has a **duplicated `DELETE FROM … WHERE …` body** in the SQL text — verify it's not a paste artifact that'll error; fix in this step if real.
- Repo (enrichment): mirror — `ON CONFLICT (…, date)`, `date` into INSERT/`_params`/`_row_to_enrichment`/`_SELECT_SQL`, `get_history_for_ticker` ordering → `date DESC`.
- Runner still writes one row/week with `date = week-end-session` (temporary) so this step stays behavior-neutral and green.
- **Gate:** `test_stock_indicator_state_repo`, `test_stock_indicator_enrichment_repo` updated (`_BT(n)` fixtures gain `date`) and green.

### ☐ Step 3 — Runner rewrite (the big mechanical one)
**Files:** `trip_ema/fundamentals/v1_runners.py` (state + enrichment populators), maybe a new `trip_ema/fundamentals/forming_bar.py` for the synth+prorate helper.
- Replace the single batch build (`v1_runners.py:164-205`) with the **per-day forming-bar loop**: for each trading day D in scope, assemble `[windowed completed weeks] + [forming bar Mon→D]`, run `compute_state_primitives`, take last row, set `date=D`, `bar_timestamp=week_anchor(D, tz='Asia/Kolkata')`.
- Add `prorate_week_volume(...)`; apply to Mon..(last-1); full real volume on week-end day.
- Reuse the **IST week-anchor helper** from `indicators/aggregation.py::_monday_of` / `daily_to_weekly` (already tz-correct) — do NOT hand-roll.
- Mirror for enrichment populator (same day loop, `compute_enrichment_primitives`, last row).
- Keep the windowing bound (~60 weeks) configurable.
- **Gate:** `test_v1_runners` row-count assertions flip weekly→daily grain; parity assertions (if any) must key on **week-end rows only**.

### ☐ Step 4 — `_slice_for_mode` incremental semantics ⚠️ real logic change
**Files:** `trip_ema/fundamentals/v1_runners.py` (`_slice_for_mode` + call sites), `tests/fundamentals/test_v1_runners_incremental.py`.
- Today "incremental" = "last N *weekly* rows" (`rows[-n_weeks_to_upsert:]`). It must become
  **"all day-rows of the current in-progress week"** — because each day's value *mutates* as the
  week fills (proration denominator changes, running H/L changes). Rewriting yesterday's rows on
  today's cycle is correct and required, not optional.
- Rename param concept (`n_weeks_to_upsert` → e.g. `refresh_scope`/`n_days`) OR keep the name but
  change semantics with a loud docstring. Prefer explicit rename to avoid silent drift.
- This is shared by all four populators — decide whether industry keeps weekly semantics (it does,
  since industry tables stay weekly) → the helper may need a grain flag, or split into two helpers.
- **Gate:** new incremental tests assert "current week's daily rows all rewritten; prior weeks untouched."

### ☐ Step 5 — `derive_signals` week-grain fix ⚠️ the landmine
**Files:** `trip_ema/orchestrator/derive_signals.py`, `trip_ema/persistence/stock_indicator_state_repo.py` (new method), `tests/orchestrator/test_derive_signals.py`, `test_derive_signals_as_of.py`.
- `derive_signals.py:137` reads `get_history_for_ticker(limit=2)` = "latest two rows". With daily
  grain that's **today + yesterday (same week) → same regime → signals basically stop**, and when
  they do fire it's on intra-week daily flips. Must reproduce the current week-over-week compare.
- Add repo method `get_latest_two_weekly(ticker_id, tf, *, as_of=None)`:
  `DISTINCT ON (bar_timestamp) … ORDER BY bar_timestamp DESC, date DESC LIMIT 2` (latest row per
  distinct week, top 2 weeks). Preserve the `as_of`/`to_dt` bound for backfill.
- Swap the call; `latest`/`previous` now mean this-week/last-week again.
- **Gate:** existing derive tests pass with weekly semantics restored; add a test proving intra-week
  daily rows do NOT spawn signals.

### ☐ Step 6 — Consumer simplifications
**Files:** `trip_ema/snapshots/market_overview.py`, `trip_ema/gui/queries.py`, `trip_ema/transports/http/routes/signal_history.py`, their tests.
- `market_overview.py:231` — `JOIN week_anchor wa ON sis.bar_timestamp = wa.monday_ts` becomes
  `ON sis.date = wa.today_d`. The intra-week value is now precomputed per day → drop the
  "hope the forming-bar row exists at the Monday anchor" logic. This consumer gets *simpler*.
- `gui/queries.py:368` current-regime lookup — `sis.bar_timestamp = :current_ts` →
  `sis.date = <today IST>`. Keep the coloring contract (`current_regime` = today's regime).
- `signal_history.py:282` `_fetch_indicator_rows` — select `date` directly instead of
  `.astimezone(tz).date()` derivation; **drop the `bar_timestamp::date` at :293** (session-TZ bug).
- **Gate:** `test_market_overview`, `test_signal_history`, GUI query tests green with `date` joins.

### ☐ Step 7 — IST-date regression guard
**Files:** `tests/persistence/test_…` (new focused test).
- Assert the `date` written for a bar is its **IST calendar date** (`.astimezone(ZoneInfo("Asia/Kolkata")).date()`), independent of session TZ. Include a `SET LOCAL TIME ZONE 'UTC'` vs `'Asia/Kolkata'` case.
- Rationale: this is the **third** TZ-drift exposure on this codebase (week_anchors fixtures twice). It earns a dedicated guard.

---

## 5. Downstream impact ledger (tick as verified clear)

| Consumer | File | Change | Status |
|---|---|---|---|
| Enrichment 1:1 + FK | `stock_indicator_enrichment` + `0021`→`0034` | daily grain, FK→`(…,date)` | ☐ |
| Signal derivation | `orchestrator/derive_signals.py` | week-grain read (Step 5) | ☐ |
| Market overview snapshot | `snapshots/market_overview.py` | join on `date` (simpler) | ☐ |
| GUI coloring | `gui/queries.py` | regime lookup on `date` | ☐ |
| Signal history route | `transports/http/routes/signal_history.py` | select `date`, drop `::date` cast | ☐ |
| Backfill stage 2 | `backfill/pipeline.py::_stage_stock_state` | verify still calls runner correctly | ☐ |
| Admin v1pipeline route | `transports/http/routes/admin_v1pipeline.py` | no shape change; verify | ☐ |
| Recompute market cycle | `orchestrator/recompute_market.py` | incremental scope semantics (Step 4) | ☐ |
| Repo getters | `persistence/stock_indicator_state_repo.py` | `ORDER BY date` | ☐ |
| **Not impacted** | `industry_*` tables, `current_signals`, `historical_signals` | separate/loose-coupled | ☑ |

---

## 6. Landmines & principles (from prior burns)

- **TZ:** never `::timestamptz` / `::date` cast that depends on session TZ. Use
  `.astimezone(ZoneInfo("Asia/Kolkata")).date()` in Python or explicit `AT TIME ZONE 'UTC'` in SQL.
  Reference impls that are already correct: `signal_history.py:300-301`, `gui/queries.py:171-183`,
  `aggregation.py::_monday_of`.
- **Parity:** `test_pine_parity` only has a reference for **week-end rows**. Intra-week rows have
  no TV reference — never assert Pine-equality on them.
- **Fixtures vs code:** any time-anchor change in a helper must be diffed against its test fixtures
  **in the same commit**.
- **Patchers:** one-shot, run from `~/Downloads/`, verify, `rm`. Never commit. Idempotency sentinels
  structural (AST/field presence), never substring text matches. Grep target substring appears
  exactly once before writing any `str_replace` anchor.
- **Revert path if failures cascade:** `git stash push -u` + `alembic downgrade -1` + selective restore.
  Don't ride the "fix the next failure" loop.

---

## 7. Open decisions to lock before Step 3

1. **`expected_sessions` source** — reuse an existing session/holiday calendar, or day-count fallback
   (`min(5, elapsed+remaining_weekdays)`)? Grep for an existing trading-calendar before inventing one.
2. **Windowing constant** — confirm ~60 weeks covers longest-EMA + ATR warmup with margin; pull the
   actual longest length from `INDICATOR_DEFAULTS`.
3. **Enrichment: same-commit or fast-follow?** — FK forces same *migration* (Step 1), but the runner
   rewrite (Step 3) could split state-first / enrichment-second if it stays green between. Decide by
   how coupled the day-loop helper is.
4. **`current_signals.bar_timestamp`** — market_overview signal counts join `cs.bar_timestamp = monday_ts`
   (market_overview.py:248). That table is weekly and unaffected, but confirm the signal-count semantics
   still line up once regime counts move to daily `date`.

---

## 8. First action

Start **Step 1**. Pull `0020_clean_pipeline_tables.py` and `0021_indicator_enrichment_tables.py`
verbatim to match the exact `op.execute` DDL idiom, then write `0034` + its migration test. It's
self-contained and gates everything downstream.
