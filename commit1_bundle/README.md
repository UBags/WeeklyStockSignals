# Commit 1 — A1: Remove industry signal-event writers

**Purpose.** Code-only deletion of the industry `signal_events` writer
path. No DB schema change. No runner-logic change (the indicator-state
runner is the subject of Commit 2). Industry indicator-state continues
to be computed and stored; only the per-industry *signal events*
(BUY/SELL/EXIT_*) compute path is removed.

Baseline before applying: `pre-industry-unification-baseline` tag
(`dc24bdb`), 1,994 tests passing, 58 skipped.

## What's in the bundle

```
commit1_bundle/
├── README.md                       (this file)
├── patches/                        (idempotent, assert-guarded patchers)
│   ├── _patch_lib.py
│   ├── 01_runners_delete_industry_signal_functions.py
│   ├── 02_eod_pipeline_remove_industry_signals_step.py
│   ├── 03_cli_remove_industry_signals_modes.py
│   ├── 04_http_routes_remove_industry_signal_endpoints.py
│   └── 05_tests_remove_industry_signal_event_tests.py
└── diffs/                          (read-only, for review before apply)
    ├── trip_ema_fundamentals_runners.diff
    ├── trip_ema_fundamentals_eod_pipeline.diff
    ├── trip_ema_fundamentals_cli.diff
    ├── trip_ema_transports_http_routes_fundamentals.diff
    ├── tests_fundamentals_test_cli.diff
    ├── tests_transports_http_test_fundamentals_routes.diff
    └── tests_fundamentals_test_signal_event_runners.diff   (whole-file delete)
```

Each patcher uses **assert-guarded block replacement**: it locates an
exact source block, refuses to run if the block isn't present verbatim,
and writes atomically only after every requested edit has matched. If
your working tree has drifted from the baseline (`dc24bdb`), the
patcher will fail loudly with a `PatchError` *before* writing.

## What changes (file-by-file summary)

| File | Change | Line delta |
|------|--------|------------|
| `trip_ema/fundamentals/runners.py` | Delete `run_signal_events_for_industry` and `run_signal_events_for_all_industries`. Ticker variants, indicator-state-for-industry, and `_fetch_bars_at_timeframe` all stay. | -191 |
| `trip_ema/fundamentals/eod_pipeline.py` | Drop import of `run_signal_events_for_all_industries`. Delete `_step_signals_industries` nested function. Remove `("compute-signals (industries)", ...)` tuple from steps list. Industry indicator-state step stays. | -14 |
| `trip_ema/fundamentals/cli.py` | Replace `cmd_compute_signals` preamble + delete industry branches. Drop `--industry` and `--all-industries` argparse args from the `compute-signals` subparser. Update `cmd_run_eod_pipeline` docstring step numbering. `cmd_compute_indicator_state` (industry modes) unchanged. | -65 |
| `trip_ema/transports/http/routes/fundamentals.py` | Delete `GET /signal-events/industries/{industry_name:path}` handler. Strip industry params/branches from `_run_signals_safe`. Strip industry query params from `POST /compute-signals`. `GET /signal-events/recent` stays. `POST /compute-indicator-state` (industry modes) stays. | -53 |
| `tests/fundamentals/test_cli.py` | Delete `TestCmdComputeSignals` class (7 industry-mode tests). `TestCmdComputeSignalsTickerModes` stays. `TestCmdComputeIndicatorState` (industry indicator tests) stays. | -147 |
| `tests/transports/http/test_fundamentals_routes.py` | Delete `TestGetIndustrySignalEvents` (6 tests) and `TestComputeSignalsEndpoint` (5 industry tests). `TestGetRecentSignalEvents`, `TestComputeSignalsTickerMode`, `TestComputeIndicatorStateEndpoint` all stay. | -149 |
| `tests/fundamentals/test_signal_event_runners.py` | Whole-file delete (`git rm`) — file is exclusively industry tests. Ticker signal-event tests are in `tests/fundamentals/test_per_ticker_runners.py`. | -357 |

Net deletion: ~976 lines of code, no additions.

## What is deliberately KEPT

These are the things last session's bundle wrongly touched and that
made the cascade unrecoverable. They MUST stay for Commit 1 to be
green standalone:

- `signal_events.industry_name` column (DB schema unchanged)
- `SignalEvent.industry_name` dataclass field
- The XOR check constraint on `signal_events`
- The partial unique index on `signal_events (industry_name, ...)`
- `SignalEventRepository.delete_for_industry` and `get_for_industry`
  (called by nothing after this commit, but harmless and tested)
- All `SignalEventRepository` industry-handling logic
- `trip_ema/fundamentals/signal_compute.py` (still imported by
  `run_signal_events_for_ticker`)
- `compute_indicator_series(..., synthetic_atr_for_index=True)` parameter
  (still passed by the industry indicator runner — that's Commit 2's job
  to flip)
- The 6 skipped `TestOrchestration` tests in `test_eod_pipeline.py`
  (re-baselining the expected step list is a separate cleanup)

