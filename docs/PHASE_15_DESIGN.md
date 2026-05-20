# Phase 15 — Historical Signal Generation for Backtesting

**Status:** Design locked. Ready to implement.
**Author of design:** Claude (in conversation with Uddi)
**Estimated scope:** Comparable in size to Phase 14.6 trilogy.
**Split:** 15a (engine + persistence) → 15b (HTTP) → 15c (GUI) → 15d (GUI endpoint audit)

---

## 0. How to use this document

This document is the complete specification for Phase 15. It assumes the
reader has access to the current `WeeklyStockSignals` codebase but no prior
context from the design conversation.

Read it top to bottom once. Section 3 is the requirements (what must be
true when Phase 15 is done). Section 4 is the architecture. Sections 5-8
are the per-phase delivery breakdowns with file lists.

The reader is **expected to**:

- Read the relevant code before writing (`trip_ema/engine/`, `trip_ema/etl/runner.py`,
  `trip_ema/transports/http/routes/`, `trip_ema/gui/views/`)
- Push back if any requirement is ambiguous or contradicts existing system
  behavior they discover while reading
- Ship one phase at a time, all tests green for that phase before starting
  the next
- Follow the established commit and bundle delivery conventions from the
  14.6 trilogy (one bundle per phase, mirror file paths, integration notes)

The reader is **not expected to**:

- Re-litigate design decisions captured in Section 2
- Add scope beyond what Section 3 specifies
- Change the live signal-generation path (compute_scheduler) — Phase 15
  is read-only against historical bars and writes only to its own new
  tables + JSON files

---

## 1. Context — what trip-ema is and what we're adding

### 1.1. The system

trip-ema is a Python service that ports a TradingView Pine Script indicator
("Triple-Trip") to a multi-exchange production setup. The relevant pieces:

- **Bars storage**: PostgreSQL table `ohlcv_bars(ticker_id, timeframe, bar_timestamp, OHLCV...)`.
  The system stores `1D` bars. `1W` (weekly) bars are synthesized in-memory
  from `1D` via `trip_ema/indicators/aggregation.py::daily_to_weekly` whenever
  the engine needs them. The `Timeframe` enum lists `15m/1h/2h/1D/1W` but
  only `1D` is actually populated; weekly is derived.

- **Engine**: `trip_ema/engine/ticker_engine.py::TickerEngine`. Stateful;
  takes a `TickerRef`, a `Timeframe`, and an `IndicatorParams`. Two main
  entry points: `prime(historical_bars)` for one-shot ingestion of a full
  history, and `ingest(bar)` for live bars. Emits a `ChangeSignal` per
  bar via the change classifier.

- **Signal types**: `ChangeSignal` enum has four values:
  `BUY` (display_bg → BULLISH), `SELL` (display_bg → BEARISH),
  `EXIT_FROM_BUY` (BULLISH → NEUTRAL), `EXIT_FROM_SELL` (BEARISH → NEUTRAL).
  Plus `NO_CHANGE` (not a signal event; not inserted).

- **Live signal storage**: `signal_events` table, append-only. **Critical:
  this table contains only signals fired by the live compute scheduler
  since the system started. It is NOT a complete historical signal
  record.** The compute scheduler only inserts a `signal_events` row when
  the *current* engine state transitions on the *latest* bar. It does not
  walk history.

- **Indicator parameters**: `IndicatorParams` dataclass (in
  `trip_ema/domain/state.py`). 17 fields covering three EMA stages
  (med/long/longest each with base, s1, s2, final), ATR length and band
  multipliers, volume SMA length, and one behavior flag. Today loaded
  globally from `config.yaml` via `Config.indicators.to_indicator_params()`;
  the same instance is shared across all tickers and timeframes.

- **Exchange registry**: `config.market_hours` lists configured exchanges.
  As of Phase 14.6, the `exchange_state` table tracks per-exchange ETL
  activity. The CLI and HTTP layers require `--exchange` / `?exchange=`
  on every action.

### 1.2. What Phase 15 adds

A backtest harness. Operators want to feed historical signals for an entire
exchange into an external backtester. Today there's no way to get that —
the engine runs live, `signal_events` is incomplete, and bars don't include
computed-signal annotations.

Phase 15 builds a **fresh-recompute pipeline**: take the historical bars
for an exchange, run them through a fresh `TickerEngine` per (ticker,
timeframe), capture every `ChangeSignal != NO_CHANGE` event, write the
results to a single JSON file per exchange. The output is the source of
truth a backtester consumes.

Because re-running the engine over 20+ years of bars × 2667+ tickers takes
significant time, the operation supports **per-ticker resume**: a tracking
table records what's been computed; subsequent runs only process new dates.
A `force_full=true` option bypasses resume for cases where the operator has
changed parameters and wants a clean recompute.

The result is shipped behind a long-running HTTP endpoint (`202 Accepted`,
async on `etl_executor`, status visible via the existing ETL runs surface)
and exposed in the GUI as a button on the Signals tab.

A separate sub-phase (15d) audits which existing HTTP endpoints lack GUI
surface and adds the missing ones where doing so is operationally useful.

---

## 2. Decisions locked from design conversation (DO NOT re-litigate)

These were settled. The implementing conversation should NOT re-open them.

| # | Decision | Rationale (brief) |
|---|----------|------------------|
| D1 | **Fresh engine replay**, not dump from `signal_events`. | `signal_events` is incomplete (only post-startup transitions). |
| D2 | **All four signal types** emitted (BUY, SELL, EXIT_FROM_BUY, EXIT_FROM_SELL). | Backtester needs the full state-machine transition log. |
| D3 | **Timeframes: 1D and 1W only in Phase 15.** | Storage today is 1D-only; 1W is synthesized in-memory. Intraday timeframes (15m/1h/2h) require a separate ETL change deferred to a future phase (informally "Phase 16"). |
| D4 | **Per-exchange engine parameters in config.yaml** (option 3a-ii from design conversation). | Operator wants timeframe-specific tuning that's persistent and version-controlled. Backtest uses these; the live engine will continue to use existing global config until a later phase migrates it. |
| D5 | **One JSON file per exchange**, keyed by date. | Backtester wants cross-ticker queries; per-ticker files would force a directory walk. |
| D6 | **In-memory merge protocol**: at run start, load existing JSON into memory; engine emits signals; for each `(ticker, timeframe, bar_timestamp)` produced in this run, insert/update/delete in the in-memory dict; at run end, write JSON atomically via temp-file + rename. | "One source of truth" semantics: re-runs preserve dates outside the recomputed window. Atomic write prevents corruption on crash. |
| D7 | **B1 resume tracking**: table `signal_backtest_runs(exchange, ticker_id, timeframe, computed_from_date, computed_through_date, computed_at)`. Resume runs use the existing `computed_through_date` to determine where to pick up. | Restartability without re-doing 30+ minutes of work. |
| D8 | **`force_full=true` flag** on the endpoint (default `false`). When true: WIPE resume table rows for that exchange in a small transaction at run start, then recompute full history. The in-memory JSON merge still applies — full recompute will overwrite all signals from those tickers. | Operator workflow when changing params: clean slate without manual table edits. |
| D9 | **No cancellation** in Phase 15. A force-full run that the operator regrets must run to completion or be killed at the OS level. Resume semantics make killed runs safe to restart. | Cancellation across thread + DB + filesystem is complex; not warranted for v1. |
| D10 | **Async execution** via existing `etl_executor`. `etl_runs` row with `kind='signal_backtest'`. Polling status via existing `/v1/admin/etl/status` (the runs table surfaces in there too). | Reuses Phase 14 infrastructure unchanged. |
| D11 | **No GUI restart-server button.** Operator restarts via `./scripts/restart_service.sh`. | GUI and server are not co-deployed in general; restart from GUI is fragile. |
| D12 | **GUI button on Signals tab** (not ETL Status). Adds picker for exchange + checkboxes for timeframes (1D / 1W) + force_full toggle. | Logical home; Signals tab is where signal data lives in operator's mental model. |
| D13 | **GUI endpoint audit (15d)** done as part of Phase 15, with recommendation per gap (add / leave / explicit-no). | Phase 15 is the natural break to do this. |

