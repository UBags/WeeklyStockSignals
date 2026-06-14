# `trip_ema/gui/` — PySide6 desktop operator console

The `trip-ema-console` application. A desktop GUI that talks to the
`trip-ema` service over HTTP (and to Postgres directly read-only for
table views). Invariant 3.14: the GUI is a **separate process** from
the service.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `console.py` | Entry point — `QApplication` bootstrap, splash, main-window construction |
| `main_window.py` | Tab container + sidebar — wires the 7 tabs together |
| `http_client.py` | `TripEmaHttpClient` — thin wrapper over `requests`; injects bearer token; raises typed errors |
| `db.py` | Direct read-only Postgres engine for table views (subscribers, signal history, etc.) |
| `queries.py` | SQL strings the GUI runs directly against `db.py`'s engine |
| `workers.py` | `submit(callable) -> Worker` — runs the callable on `QThreadPool`, emits `finished` / `failed` signals on the Qt main thread |
| `csv_export.py` | "Export to CSV" support shared across tabs |
| `console.py` | Same as above; entry-point function `main()` |
| `about_dialog.py` | Help → About menu item (version, runtime info) |

## Subdirectories

| Path | Purpose |
|---|---|
| `views/` | The 7 tab implementations + nested widget directory |
| `views/widgets/` | Reusable widgets shared across tabs — extracted in Phase 15c |

## Architecture

```
MainWindow (QMainWindow)
   sidebar + QStackedWidget
        each "view" is a QWidget in the stack:
            SignalsView         (chart + Phase 15c generation panel)
            SubscribersView     (CRUD over subscribers)
            EtlStatusView       (action buttons + runs table + state panel + parked list)
            NotificationsView   (recent log + DLQ)
            SystemHealthView    (8-card status dashboard, polled)
            TickersView         (browsable ticker table)
            SettingsView        (token, base URL, exchange persistence)
```

Each view:
- Reads `TripEmaHttpClient` for HTTP calls (passes through `workers.submit`
  to stay off the Qt main thread)
- Reads `db.py` for direct read-only DB queries (table views, signal
  history, …)
- Persists user preferences via `QSettings`

## Read-only mode

When `TRIP_EMA_ADMIN_TOKEN` is not set in the environment, all write
buttons are disabled with explanatory tooltips. All read panels work.
This is the recommended posture for analyst seats — ops/admin seats
set the token.

## Reusable widgets (Phase 15c)

`views/widgets/exchange_picker.py` is the canonical pattern: extract a
widget when it's needed in more than one view. Today: `ExchangePicker`
in ETL Status and Signals tabs; `BackfillDialog` (modal) for the ETL
Status "Backfill…" button.

## Tests

Two test trees:
- `tests/gui/` — pytest-qt; needs `QT_QPA_PLATFORM=offscreen` to run
  headless. Covers view construction, widget behavior, signal/slot wiring
- `tests/gui_pure/` — pure-Python tests that don't need a `QApplication`.
  Covers HTTP client logic and other GUI-adjacent code

The Phase 12 `tests/gui/conftest.py` implements the deferred-deletion
pattern (Gemini's pytest-qt PDF) — a `managed_widget` factory and an
autouse `_drain_after_every_gui_test` fixture. Standalone clean-exit
rate went from ~3/5 to ~4-5/5 as a result. The CI workflow's
two-invocation split + output-parsing wrapper are belt-and-braces.

## Installer (Phase 12)

`gui_installer.spec` at the repo root drives a PyInstaller build of a
standalone Linux x86_64 binary. Output: `trip-ema-console-vX.Y.Z-linux-x86_64.tar.gz`.
The `release.yml` GitHub Actions workflow runs this on tag push.
Windows + macOS builds deferred.
