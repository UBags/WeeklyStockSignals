# Clean Pipeline V1 — Cutover Plan

**Status:** Successor to `CLEAN_PIPELINE_REFACTOR.md`. That document laid out the foundation now in place (V1 schema, populators, repos for state + enrichment, A1-A3 signal_events demolition). **This document is the signpost for the next push** — wiring the live cycle, rewiring the GUI, demolishing the legacy pipeline.

**Branch:** `architecture/clean-pipeline-v1`. Merges to `master` at Step 17 (final cutover).

---

## 0. State of play

### What's done (the foundation)

The V1 tables exist, are populated by explicit admin endpoints, and have CRUD + validation covered by ~2,260 passing tests.

| Component | What | Where |
|---|---|---|
| Schema | `stock_indicator_state`, `stock_indicator_enrichment`, `industry_indicator_state`, `industry_indicator_enrichment`, `current_signals`, `historical_signals` | Migrations 0020, 0021 |
| Phase CHECK ext. | `v1_stock_indicator_state`, `v1_stock_indicator_enrichment`, `v1_industry_index_value`, `v1_industry_indicator_state`, `v1_industry_indicator_enrichment` | Migrations 0022-0026 |
| Compute primitives | `compute_state_primitives`, `compute_enrichment_primitives` | `trip_ema/fundamentals/v1_compute_primitives.py` |
| Populators (full-history one-shot) | 4 of: stock state, stock enrichment, industry state, industry enrichment + industry index value backfill | `trip_ema/fundamentals/v1_runners.py` |
| Repos for state + enrichment | 4 of: `StockIndicatorStateRepository`, `StockIndicatorEnrichmentRepository`, `IndustryIndicatorStateRepository`, `IndustryIndicatorEnrichmentRepository` | `trip_ema/persistence/` |
| HTTP endpoints | `POST /v1/admin/v1pipeline/{stock,industry}-indicator-{state,enrichment}` (+ industry-index-value) | `trip_ema/transports/http/routes/admin_v1pipeline.py` |
| signal_events demolition | A1 (writers) + A2 (app code) + A3 (column drop + NOT NULLs) all committed | Commits e8c8c64, b58157b, 3966644 |

### What's missing (the next push)

The live cycle does NOT yet use V1 tables. The GUI does NOT yet read from V1 tables. The legacy `indicator_state` + legacy `signal_events`-as-ticker-only pipeline is still doing the work in production. The V1 tables sit populated but unused by anything live.

| Gap | What needs to happen |
|---|---|
| Signal repos | `CurrentSignalsRepository`, `HistoricalSignalsRepository`, `SignalsReader` (cache-first + historical fallback) — not yet built |
| Incremental compute | The populators write full history; the cycle needs `mode='incremental'` (rewrite last N weeks only) on each tick |
| Signal derivation | `derive_signal_for_ticker_this_cycle` — compare latest two state rows; emit if regimes differ |
| Rotation | Atomic `current_signals → historical_signals` move |
| SignalCache rework | Atomic `rebuild_from(current_signals)`; drop `stale_since` + write-through |
| `recompute_market` | The function that does stock compute → rotate → derive → industry compute, injected into MarketOrchestrator |
| V1 email composer + sender | Read `current_signals`, enrich, rank, render, send via Brevo; no per-subscriber filtering yet |
| `notify_subscribers` | The function that wraps composer + sender, injected into MarketOrchestrator |
| Wire MarketOrchestrator | Swap injected `compute_run_once` and `distribute_run_once` to V1 functions |
| Backfill rewire | `generate_and_backfill_signals.py` calls `recompute_market(mode='backfill')` per historical date — must be done in V1 to keep backfill working post-cutover |
| GUI rewire | Ticker page + Signals page SQL still hits legacy `indicator_state`. Must be rewritten to read from V1 tables |
| Legacy demolition | Delete legacy `run_indicator_state_for_*`, polymorphic `IndicatorState`, legacy signal compute, vestigial `ComputeScheduler` |
| Drop legacy tables | `DROP TABLE indicator_state, signal_events` migration |

---

## 1. Original vision (recap)

From the brief that started the clean-pipeline work:

> a) After every fetch, all stock EMAs, ATRs, regimes are recalculated. If it's mid-week, the previous values for the week are overwritten (instead of adding a new row). If it's mid-day, the values for the day are overwritten.
> b) After that, signals are generated for ALL stocks. The new signals are inserted as a new row, EVEN IF it's the same signal as was generated earlier in the previous run. A signal is generated ONLY at the bar where a regime change happens.
> c) After that, industry indices are recalculated, and their ATRs, EMAs, and regimes are recalculated; previous values for that week (or day) are overwritten. No signals generated for industry_indices.
> d) Then, the email body is created for all stocks for which signals have been generated in this run.
> e) The email is sent.
>
> Storage:
> a) One table for stock EMAs/ATRs/regimes
> b) One table for industry EMAs/ATRs/regimes
> c) Two tables for signals: `historical_signals` and `current_signals`
> d) When signals are generated for a current run, first move all data from current_signals to historical_signals. Then current_signals is empty and gets populated by this run's new signals.

**Schema delta from the original vision:** We split state and enrichment into separate tables per series type (stock + industry × state + enrichment = 4 tables). The enrichment tables hold the 23 fields beyond the core 11 indicator values (pattern labels, turn flags, distance metrics). Original vision had a single table per series type; the split is justified because the enrichment fields are downstream of state and likely to evolve over time.

**Other than that, V1 implements the vision faithfully.** The cycle order matches a-e. The rotate-then-populate semantics for current_signals matches d. The "new row even if same" rule matches the doc's N4 (`historical_signals` has no uniqueness constraint, is append-only).

V1 scope: **Weekly only.** Daily/intraday is Phase 16 (post-cutover).

---

## 2. Target architecture

### 2.1 Orchestration

`MarketOrchestrator` (Phase 14) already runs the 4-tick NSE schedule (09:30, 12:15, 15:00, 16:30 IST) and dispatches:

```
fetch_fn  →  compute_run_once  →  distribute_run_once
```