---

## 3. Requirements

Numbered functional and non-functional requirements. Each is testable.

### 3.1. Functional

- **F1.** A new CLI subcommand `trip-ema-etl generate-signals --exchange NSE [--timeframe 1D] [--timeframe 1W] [--force-full]` runs the signal generation synchronously and writes the JSON file. Default timeframes: `1W` only (matches live system primary). `--timeframe` may be passed multiple times.

- **F2.** A new HTTP endpoint `POST /v1/admin/signals/generate?exchange=NSE&force_full=false` accepts the request, validates the exchange against the configured set, creates an `etl_runs` row with `kind='signal_backtest'`, kicks off the work on `etl_executor`, and returns `202 Accepted` immediately with the `run_id` and the eventual output file path.

- **F3.** A new HTTP endpoint `GET /v1/admin/signals/generated?exchange=NSE` returns metadata about the most recent generated file (filename, size, mtime, generation_run_id, signal counts per type) — or 404 if no file exists for that exchange yet.

- **F4.** The signal-generation runner reads `ohlcv_bars` for the exchange's active tickers, replays each ticker through a fresh `TickerEngine` for each requested timeframe, captures every `ChangeSignal != NO_CHANGE` event, and merges into the in-memory representation of the exchange's signal file.

- **F5.** Resume semantics: for each `(ticker_id, timeframe)`, the runner consults `signal_backtest_runs`. If a row exists, the runner replays from `(computed_through_date + 1 day)`. If no row exists, the runner replays from the ticker's earliest bar. After replay, the runner upserts the row with the new `(computed_from_date, computed_through_date)`.

- **F6.** `force_full=true` semantics: in a small dedicated transaction at run start, `DELETE FROM signal_backtest_runs WHERE exchange = :exchange` and `COMMIT`. The subsequent replay then sees empty resume state and recomputes from earliest bar for every ticker.

- **F7.** JSON output format (see Section 4.3 for the exact schema):
  - Single file per exchange at `generated_signals/{EXCHANGE}.json`
  - Top-level metadata: exchange, generated_at, params (per-timeframe), counts
  - `by_date` dict keyed by ISO date; values are arrays of signal objects
  - Atomic write: temp file `{EXCHANGE}.json.tmp` then POSIX rename to `{EXCHANGE}.json`

- **F8.** Merge protocol: at run start, load existing JSON into memory (empty dict if missing). For each signal emitted during this run, key by `(ticker, timeframe, bar_timestamp)`. If the engine emits no signal for a date that previously had one (within the recomputed window for that ticker and timeframe), delete the prior signal from the in-memory representation. Dates outside the recomputed window are untouched.

- **F9.** Per-exchange indicator parameter configuration in `config.yaml`:
  ```yaml
  indicators:                # existing global block — unchanged
    ema: {...}
    atr: {...}
  exchange_overrides:        # new optional block (Phase 15 introduces)
    NSE:
      "1W":
        ema:
          med:  {base: 8, s1: 4, s2: 4}
          long: {base: 15, s1: 4, s2: 4, final: 3}
          longest: {base: 28, s1: 7, s2: 5, final: 3}
          volume_sma_length: 2
        atr:
          length: 14
          inner_mult: 0.2
          mid_mult: 2.0
          outer_mult: 4.0
      "1D":
        # same structure, possibly different values
        ema: {...}
        atr: {...}
    BSE: {...}
  ```
  If a `(exchange, timeframe)` is missing from `exchange_overrides`, fall back to the global `indicators` block. The signal generation pipeline ALWAYS uses these resolved params; the live engine continues to use the global block unchanged.

- **F10.** GUI: a "Generate signal history" button on the Signals tab. Required pickers: exchange (from `/v1/admin/exchanges`), timeframe(s) checkboxes (1D, 1W; 1W default-on), force_full checkbox (default off). Action button posts to `/v1/admin/signals/generate`; disabled until token configured AND exchange resolved.

- **F11.** GUI: a small panel on the Signals tab showing the most recent generation status per exchange — generation timestamp, file size, signal counts. Refreshes via `GET /v1/admin/signals/generated`.

- **F12.** GUI endpoint audit (15d): every HTTP endpoint exposed by the service is catalogued; for each, the GUI's current access surface is documented; gaps are listed; for each gap, a recommendation (add now / add later / explicit-no) is provided. Where the recommendation is "add now," the audit phase ships the GUI surface.

### 3.2. Non-functional

- **N1.** The signal generation runner must not block the service. All ETL operations must remain available during a backtest run. (Achieved via `etl_executor`.)