## Apply

```bash
cd ~/PycharmProjects/WeeklyStockSignals

# 0. Confirm we're at the baseline tag
git rev-parse HEAD
# expect: dc24bdb...
git describe --tags --exact-match
# expect: pre-industry-unification-baseline (or 'fatal: no tag exactly matches' if you've moved)

# 1. Unzip the bundle somewhere outside the repo
#    (assume you already have ./commit1_bundle/ ready)

# 2. Review the diffs visually — every change is in plain text
less commit1_bundle/diffs/trip_ema_fundamentals_runners.diff
less commit1_bundle/diffs/trip_ema_fundamentals_eod_pipeline.diff
less commit1_bundle/diffs/trip_ema_fundamentals_cli.diff
less commit1_bundle/diffs/trip_ema_transports_http_routes_fundamentals.diff
less commit1_bundle/diffs/tests_fundamentals_test_cli.diff
less commit1_bundle/diffs/tests_transports_http_test_fundamentals_routes.diff

# 3. Apply patchers in order. Each prints OK / NOOP / FAILED.
#    If any FAIL, STOP — do not run the rest. Send me the error.
python commit1_bundle/patches/01_runners_delete_industry_signal_functions.py
python commit1_bundle/patches/02_eod_pipeline_remove_industry_signals_step.py
python commit1_bundle/patches/03_cli_remove_industry_signals_modes.py
python commit1_bundle/patches/04_http_routes_remove_industry_signal_endpoints.py
python commit1_bundle/patches/05_tests_remove_industry_signal_event_tests.py

# 4. Delete the whole-file test (patcher 05 verifies before
#    permitting this; if patcher 05 succeeded, this is safe).
git rm tests/fundamentals/test_signal_event_runners.py

# 5. Spot-check what actually changed
git status
git diff --stat
# expect: 7 files changed, ~0 insertions(+), ~976 deletions(-)
# plus the deleted test file showing in `git status`

# 6. Pre-commit spot check (you mentioned skipping this caused real
#    bugs to slip — don't skip it)
pre-commit run --files \
    trip_ema/fundamentals/runners.py \
    trip_ema/fundamentals/eod_pipeline.py \
    trip_ema/fundamentals/cli.py \
    trip_ema/transports/http/routes/fundamentals.py \
    tests/fundamentals/test_cli.py \
    tests/transports/http/test_fundamentals_routes.py

# 7. Run the full test suite
pytest -rsx --tb=short -q 2>&1 | tee /tmp/commit1_test_run.log
tail -40 /tmp/commit1_test_run.log

# Expected: ~1,994 - (7 industry CLI tests + 11 industry route tests + 9
#           industry runner tests = 27 deleted tests) = ~1,967 passing,
#           58 skipped, 0 failed.
#           Skip categories unchanged from baseline. No new warnings.
```

## Commit + push

```bash
git add -A
git status   # one more look

git commit -m "remove industry signal-event writers (A1 demolition)

Industries are non-tradeable regime indicators; their signal_events
table rows were never consumed downstream and the writer path was
the root of last session's cascading-test-failures attempt.

Removed:
  - run_signal_events_for_industry, run_signal_events_for_all_industries
    from trip_ema/fundamentals/runners.py (per-function delete; ticker
    variants and indicator-state runner untouched)
  - compute-signals industry step from EoD pipeline (industry
    indicator-state step retained)
  - --industry and --all-industries modes from compute-signals CLI
  - GET /v1/fundamentals/signal-events/industries/{name} endpoint
  - industry params/branches from POST /v1/fundamentals/compute-signals
  - the corresponding test classes:
      TestCmdComputeSignals (test_cli.py)
      TestGetIndustrySignalEvents (test_fundamentals_routes.py)
      TestComputeSignalsEndpoint (test_fundamentals_routes.py)
      tests/fundamentals/test_signal_event_runners.py (whole file)

Retained (kept transitional for Commit 2 + Phase 6 follow-ups):
  - signal_events.industry_name column and XOR constraint
  - SignalEvent.industry_name dataclass field + validation
  - SignalEventRepository.delete_for_industry / get_for_industry
  - signal_compute.py (used by run_signal_events_for_ticker)
  - synthetic_atr_for_index parameter (flipped in Commit 2)

Net: -976 lines; 0 schema changes; tests green standalone.
Baseline: pre-industry-unification-baseline (dc24bdb).
"

git push origin master
```

## Rollback

```bash
# If anything looks off:
git reset --hard pre-industry-unification-baseline
# Or to keep the bundle for a retry:
git reset --soft HEAD~1
```

## After this commit lands green

- Mention in your reply: pass count, any unexpected skips, any
  warnings other than the known `HTTP_422_UNPROCESSABLE_ENTITY`
  deprecation.
- That's the green gate. Commit 2 (Phase 1B — weekly aggregation of
  industry indicator compute) is the next bundle. Don't start the
  runtime regen sequence (Phase 3-5) until both commits are landed
  and green.