V1 cutover rewires the latter two by injecting new functions. The orchestrator scaffolding (loop, lock, retries, trading-day gates, pulse publishing, /readyz integration) is untouched.

| Stage | Injected function (V1) | What it does |
|---|---|---|
| Fetch | `fetch_fn` (existing) | Pulls fresh OHLCV bars from yfinance into `ohlcv_bars` |
| Compute | **`recompute_market`** (new) | Stock state incremental → stock enrichment incremental → rotate signals → derive signals → industry state incremental → industry enrichment incremental |
| Distribute | **`notify_subscribers`** (new) | Compose digest from current_signals → (future: ML filter → per-subscriber rules filter) → send via Brevo → log to notification_log |

### 2.2 `recompute_market` — canonical reusable primitive

This is the workhorse. The live cycle calls it. Backfill calls it. Future single-date admin invocations call it.

```python
def recompute_market(
    db_engine: Engine,
    *,
    mode: Literal["live", "backfill"] = "live",
    as_of: datetime | None = None,          # backfill: the historical instant being replayed
    n_weeks_to_upsert: int = 2,             # incremental: rewrite last N weekly bars
    exchange: str = "NSE",                  # filters active tickers + industry indices
) -> RecomputeMarketResult:
    """One pass through the V1 compute pipeline.

    Live mode (default):
      1. For each active ticker: compute_and_persist_stock_state(mode='incremental', n_weeks_to_upsert)
      2. For each active ticker: compute_and_persist_stock_enrichment(mode='incremental', n_weeks_to_upsert)
      3. Rotate current_signals → historical_signals (atomic)
      4. For each active ticker: derive_signal_for_ticker_this_cycle → write to current_signals
      5. SignalCache.rebuild_from(current_signals)
      6. For each active industry: compute_and_persist_industry_state(mode='incremental', n_weeks_to_upsert)
      7. For each active industry: compute_and_persist_industry_enrichment(mode='incremental', n_weeks_to_upsert)

    Backfill mode (called by generate_and_backfill_signals.py per historical date):
      - Same compute steps 1, 2, 6, 7 (state + enrichment for stocks + industries)
      - NO rotation (current_signals is for live cycle only)
      - Derive signals at the historical instant, write directly to historical_signals
      - NO SignalCache rebuild (cache reflects live state, not historical backfill)

    Returns counters for each stage + per-stage error isolation.
    """
```

**Stage isolation:** A per-ticker error in stage 1 doesn't abort stages 2-7. A stage-level error halts subsequent stages and is recorded in the result.

### 2.3 `notify_subscribers` — distribute stage

```python
def notify_subscribers(
    db_engine: Engine,
    brevo_client: BrevoHttpClient,
    *,
    cycle_id: str,
    now: datetime,
) -> NotifyResult:
    """V1: one digest per subscriber, all current_signals included.

    Pipeline:
      1. SELECT all current_signals (with joins to enrichment + benchmark_stats for rank_score)
      2. (V1: skip) Future ML ensemble filter — reorders/filters signals based on enrichment features
      3. For each active subscriber:
           a. (V1: skip) Apply subscriber.rules_json filter (P2-A)
           b. compose_email(signals, subscriber, cycle_id, now) → EmailBody
           c. send_email(subscriber, body, brevo_client) → SendResult
           d. Insert notification_log row
      4. Return aggregate counters

    V1 = one body, no rules. Step 7 builds the composer with hooks for the
    future filter layers.
    """
```

### 2.4 Schedule semantics

Original CLEAN_PIPELINE_REFACTOR proposed cron-driven stage-separated invocations with Redis coordination (`ScheduleSequence`). We're NOT doing that — MarketOrchestrator already serializes the stages in one process. **Schedule stays at the 4-tick MarketOrchestrator loop.** No cron lines to install.

If Phase 14's orchestrator-pool model needs to evolve (e.g. multi-exchange with overlapping schedules), that's a future concern, not V1 cutover.

### 2.5 Read paths after cutover

| GUI / API surface | Reads from | Notes |
|---|---|---|
| Ticker page — per-stock latest EMAs/ATRs/regime | `stock_indicator_state` (latest row by bar_timestamp) | Primary stock-list source |
| Ticker page — per-stock enrichment (pattern, turns, distances) | `stock_indicator_enrichment` (1:1 with state) | For the detail panel |
| Signals page — last N signals for ticker X | SignalCache → `historical_signals` fallback | Cache: 0 or 1 from current; fallback: N from historical ordered `bar_timestamp DESC, computed_at DESC` |
| Signals page — chart history | `stock_indicator_state` (full history for the ticker) | Same as old, just new table |
| Industry page | `industry_indicator_state` + `industry_indicator_enrichment` | Same shape as ticker side |
| Email composition | `current_signals` (joined with state, enrichment, benchmark_stats) | This-cycle's signals only |

---

## 3. Step-by-step plan

Each step is one commit. Each step lands green. Branch stays on `architecture/clean-pipeline-v1` until Step 17.

### Step 1 — Signal repositories + SignalsReader

**Goal:** The three persistence-layer classes that mediate access to `current_signals` and `historical_signals`.