- **N2.** Killing the worker mid-run must leave the system in a recoverable state. Specifically:
  - The `etl_runs` row remains with `outcome=NULL` (in-flight) but no live worker; can be cleaned up manually by setting `outcome='failed'`
  - The `signal_backtest_runs` table reflects only the tickers that committed successfully (commit-per-ticker model)
  - The `generated_signals/{EXCHANGE}.json` file is unchanged (the temp file may exist but won't have replaced the real one)
  - The next run picks up where the killed run left off

- **N3.** The JSON file write must be atomic. A reader (the backtester) opening the file mid-write must see either the old version or the new version, never a half-written one.

- **N4.** Per-exchange JSON file size: must scale to at least 100MB without breaking. (Estimate: NSE 1W history is ~20-50MB. Phase 15 is sized for one exchange at a time; multi-exchange aggregation is out of scope.)

- **N5.** Memory: the runner loads the full existing JSON into memory at start. For files up to 200MB JSON (≈600MB parsed), the runner must complete on a host with 4GB RAM available to the service process.

- **N6.** The generate-signals path must not modify `signal_events`, `ticker_signal_state`, or any other table the live system writes to. Its writes are confined to `signal_backtest_runs`, `etl_runs`, and the JSON file on disk.

- **N7.** No regressions in the existing test suite. Phase 15 adds tests; existing tests stay green (modulo the pre-existing `tests/gui/test_signals.py` flake that's `--deselect`-ed by convention).

### 3.3. Out of scope

Explicitly NOT in Phase 15. If the implementing conversation considers any of these, push back and document the trade-off rather than building.

- **OOS-1.** Intraday timeframes (15m, 30m, 45m, 1h, 2h, 4h). Requires ETL storage changes; deferred.
- **OOS-2.** Per-ticker parameter tuning (params can differ across timeframes within an exchange, but all tickers on an exchange use the same params for a given timeframe).
- **OOS-3.** Cancellation of in-flight runs.
- **OOS-4.** Multi-exchange aggregation (no single JSON across NSE + BSE; one file per exchange).
- **OOS-5.** Streaming JSON output (must fit in memory; not a JSON-streaming protocol).
- **OOS-6.** Backtest *execution* — Phase 15 produces the signal log, not the P&L of a strategy. The backtester is external.
- **OOS-7.** Live signal generation switching to per-exchange params. The live engine continues using the global `indicators` block from config.yaml. Migrating it is a separate phase.
- **OOS-8.** GUI restart-server button.
- **OOS-9.** Any change to the engine itself. The engine's behavior must be byte-identical to current production behavior given the same params and bars.

---

## 4. Architecture

### 4.1. Data model changes

#### 4.1.1. `signal_backtest_runs` (NEW table — Alembic 0005)

Resume tracking. One row per `(exchange, ticker_id, timeframe)` ever computed.

```sql
CREATE TABLE IF NOT EXISTS signal_backtest_runs (
    exchange                TEXT         NOT NULL,
    ticker_id               INTEGER      NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe               TEXT         NOT NULL,    -- '1D' or '1W'
    computed_from_date      DATE         NOT NULL,
    computed_through_date   DATE         NOT NULL,
    computed_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_run_id             BIGINT,                   -- FK to etl_runs (no enforced FK; etl_runs may be truncated)

    PRIMARY KEY (exchange, ticker_id, timeframe),

    CONSTRAINT signal_backtest_runs_date_order_chk
        CHECK (computed_through_date >= computed_from_date),

    CONSTRAINT signal_backtest_runs_timeframe_chk
        CHECK (timeframe IN ('1D', '1W'))
);

CREATE INDEX IF NOT EXISTS idx_signal_backtest_runs_exchange
    ON signal_backtest_runs (exchange);

COMMENT ON TABLE signal_backtest_runs IS
    'Per-(exchange, ticker_id, timeframe) resume tracking for backtest signal generation. Allows incremental runs to skip dates already computed. force_full=true wipes rows for the affected exchange in a small transaction at run start.';
```

**Grants** (mirror the pattern from `exchange_state` in Alembic 0004):
- `trip_etl_user`: SELECT, INSERT, UPDATE, DELETE
- `trip_engine_user`: SELECT
- `trip_gui_user`: SELECT

**Legacy parity:** `migrations/legacy/012_signal_backtest_runs.sql` is the bit-equivalent for the parity test. Same schema, same CHECK constraints, same grants. Mandatory — the parity guard test fails otherwise (per Phase 14.6b precedent).

#### 4.1.2. `etl_runs` (NO schema change; new value used)

The existing `etl_runs.kind` column accepts free text. Phase 15 uses `kind='signal_backtest'`. No constraint changes needed. The runner writes one row per generate-signals operation, links the in-flight worker via `correlation_id`, and updates `outcome` + `finished_at` when complete.

#### 4.1.3. Config schema additions (`config.yaml`)

New optional top-level block `exchange_overrides`:

```yaml
exchange_overrides:
  NSE:                              # exchange code (must match market_hours.exchange)
    "1W":                           # timeframe — string form, "1D" or "1W"
      ema:
        med:     { base: 8,  s1: 4, s2: 4 }
        long:    { base: 15, s1: 4, s2: 4, final: 3 }
        longest: { base: 28, s1: 7, s2: 5, final: 3 }
        volume_sma_length: 2
      atr:
        length: 14
        inner_mult: 0.2
        mid_mult: 2.0
        outer_mult: 4.0
    "1D":
      ema: { ... }
      atr: { ... }
  BSE:
    # If only some timeframes are overridden, the others fall back to
    # the global `indicators` block. Same applies if a whole exchange
    # is omitted from exchange_overrides.
    "1W":
      ema: { ... }
      atr: { ... }
```

**Resolution rule, in code:**

```python
def resolve_indicator_params(
    config: Config,
    exchange: str,
    timeframe: Timeframe,
) -> IndicatorParams:
    """Returns the IndicatorParams to use for (exchange, timeframe).
    Precedence:
      1. config.exchange_overrides[exchange][timeframe.value] if present
      2. config.indicators (global)
    """
    ...
```

**Backward compatibility:** an existing `config.yaml` with no `exchange_overrides` block keeps working unchanged. The function returns the global params for every `(exchange, timeframe)`.

**Schema additions** (in `trip_ema/config/schema.py`):
- New `ExchangeIndicatorOverride(BaseModel)` — one timeframe's full set (EMA + ATR config). Same fields as existing global blocks.
- New `ExchangeOverrideConfig(BaseModel)` — a dict mapping timeframe-string ("1D", "1W") to `ExchangeIndicatorOverride`.
- `Config.exchange_overrides: dict[str, ExchangeOverrideConfig] | None = None`

Validation:
- Reject unknown timeframes (must be "1D" or "1W")
- Reject exchanges not present in `market_hours`
- Validation runs at config load time; bad config → service fails to start with a clear error message

### 4.2. Engine replay architecture

#### 4.2.1. Module layout

New module: `trip_ema/etl/signal_generator.py`

Public functions:

```python
def generate_historical_signals(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    timeframes: Iterable[Timeframe],
    force_full: bool = False,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> SignalGenerationResult:
    """Recompute historical signals for an entire exchange across the
    given timeframes. See module docstring for full semantics."""
    ...
```

`SignalGenerationResult` dataclass:
```python
@dataclass(frozen=True)
class SignalGenerationResult:
    exchange: str
    run_id: int
    output_path: Path
    timeframes_computed: list[str]
    tickers_processed: int
    tickers_skipped: int          # nothing new to compute
    signals_emitted: dict[str, int]   # by signal_type
    signals_deleted: int          # merge: prior records removed
    started_at: datetime
    finished_at: datetime
```

#### 4.2.2. Replay flow per ticker × timeframe

```
For each ticker in active(exchange):
    For each tf in requested_timeframes:
        # 1. Resume check
        if not force_full:
            row = read signal_backtest_runs (exchange, ticker_id, tf)
            if row exists:
                start_date = row.computed_through_date + 1 day
            else:
                start_date = earliest bar date for (ticker, '1D') in DB
        else:
            start_date = earliest bar date for (ticker, '1D') in DB

        # 2. Fetch bars
        bars_1d = ohlcv_bars where ticker_id and timeframe='1D' and bar_timestamp >= some lookback
        # The "lookback" extends start_date backward by enough bars to prime
        # the engine. The engine needs ~30 weekly bars to settle indicators;
        # at 5 bars/week, that's ~150 daily bars of priming.

        if tf == '1W':
            bars_used = daily_to_weekly(bars_1d, exchange_tz=...)
        else:
            bars_used = bars_1d

        if not bars_used or all bars_used.timestamp < start_date:
            skip ticker, log "no new bars"
            continue

        # 3. Engine replay
        params = resolve_indicator_params(config, exchange, tf)
        engine = TickerEngine(ticker_ref, tf, params)

        # Replay bars in order. Capture every bar where
        # state.change_signal != NO_CHANGE.
        emitted_events = []
        for bar in bars_used:
            state = engine.ingest(bar)
            if state.change_signal != NO_CHANGE and bar.timestamp.date() >= start_date:
                emitted_events.append(SignalEvent(
                    ticker_ref=ticker_ref,
                    timeframe=tf,
                    bar_timestamp=bar.timestamp,
                    signal=state.change_signal,
                    price_at_signal=state.close,   # raw close, Pine-parity
                    computed_at=NOW,
                ))

        # 4. Merge into in-memory JSON dict
        merge_into_in_memory(
            in_memory_dict,
            ticker=ticker_ref.symbol,
            timeframe=tf,
            recomputed_window=(start_date, bars_used[-1].date),
            new_events=emitted_events,
        )

        # 5. Commit resume row (per-ticker commit, NOT one big transaction)
        with db_engine.begin() as conn:
            upsert signal_backtest_runs (exchange, ticker_id, tf,
                computed_from_date = COALESCE(existing.computed_from_date, start_date_of_engine_history),
                computed_through_date = bars_used[-1].date,
                computed_at = NOW,
                last_run_id = run_id)

# End of ticker loop:
# 6. Atomic write
write in_memory_dict → generated_signals/{EXCHANGE}.json.tmp
fsync the tmp file
os.rename(tmp_path, final_path)

# 7. Close run
with db_engine.begin() as conn:
    update etl_runs set outcome='success', finished_at=NOW where run_id=...
```

#### 4.2.3. Merge protocol details

The in-memory representation:

```python
in_memory: dict = {
    "exchange": "NSE",
    "generated_at": "<ISO>",
    "params": { "1W": {...}, "1D": {...} },
    "by_date": {
        "2018-03-15": [
            { "ticker": "RELIANCE", "timeframe": "1W", "signal": "BUY",
              "price_at_signal": 945.5, "bar_timestamp": "2018-03-15T00:00:00+00:00" },
            ...
        ],
        ...
    }
}
```

Merge for a `(ticker, timeframe, recomputed_window=(start, end))`:

```python
# 1. Build a lookup of the new events by date
new_by_date: dict[str, dict] = {}   # date → record
for ev in new_events:
    new_by_date[ev.bar_timestamp.date().isoformat()] = build_record(ev)

# 2. Walk the in-memory dict for this ticker+timeframe in the window;
#    update or delete as appropriate
for date_str, signals_list in list(in_memory["by_date"].items()):
    d = date.fromisoformat(date_str)
    if not (start <= d <= end):
        continue
    # Filter out existing records for (ticker, timeframe) in this window
    new_list = [s for s in signals_list
                if not (s["ticker"] == ticker and s["timeframe"] == timeframe.value)]
    # If the engine emitted a NEW signal for this date, add it
    if date_str in new_by_date:
        new_list.append(new_by_date[date_str])
        del new_by_date[date_str]   # consumed
    in_memory["by_date"][date_str] = new_list
    if not new_list:
        del in_memory["by_date"][date_str]  # tidy

# 3. Any new_by_date entries left over are signals for dates not previously
#    represented; add them
for date_str, rec in new_by_date.items():
    in_memory["by_date"].setdefault(date_str, []).append(rec)
```

This produces correct behavior for all three transitions:
- **Date had no prior signal, engine emits one now** → added
- **Date had a prior signal, engine emits a different one (or same one)** → replaced
- **Date had a prior signal, engine emits NO signal now** → removed

Tests must cover all three transitions explicitly.

#### 4.2.4. Force-full path

In `generate_historical_signals`, before any ticker work:

```python
if force_full:
    logger.info("force_full=true: wiping signal_backtest_runs for exchange=%s", exchange)
    with db_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM signal_backtest_runs WHERE exchange = :ex"),
            {"ex": exchange}
        )
    # COMMIT is implicit at end of `with`.
```

This is a single small transaction. If the DELETE fails (constraint violation, etc.), nothing else has changed and the operator gets a clean error. If everything that comes after fails, the resume table is already empty for this exchange, which is the correct state for "force-full from scratch" recovery.

The in-memory JSON merge still applies: previously-computed signals for the affected tickers and timeframes will be overwritten as each ticker is processed. Signals for tickers that DON'T get re-processed (e.g., a ticker no longer in `active`) remain in the JSON. This is intentional — a delisted ticker's old signals are still useful historical data.

If the operator wants a completely clean JSON file, they can delete `generated_signals/{EXCHANGE}.json` manually before running force-full. The runner treats a missing file as "empty in-memory dict."

### 4.3. JSON file format

Exact schema, with every field documented. This is the contract a backtester reads against; treat it as stable.

```json
{
  "schema_version": "1.0",
  "exchange": "NSE",
  "generated_at": "2026-05-21T10:30:00.123456+00:00",
  "generated_by_run_id": 17,

  "params": {
    "1W": {
      "ema": {
        "med":     { "base": 8,  "s1": 4, "s2": 4, "final": null },
        "long":    { "base": 15, "s1": 4, "s2": 4, "final": 3 },
        "longest": { "base": 28, "s1": 7, "s2": 5, "final": 3 },
        "volume_sma_length": 2
      },
      "atr": {
        "length": 14,
        "inner_mult": 0.2,
        "mid_mult": 2.0,
        "outer_mult": 4.0
      }
    },
    "1D": { ... same structure ... }
  },

  "counts": {
    "tickers": 2667,
    "signals_total": 184523,
    "signals_by_type": {
      "BUY": 45132,
      "SELL": 44897,
      "EXIT_FROM_BUY": 47218,
      "EXIT_FROM_SELL": 47276
    },
    "first_date": "2006-01-09",
    "last_date":  "2026-05-19"
  },

  "by_date": {
    "2006-01-09": [
      {
        "ticker": "RELIANCE",
        "timeframe": "1W",
        "signal": "BUY",
        "price_at_signal": 945.5,
        "bar_timestamp": "2006-01-09T00:00:00+00:00"
      },
      {
        "ticker": "TCS",
        "timeframe": "1W",
        "signal": "SELL",
        "price_at_signal": 1245.0,
        "bar_timestamp": "2006-01-09T00:00:00+00:00"
      }
    ],
    "2006-01-16": [ ... ]
  }
}
```

**Field rules:**

- `schema_version` — string, semver-ish. Starts at "1.0". Bump on incompatible changes.
- `exchange` — string, matches `tickers.exchange`.
- `generated_at` — ISO-8601 UTC, with timezone offset. Time of file write.
- `generated_by_run_id` — integer, the `etl_runs.run_id` of this generation.
- `params` — dict keyed by timeframe string ("1W", "1D"). Each value is the resolved `IndicatorParams` used for that timeframe (after `resolve_indicator_params`). Captures the parameter set used at generation time for reproducibility.
- `counts.tickers` — number of distinct tickers in the file.
- `counts.signals_total` — total entries across all `by_date` arrays.
- `counts.signals_by_type` — breakdown by `signal_type`.
- `counts.first_date` / `counts.last_date` — earliest and latest `by_date` keys.
- `by_date` — dict keyed by ISO date string (YYYY-MM-DD). Each value is a non-empty array of signal records.

**Per-signal record fields:**
- `ticker` — string, the symbol (NOT `ticker_id`; backtester-friendly).
- `timeframe` — string, "1W" or "1D".
- `signal` — string, one of "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL".
- `price_at_signal` — float, the raw close price at the bar that triggered the signal (Pine-parity: NOT adjusted close).
- `bar_timestamp` — ISO-8601 UTC, the bar's start timestamp.

**Sort order:**
- Top-level `by_date` keys: implicit (JSON object ordering preserved in Python 3.7+); but readers should treat as unordered and sort if they need chronological.
- Within each date's array: order by `(ticker, timeframe)` for stable diffs across runs.

### 4.4. HTTP endpoint contracts

#### 4.4.1. `POST /v1/admin/signals/generate`

```
Auth: Bearer token (admin) — same gate as other /v1/admin/* routes.

Query params:
  exchange    string, required. Must be in configured_exchanges(config).
  force_full  bool, optional, default false.
  timeframe   string, optional, repeatable. Default ["1W"]. Allowed: "1D", "1W".

Body: none.

Response 202 Accepted:
{
  "run_id": 17,
  "exchange": "NSE",
  "timeframes": ["1W"],
  "force_full": false,
  "estimated_output_path": "generated_signals/NSE.json",
  "submitted_at": "2026-05-21T10:30:00+00:00"
}

Response 400 Bad Request:
  - unknown exchange
  - unknown timeframe in ?timeframe
  - exchange not in market_hours

Response 401 Unauthorized: missing/wrong token
Response 409 Conflict:
{
  "detail": "A signal_backtest run is already in flight for exchange='NSE' (run_id=15). Wait for completion or kill the worker."
}
Response 503 Service Unavailable: etl_executor unavailable.
```

**Implementation outline** (in a new router `trip_ema/transports/http/routes/admin_signals.py`):

```python
@router.post("/generate", response_model=SignalGenerateAcceptedPayload, status_code=202)
def admin_signals_generate(
    exchange: str = Query(..., min_length=2, max_length=10),
    force_full: bool = Query(False),
    timeframe: list[str] = Query(default=["1W"]),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor: Executor = Depends(get_etl_executor),
):
    # 1. Validate exchange
    if exchange not in configured_exchanges(config):
        raise HTTPException(400, f"unknown or unconfigured exchange: {exchange}")
    # 2. Validate timeframes
    tfs = [Timeframe(t) for t in timeframe]  # raises ValueError on bad input
    for tf in tfs:
        if tf not in (Timeframe.DAILY, Timeframe.WEEKLY):
            raise HTTPException(400, f"unsupported timeframe in Phase 15: {tf.value}")
    # 3. Concurrent-run guard
    with db_engine.connect() as conn:
        existing = conn.execute(text("""
            SELECT run_id FROM etl_runs
            WHERE kind = 'signal_backtest'
              AND exchange = :ex
              AND outcome IS NULL
            ORDER BY started_at DESC LIMIT 1
        """), {"ex": exchange}).first()
        if existing:
            raise HTTPException(409, f"A signal_backtest run is already in flight for exchange='{exchange}' (run_id={existing.run_id}).")
    # 4. Open the etl_runs row
    correlation_id = str(uuid.uuid4())
    with db_engine.begin() as conn:
        run_id = conn.execute(text("""
            INSERT INTO etl_runs (kind, exchange, started_at, correlation_id, notes)
            VALUES ('signal_backtest', :ex, NOW(), :cid, :notes)
            RETURNING run_id
        """), {"ex": exchange, "cid": correlation_id,
               "notes": f"force_full={force_full}, timeframes={[t.value for t in tfs]}"}).scalar_one()
    # 5. Submit to executor
    executor.submit(_run_in_background, db_engine, config, exchange, tfs, force_full, run_id)
    # 6. Return 202
    return SignalGenerateAcceptedPayload(
        run_id=run_id, exchange=exchange,
        timeframes=[t.value for t in tfs],
        force_full=force_full,
        estimated_output_path=f"generated_signals/{exchange}.json",
        submitted_at=datetime.now(timezone.utc),
    )
```

#### 4.4.2. `GET /v1/admin/signals/generated`

```
Auth: Bearer token (admin).

Query params:
  exchange  string, required.

Response 200 OK:
{
  "exchange": "NSE",
  "exists": true,
  "filename": "NSE.json",
  "size_bytes": 41928374,
  "mtime": "2026-05-21T10:45:23.123456+00:00",
  "generated_by_run_id": 17,
  "schema_version": "1.0",
  "counts": { ... full counts subdict from the JSON ... }
}

Response 200 OK (file absent):
{
  "exchange": "NSE",
  "exists": false
}

Response 400 Bad Request: unknown exchange
Response 401 Unauthorized: missing/wrong token
```

When the file exists, the route reads ONLY the metadata fields (schema_version, generated_at, params, counts, generated_by_run_id) — it does NOT read or stream `by_date`. This keeps the endpoint fast even for large files. Implementation: use `ijson` or manual JSON parsing to extract just the top-level fields; OR rely on the file being small enough to load whole (200MB still loads in <1s and is server-side, not network-side).

Defer the streaming-decode approach unless file sizes exceed 200MB in practice.

#### 4.4.3. Route registration

Add the new router to `trip_ema/transports/http/app.py`:

```python
from .routes import (
    admin, admin_etl, admin_exchange_state, admin_exchanges, admin_health,
    admin_notifications, admin_orchestrator, admin_signals, admin_subscribers,
    compute, health, pulse, signals,
)
...
app.include_router(admin_signals.router)
```

### 4.5. GUI changes (Phase 15c)

#### 4.5.1. Signals tab additions

Layout — added BELOW the existing chart area:

```
┌─ Signals view (existing chart + controls) ───────────────┐
│                                                          │
│  [existing chart + ticker picker + range buttons]        │
│                                                          │
├──────────────────────────────────────────────────────────┤
│ ─ Generate backtest signal history ──────────────────── │
│                                                          │
│ Exchange:   [NSE ▼]                                      │
│ Timeframes: [✓] 1W  [ ] 1D                              │
│ Mode:       (•) Incremental (•) Force full              │
│                                                          │
│           [ Generate signals ]                           │
│                                                          │
│ ─ Last generation per exchange ─────────────────────── │
│                                                          │
│ NSE:  41.9 MB  ·  2026-05-21 10:45  ·  184k signals     │
│ BSE:  no file yet                                        │
│ NYSE: no file yet                                        │
└──────────────────────────────────────────────────────────┘
```

A separate `QGroupBox` widget — `SignalGenerationPanel` in `trip_ema/gui/views/signal_generation_panel.py`. Owns:
- The exchange picker (reuses the picker from ETL Status view via a helper extraction — see implementation note below)
- Two timeframe checkboxes (1W on by default)
- Incremental/force-full radio
- The generate button (gated like Phase 14.6c's pattern: `token AND exchange_resolved`)
- A "last generation per exchange" grid that polls `/v1/admin/signals/generated` for each configured exchange every ~30s

**Implementation note for the exchange picker reuse:** Phase 14.6c put the picker logic inline in `EtlStatusView`. Phase 15c should extract a small reusable `ExchangePicker` widget (`trip_ema/gui/views/widgets/exchange_picker.py`) that wraps the QComboBox + status hint + populate-from-list_exchanges worker pattern. EtlStatusView gets refactored to use it; SignalGenerationPanel uses it too. Single source of picker truth.

Test coverage for the extracted picker: a new pure-ish test file mocking the http client and verifying the populate / save-pref / restore-pref behavior, separately from any specific consuming view.

#### 4.5.2. Action flow

User clicks **Generate signals**:

1. Confirm dialog if `force_full=true`:
   > "Force-full will recompute all historical signals for {EXCHANGE} and overwrite existing entries in {filename}. This may take 30+ minutes. Continue?"
   > [ Cancel ] [ Proceed ]
2. POST `/v1/admin/signals/generate?exchange=NSE&force_full=...&timeframe=...`
3. Success (202): show non-modal toast "Backtest started for NSE (run_id=17). Watch progress in the ETL Status tab." Refresh the "last generation per exchange" panel after 5s.
4. 409 Conflict: show modal warning "A backtest is already running for NSE (run_id=15). Wait for completion."
5. Other errors: show modal warning with the server's `detail` text.

#### 4.5.3. Polling

The "last generation per exchange" panel polls `/v1/admin/signals/generated` for each configured exchange every 30 seconds. Each row updates independently. If a 404 (no file), the row says "no file yet."

For each row that has a file, the panel shows: `{EXCHANGE}: {size} · {mtime} · {signals_total} signals`.

When a generation completes (the run becomes visible in the ETL Status view's "recent runs"), the panel will refresh on its next poll and show the new file. No need for special notification wiring.

### 4.6. GUI endpoint audit (Phase 15d)

This is a documentation + small-additions phase. See Section 8 for the audit table and recommendations. The deliverable is:

1. An updated `scripts/operator_commands.md` or new `docs/gui_endpoint_audit.md` capturing every endpoint with its GUI surface.
2. For each endpoint flagged "add now" in the audit (Section 8), the corresponding small GUI addition.
3. Tests for any new GUI additions.

---

## 5. Phase 15a — Engine + persistence

### 5.1. Files NEW

| Path | Purpose |
|---|---|
| `alembic_pkg/versions/0005_signal_backtest_runs.py` | Alembic migration for the new table |
| `migrations/legacy/012_signal_backtest_runs.sql` | Legacy parity parallel (mandatory for parity test) |
| `trip_ema/persistence/signal_backtest_runs_repo.py` | Repository for the new table — upsert, get, list, delete-by-exchange |
| `trip_ema/etl/signal_generator.py` | The replay runner — `generate_historical_signals()` + helpers |
| `trip_ema/config/exchange_overrides.py` | `resolve_indicator_params(config, exchange, timeframe)` helper |
| `tests/persistence/test_signal_backtest_runs_repo.py` | Repo unit tests |
| `tests/etl/test_signal_generator.py` | Replay logic unit tests with synthetic data |
| `tests/etl/test_signal_generator_merge.py` | Dedicated tests for the in-memory merge protocol |
| `tests/config/test_exchange_overrides.py` | resolve_indicator_params tests |

### 5.2. Files MODIFIED

| Path | Change |
|---|---|
| `trip_ema/config/schema.py` | Add `ExchangeIndicatorOverride`, `ExchangeOverrideConfig`, `Config.exchange_overrides`. Existing fields and defaults unchanged. |
| `trip_ema/etl/cli.py` | New subcommand `generate-signals --exchange NSE [--timeframe 1D] [--timeframe 1W] [--force-full]`. Default timeframes: `["1W"]`. |
| `migrations/legacy/README.md` | Document the new 012 parity file. |
| `config.yaml` (sample/example only — DO NOT change live config) | Add a commented example `exchange_overrides:` block with the global defaults reproduced. |

### 5.3. Tests required for 15a

| Test class | Coverage |
|---|---|
| `TestSignalBacktestRunsRepo` | upsert (insert vs update), get_for_exchange, get_for_ticker_timeframe, delete_by_exchange (force-full path), CHECK constraint enforcement |
| `TestExchangeOverridesResolution` | No overrides → returns global. Override present → returns override. Per-timeframe granularity. Unknown exchange → falls back. Unknown timeframe in YAML → config rejected at load. |
| `TestSignalGeneratorReplay` | Synthetic bars → expected ChangeSignal sequence. Run twice without force_full → second run no-ops (everything in resume table). Run twice WITH force_full → resume table cleared, full recompute. |
| `TestSignalGeneratorMerge` | Three transitions explicitly: new-date-no-prior, date-with-prior-changed, date-with-prior-now-empty. Plus: dates outside recomputed window are not touched. Plus: empty initial file ok. |
| `TestSignalGeneratorAtomicWrite` | Inject a crash between temp-file write and rename → existing file unchanged. Inject a crash mid-temp-write → no half-state in final file. |
| `TestSignalGeneratorCli` | `generate-signals` subcommand wired correctly. `--force-full` propagates. Multiple `--timeframe` flags parsed correctly. |

### 5.4. Definition of done for 15a

- All tests in 5.3 pass
- Existing test suite stays green (1138 passed, modulo the test_signals deselect)
- `alembic upgrade head` succeeds on a clean DB and produces the new table
- `migrations/legacy/test_legacy_migrator.py::test_alembic_and_legacy_produce_same_tables` still passes (the parity test that bit us in 14.6b)
- `trip-ema-etl generate-signals --exchange NSE` runs end-to-end against a real DB and produces `generated_signals/NSE.json`
- Re-running the same command without `--force-full` is a no-op (`tickers_skipped == tickers_processed`)
- Running with `--force-full` recomputes everything and overwrites the file
- The CLI is the gold standard — HTTP and GUI come later but the CLI must be fully working at end of 15a

### 5.5. Single commit message template for 15a

```
Phase 15a: signal_backtest_runs table + engine replay + CLI

[summary of what's in, what's intentionally out]
[note the schema migration + parity SQL]
[note the new resolve_indicator_params helper and config.yaml backward compat]
[note the CLI subcommand]
[final test count]
```

---

## 6. Phase 15b — HTTP endpoints

### 6.1. Files NEW

| Path | Purpose |
|---|---|
| `trip_ema/transports/http/routes/admin_signals.py` | The two new endpoints (POST /generate, GET /generated) |
| `tests/transports_pure/test_admin_signals.py` | Pure tests with mocked DB engine and etl_executor |

### 6.2. Files MODIFIED

| Path | Change |
|---|---|
| `trip_ema/transports/http/schemas.py` | Add `SignalGenerateAcceptedPayload`, `SignalGeneratedFilePayload` (or similar for the GET response) |
| `trip_ema/transports/http/app.py` | Import + mount the new router |

### 6.3. Tests required for 15b

| Test class | Coverage |
|---|---|
| `TestAdminSignalsGenerate` | Happy path returns 202 with run_id and estimated_output_path. Unknown exchange → 400. Unknown timeframe → 400. Concurrent-run conflict → 409. Auth missing → 401. Verifies etl_runs row created with kind='signal_backtest'. Verifies executor.submit called with right args. |
| `TestAdminSignalsGenerated` | Happy path returns 200 with metadata. File missing → 200 with `exists: false`. Unknown exchange → 400. Auth missing → 401. |

### 6.4. Definition of done for 15b

- All 15b tests pass (~10-12 new test cases)
- Curl-driven smoke test against running service works:
  ```
  curl -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
       "http://localhost:8000/v1/admin/signals/generate?exchange=NSE&force_full=false"
  # → 202
  curl -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
       "http://localhost:8000/v1/admin/signals/generated?exchange=NSE"
  # → 200 with metadata
  ```
- `scripts/operator_commands.md` updated with the new endpoints in Section 4 (Daily updates) and Section 8 (ETL status snapshots)

---

## 7. Phase 15c — GUI button on Signals tab

### 7.1. Files NEW

| Path | Purpose |
|---|---|
| `trip_ema/gui/views/widgets/__init__.py` | New widgets subpackage (init file) |
| `trip_ema/gui/views/widgets/exchange_picker.py` | Extracted reusable ExchangePicker widget |
| `trip_ema/gui/views/signal_generation_panel.py` | The new panel widget for the Signals tab |
| `tests/gui/test_exchange_picker_widget.py` | Tests for the extracted widget |
| `tests/gui/test_signal_generation_panel.py` | Tests for the new panel |

### 7.2. Files MODIFIED

| Path | Change |
|---|---|
| `trip_ema/gui/views/etl_status.py` | Refactor to use the extracted ExchangePicker widget. Preserve all 14.6c behavior. |
| `trip_ema/gui/views/signals.py` | Embed SignalGenerationPanel below the existing chart layout |
| `trip_ema/gui/http_client.py` | Add `generate_signals(exchange, timeframes, force_full)`, `get_signal_generation_metadata(exchange)` methods |
| `tests/gui/test_http_client.py` | Mirror updates |
| `tests/gui_pure/test_http_client.py` | Mirror updates |

### 7.3. Tests required for 15c

Following the patterns established in Phase 14.6c (`isHidden()`, not `isVisible()`; `silent_modals` fixture; try/finally with `_teardown_view` to drain `QThreadPool`):

| Test class | Coverage |
|---|---|
| `TestExchangePickerWidget` | Loading sentinel → populated → selection-changed signal. Restore from QSettings. Falls back when saved pref no longer configured. (Mirrors and consolidates the 14.6c picker tests.) |
| `TestSignalGenerationPanel` | Constructs cleanly. Generate button disabled until exchange resolved AND timeframe checked AND token present. Force-full confirmation dialog (use the `silent_modals` fixture variant that returns Yes / No). |
| `TestActionDispatchPostsCorrectQueryParams` | Generate with `1W` only → POSTs with one timeframe. Generate with `1D + 1W` → POSTs both. Force-full toggle propagates. |
| `TestLastGenerationPanelRefresh` | Mock the http client's metadata endpoint; verify the panel shows what the server returns; 404 → "no file yet". |

### 7.4. Definition of done for 15c

- All 15c tests pass
- The full GUI suite still passes (with the known test_signals deselect)
- Manual test: open GUI, click "Generate signals" → backend run starts → ETL Status tab shows the in-flight run → completes → Signals tab's "last generation per exchange" panel updates
- ETL Status view's picker behavior is unchanged (no regression from the refactor to the extracted widget)

---

## 8. Phase 15d — GUI endpoint audit

### 8.1. The audit

Every HTTP route exposed by the service, its current GUI surface, and a recommendation. The recommendation column drives 15d's additions.

| Endpoint | Method | Current GUI access | Recommendation |
|---|---|---|---|
| `/healthz` | GET | Status bar reflects implicitly (connection indicator) | **Leave.** No need for a separate button. |
| `/readyz` | GET | None | **Leave.** Diagnostic only; ops uses curl. |
| `/v1/admin/etl/run-eod` | POST | EtlStatusView "Run EoD" button | OK |
| `/v1/admin/etl/run-intraday` | POST | EtlStatusView "Run Intraday" button | OK |
| `/v1/admin/etl/backfill` | POST | **None** | **Add now.** Operator workflow: occasional backfill from a specific date. Add a small modal triggered by an EtlStatusView "Backfill…" button that takes a date and optional tier. |
| `/v1/admin/etl/refresh-listings` | POST | EtlStatusView "Refresh Listings" button | OK |
| `/v1/admin/etl/recompute-tiers` | POST | EtlStatusView "Recompute Tiers" button | OK |
| `/v1/admin/etl/unpark-all` | POST | EtlStatusView "Unpark All" button | OK |
| `/v1/admin/etl/status` | GET | EtlStatusView periodic poll | OK |
| `/v1/admin/etl/parked` | GET | EtlStatusView parked list | OK |
| `/v1/admin/exchange-state` (list + per-exchange) | GET | EtlStatusView exchange state panel | OK |
| `/v1/admin/exchanges` | GET | EtlStatusView picker, Signals tab picker | OK |
| `/v1/admin/orchestrator/status` | GET | **None** | **Add now.** Already exposed in System Health tab as a card. Verify it's there; if not, add. |
| `/v1/admin/orchestrator/{ex}/run-now` | POST | **None** | **Add later.** A "force tick now" button would be useful operationally but feels niche; defer unless workflow demands it. Capture as explicit TODO. |
| `/v1/admin/orchestrator/{ex}/pause` | POST | **None** | **Add later.** Same reasoning as run-now. |
| `/v1/admin/orchestrator/{ex}/resume` | POST | **None** | **Add later.** Same reasoning. |
| `/v1/admin/subscribers/*` | GET/POST/PATCH/DELETE | Subscribers tab | OK (verify all CRUD operations are accessible) |
| `/v1/admin/notifications/recent` | GET | Notifications tab | OK |
| `/v1/admin/notifications/dlq` | GET | Notifications tab DLQ panel | OK (verify) |
| `/v1/admin/health` (the health snapshot) | GET | System Health tab | OK |
| `/v1/admin/circuit-breakers` | GET | System Health tab (verify) | **Add now if missing.** Status only; not actions. |
| `/v1/admin/reprime` | POST | **None** | **Add later.** Niche operator workflow; document in operator_commands.md only. |
| `/v1/pulse/stream` | GET (SSE) | None (consumed internally by some views) | **Leave.** Internal use; document in debug_recipes.md. |
| `/v1/signals/*` | GET | Signals tab | OK |
| `/v1/admin/signals/generate` (NEW in 15b) | POST | Signals tab "Generate signals" button (15c) | OK after 15c |
| `/v1/admin/signals/generated` (NEW in 15b) | GET | Signals tab "last generation" panel (15c) | OK after 15c |

### 8.2. Files NEW in 15d

Based on the "Add now" entries above:

| Path | Purpose |
|---|---|
| `trip_ema/gui/views/widgets/backfill_dialog.py` | Modal dialog: pick start date + optional tier; posts to backfill endpoint |
| `tests/gui/test_backfill_dialog.py` | Dialog tests |
| `docs/gui_endpoint_audit.md` | The full audit table, in the repo as living documentation |

### 8.3. Files MODIFIED in 15d

| Path | Change |
|---|---|
| `trip_ema/gui/views/etl_status.py` | Add "Backfill…" button that opens BackfillDialog |
| `trip_ema/gui/views/system_health.py` (or wherever orchestrator status renders) | Verify orchestrator + circuit breakers cards exist; add if missing |
| `scripts/operator_commands.md` | Cross-reference the new GUI surface |

### 8.4. Definition of done for 15d

- "Add now" items shipped with tests
- The audit table is in the repo as `docs/gui_endpoint_audit.md`
- For each "Add later" item, a TODO comment in the relevant view explaining the gap and the reason for deferring
- For each "Leave" item, a similar comment if the gap is non-obvious

---

## 9. Test plan rollup

| Phase | New tests | Existing-suite impact |
|---|---|---|
| 15a | ~50 (repo + replay + merge + config + CLI) | None (no changes to existing test paths) |
| 15b | ~12 (pure HTTP route tests) | None |
| 15c | ~25 (picker widget + panel + integration + http_client) | EtlStatusView refactor must not regress 14.6c picker tests |
| 15d | ~8-12 (BackfillDialog + audit doc + other additions) | None |

**Cumulative expected test count after Phase 15:** roughly 1138 (current) + ~100 = 1238 passing, plus the same 16 deselects (test_signals flake) and 2 skips (Postgres-only).

---

## 10. Deferred / future phases

These are surfaced here so the next operator workflow conversation doesn't ask "why didn't Phase 15 do X?"

- **Phase 16 — Intraday bar storage and ingestion.** Required before adding 15m/30m/45m/1h/2h/4h timeframes to backtest generation. Scope: ETL changes to fetch and reconcile intraday bars from yfinance, new partitioning strategy for `ohlcv_bars`, vendor-data-quality safeguards. After this, the timeframe list in `POST /v1/admin/signals/generate` can be extended; the JSON schema is already forward-compatible.

- **Phase 17 — Per-exchange parameters for live engine.** Today the live engine uses the global `indicators` block. Phase 15 introduces `exchange_overrides` but only for the backtest path. Phase 17 migrates the live engine to use the same resolution, ensuring backtest results match live behavior.

- **Phase 18 — Cancellation of in-flight backtest runs.** A POST to `/v1/admin/etl/cancel/{run_id}` that signals the worker to stop cleanly. Requires thread-safe cancellation flags in the runner; modest complexity.

- **Phase 19 — Multi-exchange aggregation.** If the backtester wants a single file covering NSE+BSE+NYSE, design what that file looks like and how it gets generated.

---

## 11. Pre-implementation reading list

Before writing code, the implementing conversation should read:

1. `trip_ema/engine/ticker_engine.py` — understand `TickerEngine.prime`, `.ingest`, what a `BarState` looks like, what `change_signal` is
2. `trip_ema/engine/change_classifier.py` — what the four ChangeSignal values actually mean in terms of regime transitions
3. `trip_ema/indicators/aggregation.py::daily_to_weekly` — the function used to synthesize weekly bars from daily
4. `trip_ema/domain/state.py` — `IndicatorParams`, `Timeframe`, `ChangeSignal`, `SignalEvent`, `BarState`
5. `trip_ema/config/schema.py` — existing config structure, especially `IndicatorsConfig.to_indicator_params()`
6. `trip_ema/etl/runner.py::run_eod` — the established pattern for an async ETL operation with etl_runs row
7. `trip_ema/transports/http/routes/admin_etl.py::run_eod` — the HTTP endpoint pattern for async work
8. `trip_ema/persistence/exchange_state_repo.py` — pattern for a per-exchange/per-key repo
9. `alembic_pkg/versions/0004_exchange_state.py` — pattern for a new table migration (with grants block)
10. `migrations/legacy/011_exchange_state.sql` — pattern for the legacy parity SQL file
11. `tests/gui/conftest.py` — known pytest-qt teardown flake; understand `isHidden()` vs `isVisible()`; understand worker draining
12. The Phase 14.6c bundle and the test-hotfix iterations to understand the GUI testing patterns that will apply to 15c

---

## 12. Glossary

- **Bar** — one OHLCV row for one (ticker, timeframe, timestamp)
- **Signal event** — a `(ticker, timeframe, bar_timestamp, signal_type, price_at_signal, computed_at)` row representing a state transition
- **ChangeSignal** — the enum: BUY, SELL, EXIT_FROM_BUY, EXIT_FROM_SELL, NO_CHANGE
- **Resume table** — `signal_backtest_runs`; tracks where each (ticker, timeframe) has been computed through
- **Engine replay** — running `TickerEngine.ingest()` over a historical bar sequence to recover the state-transition sequence
- **Force full** — operator-requested clean recompute; wipes resume rows for the exchange
- **Merge** — the in-memory dict update protocol that handles insert/update/delete of signals across runs while preserving signals outside the recomputed window
- **Atomic write** — write to `{file}.tmp`, fsync, `os.rename({file}.tmp, {file})`; POSIX guarantees atomicity of the rename
- **etl_executor** — the existing thread pool that runs async ETL operations; Phase 15 reuses it
- **etl_runs** — the table that tracks async ETL operations; Phase 15 adds rows with `kind='signal_backtest'`

---

## 13. Open questions

None. All design decisions captured in Section 2 (D1-D13). If the implementing conversation discovers an ambiguity, it should push back BEFORE writing code rather than make a silent choice.

---

## END OF DESIGN DOCUMENT
