# `trip_ema/gui/` — PySide6 desktop operator console

The `trip-ema-console` application. A desktop GUI that talks to the
`trip-ema` service over HTTP (and to Postgres directly read-only for
table views). Invariant 3.14: the GUI is a **separate process** from
the service.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `console.py` | Entry point — `QApplication` bootstrap, splash, main-window construction. Exposes `main()` |
| `main_window.py` | `QMainWindow` — sidebar (left) + `QStackedWidget` (right). Slice 4 final layout, 7 working views. Help → About menu |
| `http_client.py` | `TripEmaHttpClient` — thin `requests` wrapper. Injects bearer token, raises typed errors |
| `db.py` | `GuiDbSession` — direct read-only Postgres engine for table views (subscribers, signal history, …) |
| `queries.py` | Pure SQL functions over a `Connection`, returning typed records. No Qt |
| `workers.py` | `submit(callable) -> Worker` — runs the callable on `QThreadPool`, emits `finished` / `failed` signals on the Qt main thread. Plus the deferred-deletion primitives |
| `csv_export.py` | `export_rows_to_csv` + `timestamped_filename`. Shared across tabs |
| `about_dialog.py` | Help → About — version, runtime info |
| `signals_helpers.py` | Pure helpers for the Signals tab — Qt-free formatting, partitioning, and rendering logic so it's unit-testable |
| `benchmark_rerank_client.py` | GUI-side passthrough for `fetch_rank_scores`. The actual fetch lives in `notifications/benchmark_rerank.py`; this shim keeps the import direction one-way |

## Views (the 7 tabs)

| View | Purpose |
|---|---|
| `signals.py` | Signal viewer (Phase C layout). Top bar + horizontal splitter: 55 % `IndustryPanel` / 45 % main chart. Embeds the Phase 15c generation panel below |
| `subscribers.py` | CRUD over `subscribers` + child `subscriptions` rows. Send-test-email button |
| `etl_status.py` | Tab A. Action bar + `ExchangeStatePanel` + recent-runs table + parked list. `Backfill…` and `Backfill historical shares…` dialogs |
| `notifications.py` | Recent notification log + DLQ. `Last [N] hours`, subscriber filter, export CSV |
| `system_health.py` | 8-card status dashboard, polled |
| `tickers.py` | Browsable ticker table. Row backgrounds: BUY → light green, SELL → light red, EXIT_* → light blue (`Qt.BackgroundRole`). `TickerSortFilterProxy` floats signalled rows to top. Recent signal events panel shows `computed_at` (IST) — not `bar_timestamp` |
| `settings.py` | Token, base URL, exchange persistence |

## Widgets

Reusable widgets — extract when needed in more than one view.

| Widget | Purpose |
|---|---|
| `chart_widget.py` | `ChartWidget` with `MAIN` / `INDUSTRY` / `MINI` modes. Same rendering path used by SignalsView's main chart and the 4×3 mini grid |
| `industry_panel.py` | Left panel for Signals tab. Industry list, 4×3 mini grid (top-12 by mcap in industry), last-5-signals panel. NIFTY 50 fallback constant `_NIFTY50_FALLBACK = "NIFTYBEES"` (NSE ETF, 4300 bars from 2009) |
| `signal_generation_panel.py` | Phase 15c. `QGroupBox` below the chart for in-GUI signal generation + backtest |
| `exchange_state_panel.py` | Phase 14.6c. Sits between the action bar and the recent-runs table in ETL Status |
| `exchange_picker.py` | `QComboBox` + status hint. Today: ETL Status + Signals + Backfill dialog |
| `backfill_dialog.py` | Modal — driven from ETL Status's `Backfill…` button |
| `backfill_shares_dialog.py` | Modal — collects `start_date` (default 10y back) for the historical-shares backfill button |
| `backtest_dialog.py` | Non-modal — pops up from SignalsView's `Run Backtest`. Stays open so the user can compare runs |

## Architecture

```
MainWindow (QMainWindow)
   sidebar + QStackedWidget
        each "view" is a QWidget in the stack:
            SignalsView         (IndustryPanel + chart + generation panel)
            SubscribersView     (CRUD over subscribers)
            EtlStatusView       (action bar + state panel + runs + parked)
            NotificationsView   (recent log + DLQ)
            SystemHealthView    (8-card status dashboard, polled)
            TickersView         (browsable ticker table)
            SettingsView        (token, base URL, exchange persistence)
```

Each view:
- Reads `TripEmaHttpClient` for HTTP (passed through `workers.submit`
  to stay off the Qt main thread)
- Reads `db.py` for direct read-only DB queries
- Persists user preferences via `QSettings` (including splitter sizes)

## Read-only mode

When `TRIP_EMA_ADMIN_TOKEN` is unset, all write buttons are disabled
with explanatory tooltips. All read panels work. Recommended posture
for analyst seats — ops/admin seats set the token.

## Tests

Two trees:
- `tests/gui/` — pytest-qt; needs `QT_QPA_PLATFORM=offscreen` for
  headless. Covers view construction, widget behaviour, signal/slot
  wiring
- `tests/gui_pure/` — pure Python, no `QApplication`. Covers HTTP
  client, `signals_helpers`, and other Qt-free code

`tests/gui/conftest.py` implements the deferred-deletion pattern: a
`managed_widget` factory + autouse `_drain_after_every_gui_test`
fixture. Standalone clean-exit went from ~3/5 to ~4–5/5. The CI
workflow's two-invocation split + output-parsing wrapper are
belt-and-braces.

Known non-regressions: intermittent GUI teardown segfault on
`test_run_eod_targets_picker_selection`. Tactical workaround:
`--deselect "tests/gui/test_signals.py::TestRangeButton::test_range_button_click_updates_years"`.

## Installer

`gui_installer.spec` at the repo root drives a PyInstaller build of a
standalone Linux x86_64 binary. Output:
`trip-ema-console-vX.Y.Z-linux-x86_64.tar.gz`. `release.yml` runs it
on tag push. Windows + macOS deferred.
