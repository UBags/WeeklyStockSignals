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
