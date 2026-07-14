# `trip_ema/gui/views/` — GUI tab implementations

One file per tab in the operator console. Each is a `QWidget` slotted
into `MainWindow`'s `QStackedWidget` via the sidebar.

## Tabs

| File | Tab | Purpose |
|---|---|---|
| `signals.py` | Signals | Chart (pyqtgraph) — candles + EMAs + ATR bands + regime fill + BUY/SELL markers + volume sub-panel. Crosshair OHLC readout. Time-range buttons (1Y/3Y/5Y/10Y/All), layer toggles. **Embeds Phase 15c `SignalGenerationPanel`** at the bottom |
| `subscribers.py` | Subscribers | Full CRUD over the `subscribers` table — transport (email/webhook/slack), address, cadence, filter editor (per-ticker / per-signal). Test-notification button. CSV export |
| `etl_status.py` | ETL Status | Exchange picker + 7 action buttons (Run EoD, Run Intraday, **Backfill…**, Refresh Listings, Recompute Tiers, Unpark All, Reprime). `etl_runs` table, exchange-state panel, parked-ticker list |
| `exchange_state_panel.py` | (helper) | Per-exchange ETL state widget — embedded inside `etl_status.py` |
| `notifications.py` | Notifications | Recent `notification_log` entries with signal color-coding, subscriber + hours filters. DLQ panel. CSV export |
| `system_health.py` | System Health | 8 status cards polled every 10s — ticker universe, ETL freshness, compute scheduler, signal cache, circuit breakers, notification queue, subscribers, recent events. Color-coded by status |
| `tickers.py` | Tickers | Searchable table of all tickers; per-row signal history; CSV export. Cross-tab nav: "View chart" opens Signals pre-loaded with the ticker |
| `settings.py` | Settings | GUI preferences (refresh cadences, default chart range, default chart range, exchange persistence) → `QSettings`. Read-only viewer for loaded `config.yaml`. "Backup config" action |
| `signal_generation_panel.py` | (embedded in Signals tab) | **Phase 15c.** ExchangePicker + timeframe checkboxes + incremental/force-full radio + Generate button + per-exchange "last generated file" metadata grid (polls `/v1/admin/signals/generated` every 30s) |

## Subdirectory

| Path | Purpose |
|---|---|
| `widgets/` | Reusable widgets shared across tabs — `ExchangePicker`, `BackfillDialog`. Extracted in Phase 15c |

## Conventions

- One QWidget subclass per file
- Every HTTP call goes through `workers.submit(...)` to stay off the
  Qt main thread
- Direct DB reads go through `gui/db.py` + `gui/queries.py`
- All write buttons disabled when `TRIP_EMA_ADMIN_TOKEN` is not set
  (read-only mode)
- Cross-tab nav happens via `MainWindow` signals — a view emits a
  signal; `MainWindow` translates to "switch to tab X with state Y"

## Tickers tab — row coloring and signal-grouped sort

The Tickers tab uses two GUI-side mechanisms that affect what you see at the
top of the list: row background coloring (Phase 16d) and a signal-grouped
sort proxy (Phase 16e). Both feed off the same data — the
`last_run_signals(conn, exchange)` query in `gui/queries.py`, which returns
`{ticker_id: signal_type}` for events whose `computed_at` falls within
60 seconds of the global `MAX(computed_at)` (i.e. the most recent
compute tick's emissions).

**Row coloring (Phase 16d)** — `TickerTableModel.data(..., Qt.BackgroundRole)`
returns a light QBrush per signal type:

| `signal_type` | Background |
|---|---|
| `BUY` | 🟢 light green (`#c8e6c9`) |
| `SELL` | 🔴 light red (`#ffcdd2`) |
| `EXIT_FROM_BUY` | 🔵 light blue (`#bbdefb`) |
| `EXIT_FROM_SELL` | 🔵 light blue (`#bbdefb`) |

The existing per-row ForegroundRole (red text for parked, gray for
delisted) still composes on top — a parked ticker with a fresh BUY
shows red text on a light-green background.

**Sort proxy (Phase 16e)** — `TickerSortFilterProxy` overrides `lessThan`
to push signalled rows to the top, grouped by color:

  1. 🟢 BUYs first, alphabetical by symbol
  2. 🔴 SELLs next, alphabetical
  3. 🔵 EXIT_FROM_BUY and EXIT_FROM_SELL last (they share a rank),
     alphabetical
  4. Non-signalled rows below — delegate to the parent's `lessThan`,
     so whatever column the user has clicked drives the within-bucket
     order

Click a column header to change the within-bucket order; the
signalled-first grouping stays intact regardless.

## Tickers tab — default sort and tier mechanism

When you open the Tickers tab with no fresh signals, the first 200-ish
rows look like NIFTY 100/200 names. That's not a hardcoded list — it's
the **tier** mechanism, computed weekly from average daily turnover.

How tiers are assigned (`trip_ema/etl/tiering.py`):

  - **Tier 1**: top **200** tickers ranked by 30-trading-day **Average
    Daily Value** (close × volume), descending. ADV correlates strongly
    with index inclusion at NSE, so the result reads as "NIFTY-200-ish".
  - **Tier 2**: next **500** by ADV.
  - **Tier 3**: everything else, including any ticker with `avg_daily_value=0`
    (new listings with no history can't jump to Tier 1 even if there
    were < 200 names overall).
  - Ties at tier boundaries break by `ticker_id ASC` for determinism.

The two size knobs are configurable in `config.yaml` under the `etl:`
block as `tier_1_size` and `tier_2_size`. Defaults match the README.

When the assignment is recomputed:

  - On demand: `trip-ema-etl recompute-tiers --exchange NSE`
  - The "Recompute Tiers" button on the ETL Status tab
  - Weekly cadence is the operational expectation; no auto-trigger is
    wired into the orchestrator today

How the GUI loads it:

  - `list_tickers` in `gui/queries.py` does `ORDER BY t.tier ASC, t.symbol ASC LIMIT 5000`
  - So you get all Tier 1 names (alphabetical) → all Tier 2 (alphabetical)
    → all Tier 3 (alphabetical), up to the 5000-row cap
  - The Phase 16e proxy then layers signal-grouped sorting on top —
    signalled rows float above the Tier 1 head; the rest stays in
    Tier-ASC order

The stability point (and why we don't add hysteresis at the boundary):
liquidity is volatile enough that a name on the border can legitimately
flip between tier 1 and tier 2 week-to-week, and the operational cost
of being in tier 2 is small enough not to warrant the complication. See
the module docstring of `etl/tiering.py` for the rationale.

## Async-action UI pattern

For long-running admin actions (Run EoD, Backfill, Generate signals, …):

1. Click button → confirm modal (especially for force-full / destructive)
2. POST `/v1/admin/.../action?exchange=...` (returns 202 with `run_id`)
3. Toast: "Action started for {EX} (run_id={N})"
4. Schedule a metadata refresh in 5s — the runs table or status panel
   picks up the in-flight row

## Tests

Each view file has a matching test in `tests/gui/test_<name>.py`:
- `test_signals.py`, `test_signal_generation_panel.py`, `test_etl_status_view_exchange_picker.py`, `test_backfill_dialog.py`, ...

Plus broader integration cases (`test_gui.py`, `test_slice4_*`,
`test_workers_lifetime.py`).