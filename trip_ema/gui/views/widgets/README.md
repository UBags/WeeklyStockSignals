# `trip_ema/gui/views/widgets/` — Reusable widgets

Widgets that are shared by more than one tab. The pattern: when a
widget's logic shows up in two views, extract it here. Phase 15c
established the pattern by pulling `ExchangePicker` out of the
ETL Status tab so the Signals tab's `SignalGenerationPanel` could
reuse it.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker + re-exports |
| `exchange_picker.py` | `ExchangePicker` — `QComboBox` + status hint + loading sentinel + persistence via `QSettings` + `selection_changed` signal. Used by ETL Status and Signals (Phase 15c) |
| `backfill_dialog.py` | `BackfillDialog` — modal triggered by the ETL Status "Backfill…" button. `QDateEdit` (default = today − 30d, min 1990-01-01, max today) + optional "Restrict to one tier" checkbox + `QSpinBox(1..3)`. OK button always enabled (Phase 15d) |

## When to extract a widget here

Rule of thumb: **two consumers and growing**. A widget used by one
view stays inline in that view's file. A widget used by two views
moves here, and both views import it.

The extraction usually starts as a refactor: the second consumer is
the trigger. Phase 15c did exactly this with the picker — see the
test file `tests/gui/test_etl_status_view_exchange_picker.py` (18
tests), which exists specifically to guard against regressions when
the original inline picker in ETL Status was swapped for the shared
widget.

## Conventions

- One QWidget subclass per file
- Public API is a constructor + a small set of named signals (e.g.
  `selection_changed(str)`)
- Persistence via `QSettings` lives inside the widget — consumers
  don't manage storage
- Loading states (especially for HTTP-driven content) handled
  internally — consumers don't need to do "if not loaded yet" checks

## Tests

- `tests/gui/test_exchange_picker_widget.py` — 10 tests for ExchangePicker
- `tests/gui/test_backfill_dialog.py` — 14 tests for BackfillDialog
- `tests/gui/test_etl_status_view_exchange_picker.py` — 18 tests verifying
  ETL Status consumes the shared widget correctly (refactor guard)