**New files:**
- `trip_ema/domain/signal_event.py` — V1 `SignalEvent` dataclass (replaces the legacy `trip_ema.fundamentals.SignalEvent` eventually; coexists during transition). Fields: `ticker_id, timeframe, bar_timestamp, signal_type, regime_before, regime_after, price_at_signal, computed_at`. (No `signal_id` on the dataclass — `historical_signals` has `signal_id BIGSERIAL` but it's persistence-internal.)
- `trip_ema/persistence/current_signals_repo.py` — `CurrentSignalsRepository`:
  - `insert_many(signals: list[SignalEvent]) -> int` (PK is `ticker_id`; ON CONFLICT DO UPDATE replaces; idempotent)
  - `truncate() -> None`
  - `get_for_ticker(ticker_id) -> SignalEvent | None`
  - `list_all() -> list[SignalEvent]`
  - `count() -> int`
- `trip_ema/persistence/historical_signals_repo.py` — `HistoricalSignalsRepository`:
  - `insert_many(signals: list[SignalEvent], archived_at: datetime | None = None) -> int` (append-only; no uniqueness)
  - `get_for_ticker(ticker_id, *, kinds=None, from_date=None, to_date=None, limit=None) -> list[SignalEvent]` (ordered `bar_timestamp DESC, computed_at DESC`)
  - `count_for_ticker(ticker_id) -> int`
  - `count() -> int`
- `trip_ema/signals/signals_reader.py` — `SignalsReader`:
  ```python
  class SignalsReader:
      def __init__(self, cache: SignalCache, historical_repo: HistoricalSignalsRepository): ...

      def last_n_for_ticker(self, ticker_id: int, n: int) -> list[SignalEvent]:
          """Cache hit: 1 from cache + (n-1) from historical. Cache miss:
          n from historical. No UNION."""

      def latest_for_ticker(self, ticker_id: int) -> SignalEvent | None:
          """Cache → historical fallback."""
  ```

**Tests new:**
- `tests/persistence/test_current_signals_repo.py` — insert + idempotent overwrite (PK is ticker_id), truncate, reads
- `tests/persistence/test_historical_signals_repo.py` — append-only (duplicates allowed), ordering on read, filters
- `tests/signals/test_signals_reader.py` — cache hit + miss paths, no UNION, ordering

**Commit:** `feat(persistence): CurrentSignalsRepository + HistoricalSignalsRepository + SignalsReader (V1 cutover step 1)`

---

### Step 2 — Cycle-incremental compute mode

**Goal:** Add `mode='incremental'` to the four populators we already have. Existing `mode='full'` (the one-shot full-history we shipped) stays.

**Background:** Each populator currently writes the entire history every invocation. For the live cycle that's expensive (~250 weekly bars × 2,464 tickers per tick). Incremental mode rewrites only the latest N weekly bars (default N=2 — covers the in-progress week plus the just-finalized prior week in case it changed since last cycle).

**Files modified:**
- `trip_ema/fundamentals/v1_runners.py` — extend the four populator functions to accept `mode: Literal["full", "incremental"]` and `n_weeks_to_upsert: int = 2`. Default mode stays `'full'` to keep existing callers (the admin endpoints) working.
- Internal: incremental mode computes the full trajectory (cheap pandas math) but ONLY upserts the last N bars. The repo's `upsert_many` is already idempotent.
- HTTP endpoints in `admin_v1pipeline.py`: add optional `?mode=incremental&n_weeks_to_upsert=2` query params for testability.

**Tests new:**
- `tests/fundamentals/test_v1_runners_incremental.py` — incremental writes exactly N rows, doesn't touch older rows, late correction to bar T-1 is picked up by next incremental run, full→incremental→full round-trip is consistent.

**Commit:** `feat(v1pipeline): cycle-incremental mode for state + enrichment populators (V1 cutover step 2)`

---

### Step 3 — Signal derivation primitive

**Goal:** The function that produces a signal IFF this cycle detects a regime change.

**New file:** `trip_ema/orchestrator/derive_signals.py`:
```python
def derive_signal_for_ticker_this_cycle(
    conn: Connection,
    ticker_id: int,
    *,
    timeframe: str = "Weekly",
    now: datetime,
) -> SignalEvent | None:
    """Read the latest TWO stock_indicator_state rows for this
    (ticker, timeframe). If their display_regime differs, emit
    a SignalEvent at the latest bar's bar_timestamp. Otherwise None.

    Returns None when the ticker has fewer than 2 state rows (warmup).
    O(1) per ticker.
    """

def derive_signals_for_all_active_stocks(
    conn: Connection,
    *,
    timeframe: str = "Weekly",
    now: datetime,
) -> list[SignalEvent]:
    """Iterate active tickers; call derive_signal_for_ticker_this_cycle;
    drop Nones. Returns the list to bulk-insert into current_signals."""
```

**Signal kind mapping** (matches existing convention):
- `regime_before ≠ +1, regime_after = +1` → `BUY`
- `regime_before ≠ -1, regime_after = -1` → `SELL`
- `regime_before = +1, regime_after ≠ +1` → `EXIT_FROM_BUY`
- `regime_before = -1, regime_after ≠ -1` → `EXIT_FROM_SELL`

`price_at_signal` = the latest bar's close from `ohlcv_bars` (one extra small query per ticker).

**Same-bar re-detection is by design** — if cycle N detected a regime change at bar B (week still open), cycle N+1 will detect the same regime change at bar B (still open). Both cycles emit a signal at B. Rotation moves cycle N's signal to historical between the two cycles, so historical accumulates a copy per cycle until the bar closes. This matches the original vision's *"new row even if same."*

**Tests new:** `tests/orchestrator/test_derive_signals.py` — all 4 signal kinds, no-change returns None, warmup returns None, bulk version skips inactive tickers, same-bar re-detection across two cycles.

**Commit:** `feat(orchestrator): derive_signal_for_ticker_this_cycle — O(1) regime-change detection (V1 cutover step 3)`

---

### Step 4 — Rotation primitive

**Goal:** Atomic `current_signals → historical_signals` move.

**New file:** `trip_ema/orchestrator/rotate_signals.py`:
```python
def rotate_current_to_historical(conn: Connection) -> int:
    """In one transaction:
      INSERT INTO historical_signals (
        ticker_id, timeframe, bar_timestamp, signal_type,
        regime_before, regime_after, price_at_signal,
        computed_at, archived_at
      )
      SELECT ticker_id, timeframe, bar_timestamp, signal_type,
             regime_before, regime_after, price_at_signal,
             computed_at, NOW()
        FROM current_signals;

      TRUNCATE current_signals;

    Returns the number of rows moved. Atomic — both ops in one
    transaction.
    """
```

**Tests new:** `tests/orchestrator/test_rotate_signals.py` — count match, current_signals empty after, historical_signals has the rows with archived_at populated, transactional (raises mid-rotation → no change), empty source is a no-op.

**Commit:** `feat(orchestrator): rotate_current_to_historical — atomic current → historical (V1 cutover step 4)`

---

### Step 5 — SignalCache rework

**Goal:** Atomic full-replace cache from `current_signals`. Drop the legacy `stale_since` concept and the write-through `update_signal_event`.

**Modified file:** `trip_ema/cache/signal_cache.py`:
```python
class SignalCache:
    def rebuild_from(self, signals: Sequence[SignalEvent]) -> None:
        """Atomic swap: replace the internal dict with one built
        from `signals`. Tickers not in `signals` are dropped from
        the cache (no stale_since flag — they're just gone).

        Readers in flight during the swap see either the OLD dict
        or the NEW — never a torn intermediate. Implemented via
        local-build-then-single-reassign (CPython GIL-safe)."""

    def get_signal(self, ticker_id: int, timeframe: str) -> SignalEvent | None: ...
    def all_signals(self) -> list[SignalEvent]: ...
```

**Deletions:** `update_signal_event` (per-event write-through), `stale_since` field, anything that exists to mark "this cached signal is old."

**Rationale:** Under the V1 model, the cache holds only this-cycle's signals. A ticker that hasn't flipped this cycle simply isn't in cache — `SignalsReader` falls through to historical. No staleness flag needed.

**Tests invalid:** Any test of `update_signal_event`, any test asserting `stale_since` semantics.

**Tests new:** `tests/cache/test_signal_cache_rebuild.py` — empty rebuild empties cache, populated rebuild populates exactly those keys, second rebuild replaces (doesn't accumulate), concurrent reader stress test (100 threads × 10 rebuilds) returns no torn data, tickers in old cache but not new snapshot are dropped.

**Commit:** `refactor(cache): SignalCache.rebuild_from — atomic full-replace (V1 cutover step 5)`

---

### Step 6 — `recompute_market`

**Goal:** Wire Steps 2-5 + cache rebuild into one function. The cycle's compute stage.

**New file:** `trip_ema/orchestrator/recompute_market.py`:
```python
@dataclass(frozen=True, slots=True)
class RecomputeMarketResult:
    mode: Literal["live", "backfill"]
    as_of: datetime
    started_at: datetime
    finished_at: datetime
    stock_state_succeeded: int
    stock_state_failed: int
    stock_enrichment_succeeded: int
    stock_enrichment_failed: int
    signals_rotated: int        # current → historical row count (live only)
    signals_emitted: int        # rows written to current/historical depending on mode
    industry_state_succeeded: int
    industry_state_failed: int
    industry_enrichment_succeeded: int
    industry_enrichment_failed: int
    stage_errors: list[tuple[str, str]]   # (stage_name, error_msg) — stage-halt errors


def recompute_market(
    db_engine: Engine,
    *,
    mode: Literal["live", "backfill"] = "live",
    as_of: datetime | None = None,
    n_weeks_to_upsert: int = 2,
    cache: SignalCache | None = None,   # required in live mode
    exchange: str = "NSE",
) -> RecomputeMarketResult: ...
```

**Live-mode internals** (sequential):
1. Stock state incremental for all active tickers (per-ticker error isolation)
2. Stock enrichment incremental for all active tickers (per-ticker error isolation; skips ticker if state row is missing)
3. `rotate_current_to_historical(conn)` (atomic)
4. `derive_signals_for_all_active_stocks(conn, now=as_of)` → `CurrentSignalsRepository.insert_many`
5. `cache.rebuild_from(CurrentSignalsRepository.list_all())`
6. Industry state incremental for all active industries (per-industry error isolation)
7. Industry enrichment incremental for all active industries (per-industry error isolation)

**Backfill-mode internals:**
1. Stock state incremental for all active tickers (or, if `as_of` is far in the past, full?) — Step 11 settles this
2. Stock enrichment incremental for all active tickers
3. Derive signals at `as_of` → `HistoricalSignalsRepository.insert_many` directly. **No rotation.**
4. Industry state incremental for all active industries
5. Industry enrichment incremental for all active industries
6. **No cache rebuild.** **No email.**

**Tests new:** `tests/orchestrator/test_recompute_market.py`:
- Live mode end-to-end with synthetic 10-ticker fixture: all stages execute, result counters match
- Backfill mode skips rotation + cache rebuild
- Per-ticker error in stage 1 doesn't abort stages 2-7
- Stage-level error halts subsequent stages, records in `stage_errors`
- Two consecutive live cycles where the same regime change persists → rotation works correctly between them

**Commit:** `feat(orchestrator): recompute_market — V1 cycle compute primitive (V1 cutover step 6)`

---

### Step 7 — V1 email composer + sender

**Goal:** Two pure functions. Composer reads `current_signals`, enriches, ranks, renders. Sender takes a body + subscriber, POSTs to Brevo, returns a result.

**New files:**
- `trip_ema/notifications/compose_email.py`:
  ```python
  @dataclass(frozen=True, slots=True)
  class EmailBody:
      subject: str
      html: str
      plain_text: str
      signal_count: int

  def compose_email(
      conn: Connection,
      subscriber: Subscriber,
      cycle_id: str,
      now: datetime,
  ) -> EmailBody | None:
      """V1: read all rows from current_signals, enrich each with
      ticker symbol + industry + stock_indicator_state +
      stock_indicator_enrichment + latest benchmark_stats; compute
      rank_score; sort DESC; render HTML + plain text via Jinja.

      V1 sends the SAME body to every subscriber. The `subscriber`
      parameter is present so P2-A can hook in subscriber-specific
      filters without changing the call signature.

      Returns None if current_signals is empty (no email sent)."""
  ```
- `trip_ema/notifications/send_email.py`:
  ```python
  @dataclass(frozen=True, slots=True)
  class SendResult:
      subscriber_id: int
      success: bool
      message_id: str | None
      error: str | None
      sent_at: datetime

  def send_email(
      subscriber: Subscriber,
      body: EmailBody,
      brevo_client: BrevoHttpClient,
      now: datetime,
  ) -> SendResult: ...
  ```
- Templates: `trip_ema/notifications/templates/email_body.html.j2`, `email_body.txt.j2`. Group by industry, columns: signal type, ticker, regime, price, rank_score.

**Rank score** (matches existing convention): `0.5 × stock_RS_26w + 0.3 × industry_RS_26w + 0.2 × R²`. NULLs sort last.

**Subject:** `[trip_ema] {cycle_id} — {N} signals ({n_buys} BUY, {n_sells} SELL, {n_exits} EXIT)`.

**Hooks documented (not implemented in V1):**
- ML ensemble filter — sits between `current_signals` SELECT and the per-subscriber loop in `notify_subscribers` (Step 9)
- Per-subscriber rules JSON filter — sits inside the per-subscriber loop, before `compose_email`

**Tests new:**
- `tests/notifications/test_compose_email.py` — empty current_signals → None, 5 signals → all 5 present and ordered, NULL rank_score sorts last, HTML well-formed (bs4 parse), plain-text has no tags, subject counts match body, industry grouping correct.
- `tests/notifications/test_send_email.py` — success path returns message_id, 4xx returns error, timeout returns "timeout", uses mocked Brevo client.

**Commit:** `feat(notifications): V1 email composer + Brevo sender — pure functions, no rules (V1 cutover step 7)`

---

### Step 8 — `notification_log` denormalization

**Goal:** Drop the FK from `notification_log` to legacy `signal_events` (which gets dropped in Step 15). Denormalize signal identity into `notification_log`.

**Migration 0027:**
- ADD columns: `signal_ticker_id INT`, `signal_timeframe VARCHAR(8)`, `signal_bar_timestamp TIMESTAMPTZ`, `signal_type VARCHAR(16)`, `signal_price DOUBLE PRECISION`
- BACKFILL from existing FK join: `UPDATE notification_log SET signal_ticker_id = se.ticker_id, ... FROM signal_events se WHERE notification_log.event_id = se.event_id`
- DROP FK `notification_log_event_id_fkey`
- DROP COLUMN `event_id`
- ALTER the 5 new columns to `NOT NULL` after backfill

**Files modified:**
- `alembic_pkg/versions/0027_notification_log_denormalize.py` (new)
- `migrations/legacy/031_notification_log_denormalize.sql` (new)
- `trip_ema/persistence/notification_log_repo.py` — write the denormalized columns

**Tests new:** `tests/persistence/test_notification_log_denormalized.py` — schema check, write path, backfill correctness on a seeded `notification_log` + `signal_events` pair.

**Tests invalid:** Anything that asserts `notification_log.event_id` semantics.

**Commit:** `refactor(notification_log): denormalize signal identity, drop FK to legacy signal_events (V1 cutover step 8)`

---

### Step 9 — `notify_subscribers`

**Goal:** Wire compose + send into the distribute stage.

**New file:** `trip_ema/orchestrator/notify_subscribers.py`:
```python
@dataclass(frozen=True, slots=True)
class NotifyResult:
    cycle_id: str
    signals_in_current: int
    subscribers_targeted: int
    emails_composed: int        # nonzero only when signals_in_current > 0
    emails_sent: int
    emails_failed: int
    errors: list[tuple[int, str]]    # (subscriber_id, error_msg)


def notify_subscribers(
    db_engine: Engine,
    cache: SignalCache,
    brevo_client: BrevoHttpClient,
    *,
    cycle_id: str,
    now: datetime,
) -> NotifyResult:
    """V1 distribute stage.

    Flow:
      1. Count current_signals rows. If zero, return early
         (no emails, no work).
      2. Future hook: ML ensemble filter (V1: no-op pass-through).
      3. For each active subscriber:
           a. Future hook: per-subscriber rules JSON filter (V1: no-op).
           b. body = compose_email(conn, subscriber, cycle_id, now)
           c. result = send_email(subscriber, body, brevo_client, now)
           d. NotificationLogRepository.insert(result, ...)
      4. Return aggregate counters.
    """
```

**Tests new:** `tests/orchestrator/test_notify_subscribers.py` — zero current_signals returns early with zero emails, one subscriber + 3 signals → 1 email sent + 1 notification_log row, send failure recorded in errors but doesn't abort other subscribers, both success and failure paths write notification_log.

**Commit:** `feat(orchestrator): notify_subscribers — V1 distribute stage (V1 cutover step 9)`

---

### Step 10 — Wire MarketOrchestrator + delete ComputeScheduler

**Goal:** Swap the injected `compute_run_once` and `distribute_run_once` of `MarketOrchestrator` to the V1 functions. Delete the vestigial `ComputeScheduler`. The cycle is now live on V1.

**Files modified:**
- `trip_ema/app.py` — at MarketOrchestrator construction site, inject:
  - `compute_run_once = lambda: recompute_market(db_engine, mode='live', cache=cache, exchange='NSE')`
  - `distribute_run_once = lambda: notify_subscribers(db_engine, cache, brevo_client, cycle_id=..., now=datetime.now(UTC))`
- `trip_ema/transports/http/routes/health.py` — `/readyz` no longer reports `compute_scheduler_alive`; report MarketOrchestrator's `is_alive()` directly (already exposed). Also fix the cosmetic null-bool issue noted in queued workstreams memory.

**Files deleted:**
- `trip_ema/scheduler/compute_scheduler.py` (entire file)
- `tests/scheduler/test_compute_scheduler.py` (entire file)

**Tests new:**
- `tests/integration/test_market_orchestrator_v1_wiring.py` — synthetic 10-ticker fixture, invoke `MarketOrchestrator.run_once()`, assert: stock_indicator_state has fresh rows, current_signals populated (or empty if no regime changes), notification_log has a row, no writes to legacy `indicator_state` or legacy `signal_events`.

**Tests invalid:**
- `tests/scheduler/test_compute_scheduler.py` (deleted file)
- Anything asserting `/readyz` has a `compute_scheduler_alive` field

**Commit:** `refactor(orchestrator): wire MarketOrchestrator to V1 compute + notify; delete ComputeScheduler (V1 cutover step 10)`

---

### Step 11 — Backfill rewire

**Goal:** `generate_and_backfill_signals.py` calls `recompute_market(mode='backfill')` in a date-range loop. Must land before legacy demolition because the existing backfill writes via legacy paths.

**File modified:** `trip_ema/signals/generate_and_backfill_signals.py`:
- Replace the current rules-based body with a date-range loop calling `recompute_market(mode='backfill', as_of=date)` for each historical date in the range.
- The function's CLI signature stays the same (operator-facing): `--exchange NSE --from-date YYYY-MM-DD --to-date YYYY-MM-DD`.
- For each iteration: synthesize an `as_of = datetime(date, 16:30, tz=IST)` (end-of-trading-day for that date).

**Other files modified:**
- `trip_ema/signals/__init__.py` — remove obsolete exports (`PositionState`, `SignalDecision`, `walk_and_generate_events`, etc.)
- `trip_ema/etl/cli.py` — keep the `backfill-signals` subcommand pointing at the new entry point.

**Tests new:**
- `tests/signals/test_backfill_rewire.py` — given a synthetic 100-week input with regime flips at weeks 20/50/80, run backfill over the full range → exactly 3 rows in historical_signals (no rotation, no current_signals writes), idempotent re-run produces duplicates (no dedup at insert — that's the read-side concern).

**Tests invalid:** Old `test_walk_and_generate_events.py` and any test exercising `prev_state: PositionState` threading.

**Commit:** `refactor(signals): backfill rewires to recompute_market(mode='backfill') (V1 cutover step 11)`

---

### Step 12 — GUI rewire (Ticker + Signals pages)

**Goal:** All HTTP routes and GUI panels that currently read from legacy `indicator_state` or legacy `signal_events` (ticker rows) get rewired to read from V1 tables.

**Audit required first.** Before writing code, run:
```bash
grep -rln "indicator_state\b\|signal_events\b" trip_ema/transports/http/routes/ trip_ema/gui/
```
Each hit needs a decision: rewire to which V1 table, or delete entirely.

**Anticipated route rewires** (per the current dump):
- `/v1/signals/{exchange}/{symbol}/history` — chart data reads `stock_indicator_state` (latest N bars), signals list reads via `SignalsReader.last_n_for_ticker(ticker_id, N=10)`.
- `/v1/signals/industry/{name}/history` — reads `industry_indicator_state`.
- `/v1/tickers/{exchange}/{symbol}` (or wherever the Tickers tab data comes from) — reads `stock_indicator_state` (latest) + `stock_indicator_enrichment` (latest) + benchmark_stats (existing).

**Anticipated GUI panel rewires:**
- Ticker page panel: hits the rewired ticker route; payload shape may shift if enrichment fields were previously inline with state and now come from a separate table. Verify before/after.
- Signals page panel: hits the rewired signals route; last-N-signals list rendering should be unchanged at the GUI layer if the HTTP response shape is preserved.

**Tests new:**
- `tests/transports/http/test_signals_history_v2.py` — chart data from `stock_indicator_state`, signals list from SignalsReader.
- `tests/transports/http/test_tickers_state.py` — latest state + enrichment composed correctly.
- `tests/transports/http/test_signals_history_industry_v2.py`

**Tests invalid:** Anything asserting the polymorphic `indicator_state` row shape on HTTP responses.

**Verification:** Manual GUI smoke after the route changes — chart loads, ticker latest state populated, last-5-signals list renders.

**Commit:** `feat(gui): rewire Ticker + Signals pages to read from V1 tables (V1 cutover step 12)`

---

### Step 13 — ETL GUI rework

**Status: TBD pending design decision.** Per Uddi's note (C), the ETL GUI page needs rework but the target design isn't settled yet. Placeholder for the step; details will be filled in once direction is locked.

**Likely shape** (subject to revision):
- Buttons for: run full cycle now (= one MarketOrchestrator tick), individual stages (fetch, recompute_market, notify_subscribers), Full Regenerate (stock state + industry state from scratch, archive-first).
- Each button: HTTP POST → 202 with run_id → progress polling.
- Atomic controls scope: current cycle only. Date-range backfill stays in the CLI (Step 11).

Will revisit once Uddi provides the target shape.

**Commit:** `feat(gui): ETL atomic-control buttons (V1 cutover step 13)` — when design lands.

---

### Step 14 — Legacy demolitions

**Goal:** Delete every dead code path the V1 cycle has replaced. Now safe because nothing live reads from or writes to them.

**Modules to delete:**

| Module | Reason |
|---|---|
| `trip_ema/signals/generate_signals_in_live_production.py` | Replaced by `derive_signals.py` |
| `trip_ema/fundamentals/runners.py::run_indicator_state_for_ticker` | Old runner with 80-day guard |
| `trip_ema/fundamentals/runners.py::run_indicator_state_for_industry` | Same |
| `trip_ema/fundamentals/runners.py::run_indicator_state_for_all_tickers` | Same |
| `trip_ema/fundamentals/signal_compute.py` | Old signal compute on polymorphic table |
| `trip_ema/persistence/fundamentals_repo.py::IndicatorStateRepository` | Polymorphic; superseded by Stock/Industry State+Enrichment repos |
| `trip_ema/persistence/fundamentals_repo.py::SignalEventRepository` (ticker-only after A2/A3) | Replaced by `CurrentSignalsRepository` + `HistoricalSignalsRepository` |
| `trip_ema/domain.IndicatorState` (or `trip_ema/fundamentals.IndicatorState`) — polymorphic | Superseded |
| `trip_ema/fundamentals.SignalEvent` (legacy) | Superseded by `trip_ema.domain.signal_event.SignalEvent` (Step 1) |
| `trip_ema/scheduler/subscriber_distributor.py` | Per-signal Redis enqueue is gone — emails composed inline by orchestrator |
| `trip_ema/notifier_worker.py` (separate process) | Same |
| `trip_ema/notifications/transports/*` (Redis-job transports) | Replaced by `send_email.py` (Step 7) |
| `trip_ema/etl/cli.py` subcommands for the deleted functions | Gone |

**Tests to delete:**
- `tests/fundamentals/test_indicator_state_runners.py`
- `tests/scheduler/test_subscriber_distributor.py`
- `tests/notifications/test_notifier_worker.py`
- Anything else importing a deleted symbol

**Tasks:**
1. `git rm` the files
2. Strip `__init__.py` exports
3. Update `trip_ema/etl/cli.py` to remove obsolete subcommands
4. Update `pyproject.toml` `[project.scripts]` if any deleted entry points are referenced
5. Run `mypy trip_ema/ tests/` + `pyright trip_ema/`; fix dangling imports
6. Run full pytest; delete any tests broken by removed symbols

**Verification:** Full pytest green; mypy + pyright clean.

**Commit:** `chore: demolish legacy compute + signal + notifier paths (V1 cutover step 14)`

---

### Step 15 — Drop legacy tables

**Goal:** Final schema cleanup.

**Pre-condition:** `grep -rln 'indicator_state\b\|signal_events\b' trip_ema/ tests/` returns zero hits in production code. If any hit, this step is blocked.

**Migration 0028:**
- `DROP TABLE indicator_state CASCADE;`
- `DROP TABLE signal_events CASCADE;`

**Files:**
- `alembic_pkg/versions/0028_drop_legacy_tables.py` (new)
- `migrations/legacy/032_drop_legacy_tables.sql` (new)

**Tests new:** `tests/persistence/test_migration_0028.py` — tables don't exist post-upgrade; downgrade recreates empty versions (best-effort; data is not recoverable).

**Verification:**
- `alembic upgrade head` succeeds
- `psql -c "\dt"` shows no `indicator_state` or `signal_events`
- `pytest tests/ -x` green

**Commit:** `feat(schema): drop legacy indicator_state + signal_events tables (V1 cutover step 15)`

---

### Step 16 — Documentation refresh

**Goal:** Bring `docs/` into alignment with V1 architecture.

**Tasks:**
1. Rewrite `docs/01_FUNCTIONALITY.md` — capability inventory reflects V1 cycle (no more parallel pipelines)
2. Rewrite `docs/02_ARCHITECTURE.md` — module map updated; sequence diagrams replaced with MarketOrchestrator + recompute_market + notify_subscribers flow
3. Update `docs/03_OPEN_ITEMS.md` — V1 is done; remaining queue: ETL GUI design, subscriber rules JSON (P2-A), ML ensemble filter, lead/lag analytics workstream, multi-exchange (Phase 19), intraday (Phase 16)
4. Update `scripts/operator_commands.md` with current CLI flags + admin endpoints
5. Update `README.md` to drop any obsolete sections
6. Archive obsolete docs to `docs/legacy/`
7. Add `docs/architecture/CLEAN_PIPELINE_V1_CUTOVER.md` — permanent record of this cutover's design (essentially this file, edited for posterity)

**Verification:** `grep -rn 'compute_scheduler\|signal_events\|generate_signals_in_live_production' docs/` returns zero hits.

**Commit:** `docs: refresh for V1 cutover; archive legacy docs (V1 cutover step 16)`

---

### Step 17 — E2E smoke + cutover to master

**Goal:** Final verification and merge to `master`.

**Smoke procedure:**

1. **Fresh DB state:**
   ```bash
   sudo -u postgres pg_dump trip_ema > ~/archives/pre-v1-cutover-$(date +%Y%m%d).sql
   sudo -u postgres dropdb trip_ema && sudo -u postgres createdb trip_ema
   alembic upgrade head
   ```

2. **Seed listings + initial backfill:**
   ```bash
   trip-ema-etl refresh-listings --exchange NSE
   trip-ema-etl backfill --exchange NSE --start 2009-01-01
   ```

3. **Full initial regen** (use the populator HTTP endpoints we shipped):
   ```bash
   curl -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
     "http://localhost:8000/v1/admin/v1pipeline/stock-indicator-state?all_exchanges=true"
   # ... same for stock-indicator-enrichment, industry-index-value, industry-indicator-state, industry-indicator-enrichment
   ```
   Expected: ~370k rows in `stock_indicator_state`, ~9k in `industry_indicator_state`.

4. **First cycle dry run:**
   ```bash
   curl -X POST http://localhost:8000/v1/admin/compute-now \
     -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN"
   ```
   Expected:
   - `current_signals` populated (N rows for tickers with regime changes this cycle)
   - `historical_signals` initially empty (this is the first cycle)
   - SignalCache populated
   - 1 email composed and sent to uddipan@gmail.com

5. **Second cycle** (proves rotation):
   ```bash
   curl -X POST http://localhost:8000/v1/admin/compute-now ...
   ```
   Expected:
   - `historical_signals` has the N rows from the first cycle (rotation)
   - `current_signals` has the latest-per-ticker again
   - Cache rebuilt

6. **GUI manual verification:**
   - Ticker page: latest EMAs/ATRs/regimes per stock load fast (V1 table read)
   - Signals page: last 5 signals per ticker — cache hits for just-flipped tickers, fallback to historical for others
   - Industry page: latest industry state populated
   - ETL page: buttons work (whatever lands in Step 13)

7. **MarketOrchestrator schedule verification:**
   - Service running under `restart_service.sh`
   - Wait for next NSE tick (09:30 / 12:15 / 15:00 / 16:30 IST)
   - `/tmp/trip-ema.log` shows stage transitions
   - `/readyz` reports MarketOrchestrator alive

8. **Final inspection:**
   ```sql
   SELECT COUNT(*) FROM current_signals;     -- ≤ 2,464
   SELECT COUNT(*) FROM historical_signals;  -- grows by ~N each cycle
   SELECT COUNT(*) FROM notification_log WHERE sent_at > NOW() - INTERVAL '1 day';
   -- expect 4 (one per cycle, one subscriber)
   ```

**Cutover:**
```bash
git checkout master
git merge --no-ff architecture/clean-pipeline-v1
git tag v1-clean-pipeline-cutover
git push origin master --tags
```

**Commit:** (merge commit — no separate commit needed)

---

## 4. Open / TBD

| Item | Status | Owner |
|---|---|---|
| ETL GUI rework target design (Step 13) | Pending Uddi's input | Uddi to come back with shape |
| Subscriber rules JSON (per-subscriber filtering of emails) | Documented as P2-A in `CLEAN_PIPELINE_REFACTOR.md` §9b. **Hook in place** at notify_subscribers (Step 9) but body is no-op in V1. Implement after V1 cutover stabilizes. | Future session |
| ML ensemble filter (reorder/filter signals before mail) | **Hook in place** at notify_subscribers (Step 9) but body is no-op in V1. Requires enrichment data — that's why enrichment runs in-cycle (per Uddi's directive). Implement after V1 cutover. | Future session |
| Lead/lag analytics workstream | Phases A-E, ~12 sessions per existing memory. Independent of V1 cutover; starts post-cutover. | Future session |
| Cosmetic /readyz fixes (null bools, /admin/health 404, bootstrap log handler) | Step 10 picks up the `/readyz` fix; others sweep up alongside | Step 10 |
| Multi-exchange (NSE + BSE + NYSE) | Phase 19; out of scope for V1 cutover | Future phase |
| Intraday timeframes | Phase 16; out of scope. `timeframe` column kept in V1 PKs for this reason. | Future phase |

---

## 5. Risk register

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Step 15 drops legacy tables — accidental data loss | Pre-cutover `pg_dump` in Step 17.1; populators give us the ability to rebuild V1 tables from scratch |
| R2 | V1 cycle produces different signals than legacy cycle (silent regression) | Run both pipelines in parallel for one cycle pre-cutover; diff the outputs. Halt if diff is non-trivial. Step 10 includes this verification. |
| R3 | SignalCache atomic swap races against concurrent reads | Step 5's stress test (100 reader threads × 10 rebuilds) |
| R4 | Brevo API down during email stage | `send_email` returns `SendResult.success=False`; logged; next cycle re-attempts (current_signals row is idempotent per cycle) |
| R5 | `recompute_market` takes longer than the inter-tick interval (≥ 165 min between ticks, plenty of headroom) | Measure first cycle's actual duration; monitor over first week post-cutover |
| R6 | Backfill (Step 11) breaks if run during the same time as a live cycle tick | Operator instruction: don't run backfill during market hours. Long-term: add an advisory lock that backfill acquires. |
| R7 | mypy / pyright errors balloon during refactor | Run after every step; reject step if errors increase |
| R8 | GUI rewire (Step 12) misses a code path; user sees stale data | The grep audit at the start of Step 12 must return zero in production code before tests are written |

---

## 6. Time budget (rough)

| Step | Sessions |
|---|---|
| 1 — Signal repos + SignalsReader | 1 |
| 2 — Cycle-incremental compute mode | 1 |
| 3 — Signal derivation | 0.5 |
| 4 — Rotation primitive | 0.5 |
| 5 — SignalCache rework | 1 |
| 6 — recompute_market | 2 |
| 7 — V1 email composer + sender | 1.5 |
| 8 — notification_log denormalization | 1 |
| 9 — notify_subscribers | 1 |
| 10 — MarketOrchestrator wiring + ComputeScheduler delete | 1 |
| 11 — Backfill rewire | 1 |
| 12 — GUI rewire (Ticker + Signals) | 1.5 |
| 13 — ETL GUI rework | TBD (1.5 est once design lands) |
| 14 — Legacy demolitions | 2 |
| 15 — Drop legacy tables | 0.5 |
| 16 — Documentation refresh | 1 |
| 17 — E2E smoke + cutover | 1 |
| **Total** | **~18 sessions** + Step 13 TBD |

---

## 7. Sign-off checklist (pre-merge to master at Step 17)

- [ ] All 17 steps' tests pass: `pytest tests/ -x`
- [ ] mypy clean: `mypy trip_ema/ tests/` → 0 errors
- [ ] pyright clean: `pyright trip_ema/` → 0 errors
- [ ] Full migration round-trip: `alembic upgrade head && alembic downgrade base && alembic upgrade head`
- [ ] Smoke test (Step 17) passes manually
- [ ] One email actually arrives at `uddipan@gmail.com` from the smoke cycle
- [ ] `psql` queries confirm row counts in V1 tables match expectations
- [ ] `indicator_state` + `signal_events` legacy tables don't exist post-Step-15
- [ ] No grep hits in `trip_ema/` for deleted symbols (`generate_signals_in_live_production`, `run_indicator_state_for_*`, `ComputeScheduler`, `IndicatorStateRepository`, `SignalEventRepository`, `subscriber_distributor`, `notifier_worker`)
- [ ] `docs/` refreshed; no stale references to dead modules
- [ ] `restart_service.sh` works against the new code
- [ ] MarketOrchestrator schedule (09:30 / 12:15 / 15:00 / 16:30 IST) ran cleanly for at least one full trading day post-merge

---

## 8. Naming guide (for posterity)

| V1 name | What it does | Rationale |
|---|---|---|
| `recompute_market` | Compute stage: stock state + enrichment incremental, rotate signals, derive signals, industry state + enrichment incremental | "Market" frames the dual purpose — indicators ARE the market view; signals are derivatives of it. Symmetric with `notify_subscribers`. |
| `notify_subscribers` | Distribute stage: compose digest from current_signals, future ML + rules filters, send via Brevo, log | Reads as the actual operator-level outcome. Doesn't leak implementation (no "Brevo", "Redis", "email") into the function name. |
| `current_signals` | This-cycle's signals only. PK is `ticker_id`. | Matches original vision. |
| `historical_signals` | Append-only audit trail of all signals ever. No uniqueness. | Matches original vision. |
| `SignalsReader` | Cache → historical fallback for last-N-signals queries | "Reader" hints that it's the only sanctioned consumer-facing read path; no UNION queries across tables. |
| `mode='live'` vs `mode='backfill'` | Single parameter on `recompute_market` controls rotation + write target | One primitive; two callers (orchestrator + backfill CLI). |

---

## 9. Living document

This file replaces `CLEAN_PIPELINE_REFACTOR.md` as the active plan. The original stays in repo as historical record of the foundation-building phase (it described Steps 0-8 in our actual numbering, which mapped to the foundation parts of its own 20-step plan).

Edits welcome as we discover constraints during the steps. Significant deviations are recorded in commit messages and surface back here.
