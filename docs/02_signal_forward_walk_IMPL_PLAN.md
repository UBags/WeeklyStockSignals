# Signal Forward-Walk — Implementation Plan

Companion to `02_signal_forward_walk_labels_spec.md`. Six green commits, one
logical change each. Compile → import-test → scoped suite before every commit.

**Branch.** The walk needs corrected/self-adjusted bars, so branch off the
corrector line:

```bash
cd ~/PycharmProjects/WeeklyStockSignals
git checkout feature/ohlcv-seam-corrector
git pull
git checkout -b feature/signal-forward-walk
```

**Patcher discipline.** Every patcher that edits an existing file is a one-shot
idempotent anchor-guarded script staged to `~/Downloads/`, run, verified,
deleted (never committed). `count==1` abort-on-drift, validate-ALL-before-write,
compile + import-test, full revert on any post-write failure, idempotency
sentinel. Anchors are cut from **live file reads on this branch**, not from the
bundle snapshot — so each patcher is generated at the start of its phase, after
a fresh read, to avoid the source-bundle drift hazard.

---

## Phase 0 — stop writing `quality_class` (prerequisite for the column drop)

Ordering gate: the labeler INSERT must stop naming `quality_class` before
migration 0055 drops it.

Files & transforms:
- `trip_ema/domain/signal_label.py` — drop the `quality_class: str | None` field.
- `trip_ema/fundamentals/signal_labels.py` — `compute_label` returns the 5-tuple
  `(r4, r8, r13, fwd_max_dd, rr)` (drop `qc`); delete `CLASS_*`, `RET_VERYGOOD`,
  `DD_VERYGOOD`, `RR_VERYGOOD`, `DD_BAD`, and the class-judgement block. `fwd_rr`
  is retained.
- `trip_ema/fundamentals/signal_labels_runner.py` — `build_ticker_labels`
  unpacks 5, drops `quality_class=qc` from the `SignalLabel(...)` call.
- `trip_ema/persistence/signal_labels_repo.py` — drop `quality_class` from the
  `_INSERT` column list, `:qc` value, the `ON CONFLICT … quality_class=EXCLUDED`
  line, and `"qc"` from `_params`.
- `tests/fundamentals/test_signal_labels.py` (9 sites) and
  `tests/fundamentals/test_signal_labels_runner.py` (2 sites) — drop
  `quality_class` assertions / kwargs; adjust the `compute_label` unpack.

Patcher (generated at phase start, after live read):

```bash
python ~/Downloads/p0_drop_quality_class.py
rm ~/Downloads/p0_drop_quality_class.py
```

Verify + scoped tests:

```bash
python -c "import trip_ema.fundamentals.signal_labels, trip_ema.fundamentals.signal_labels_runner, trip_ema.persistence.signal_labels_repo, trip_ema.domain.signal_label"
pytest tests/fundamentals/test_signal_labels.py tests/fundamentals/test_signal_labels_runner.py tests/fundamentals/test_per_signal_stages.py tests/fundamentals/test_signal_features.py -q -n 6
```

Commit:

```bash
git add trip_ema/domain/signal_label.py trip_ema/fundamentals/signal_labels.py \
        trip_ema/fundamentals/signal_labels_runner.py \
        trip_ema/persistence/signal_labels_repo.py \
        tests/fundamentals/test_signal_labels.py \
        tests/fundamentals/test_signal_labels_runner.py
git commit -m "signal_labels: drop quality_class write path (pre-0055)"
git push -u origin feature/signal-forward-walk
```

---

## Phase 1 — migration `0055_signal_label_forward_walk`

New file (already drafted): `alembic_pkg/versions/0055_signal_label_forward_walk.py`
— drops `quality_class` + `sl_quality_class_chk` + `ix_signal_labels_quality`,
adds the 79 walk columns (rung lists generated in the migration). Revises
`0054_nse_corporate_actions`.

Apply + round-trip check + regenerate DDL:

```bash
cp 0055_signal_label_forward_walk.py alembic_pkg/versions/
alembic upgrade head
alembic downgrade -1 && alembic upgrade head      # reversibility smoke
pg_dump -s -d trip_ema > DDL.txt                  # your standard schema dump
```

New test: `tests/architecture/test_migration_0055_columns.py` — asserts the 79
columns exist with the right nullability/defaults and that `quality_class` +
its CHECK/index are gone (mirror `test_migration_0052_columns.py`).

Scoped tests:

```bash
pytest tests/architecture tests/config/test_alembic_bridge_no_db.py -q -n 6
```

Commit:

```bash
git add alembic_pkg/versions/0055_signal_label_forward_walk.py DDL.txt \
        tests/architecture/test_migration_0055_columns.py
git commit -m "migration 0055: drop quality_class, add forward-walk label columns"
git push
```

---

## Phase 2 — pure `walk_signal` + exhaustive unit tests

New file: `trip_ema/domain/forward_walk.py` — `ForwardWalkResult` dataclass +
`walk_signal(entry_open, bars, side, atr20, industry_ret, market_ret) ->
ForwardWalkResult`. Pure, no I/O. All threshold/MFE/MAE/slope/vol/Sharpe/
capture/ATR-normalisation logic. Bar 0 = signal day, bar 1 = entry day,
`bars_to_* = hit_bar_index − 1`, `-1` = miss; intrabar direction-aware touch
(long: gains on high / losses on low; short mirrored). Excess returns are
passed in pre-computed (the runner owns the DB reads).

New tests: `tests/domain/test_forward_walk.py` — long & short; "3/5/7/9 all hit
on bar 2 ⇒ distance 1"; `-1` defaults; censored window; MAE/MFE bars;
`mae_before_mfe`; capture ratio (incl. NULL when `mfe=0`); ATR-normalised
MFE/MAE; slope/vol/Sharpe; `ever_in_profit`; a window abutting a censored region.

```bash
pytest tests/domain/test_forward_walk.py -q -n 6
```

Commit:

```bash
git add trip_ema/domain/forward_walk.py tests/domain/test_forward_walk.py
git commit -m "forward_walk: pure walk_signal + unit tests"
git push
```

---

## Phase 3 — per-ticker runner (window pairing + context returns)

New file: `trip_ema/fundamentals/forward_walk_runner.py`.
- Resolve the NIFTYBEES market ticker once (`exchange, symbol='NIFTYBEES'`);
  load its 1D closes.
- Per ticker (reads via `trip_gui_user`, **no writes**): load `signal_enrichment`
  rows `ORDER BY signal_date`; load adjusted 1D `ohlcv_bars`
  (`AT TIME ZONE 'Asia/Kolkata'`, keyed on `signal_date`); resolve industry via
  `instrument_industry.industry` → `industry_index_value` (fallback to NIFTYBEES
  per §3a); pair consecutive signals (last ⇒ censored to data end); slice each
  window in memory; compute `industry_ret` / `market_ret` (asof-backward,
  `close_last/close_first − 1`); call `walk_signal`; return rows.
- Data-quality gate: mark `window_censored` when the window overlaps an OHLCV
  corrector `step_suspect`/demerger region. **Dependency:** wire to the
  corrector's flag surface once that branch lands it (bad-tick/phantom bars are
  already dropped upstream).

New tests: `tests/fundamentals/test_forward_walk_runner.py` — window pairing;
censored last signal; industry→market fallback; nearest-prior alignment; only
BUY/SELL get rows; `EXIT_FROM_*` acts only as terminator.

```bash
pytest tests/fundamentals/test_forward_walk_runner.py -q -n 6
```

Commit:

```bash
git add trip_ema/fundamentals/forward_walk_runner.py \
        tests/fundamentals/test_forward_walk_runner.py
git commit -m "forward_walk: per-ticker runner + industry/market excess returns"
git push
```

---

## Phase 4 — orchestration (ProcessPool, single writer) + repo upsert

- Extend `trip_ema/persistence/signal_labels_repo.py`: `upsert_forward_walk(rows)`
  writing `forward_walk_done=true` + all 79 columns `ON CONFLICT
  (signal_enrichment_id) DO UPDATE`, bumping `computed_at` (via `trip_etl_user`).
- New file: `trip_ema/orchestrator/forward_walk_orchestrator.py` —
  `ProcessPoolExecutor(max_workers=round(2/3 × physical_cores))` (= 4 on the NUC),
  `imap_unordered` over `sorted(ticker_ids)`; workers read + return rows, the
  main process is the sole writer. No cross-process session sharing; re-runnable.

New tests: `tests/orchestrator/test_forward_walk_orchestrator.py` — worker-read /
main-write split; idempotent re-run; `computed_at` bump; `forward_walk_done`
flips true.

```bash
pytest tests/orchestrator/test_forward_walk_orchestrator.py tests/fundamentals/test_forward_walk_runner.py -q -n 6
```

Commit:

```bash
git add trip_ema/orchestrator/forward_walk_orchestrator.py \
        trip_ema/persistence/signal_labels_repo.py \
        tests/orchestrator/test_forward_walk_orchestrator.py
git commit -m "forward_walk: process-pool orchestration + single-writer upsert"
git push
```

---

## Phase 5 — CLI wiring (+ optional admin route)

- `trip_ema/fundamentals/cli.py`: add a `forward-walk` subcommand (runs after
  signal generation and after the OHLCV correction pass). Optional:
  `POST /v1/admin/signals/forward-walk` (bearer-gated).

New tests: extend `tests/fundamentals/test_cli.py`.

```bash
pytest tests/fundamentals/test_cli.py -q -n 6
```

Commit:

```bash
git add trip_ema/fundamentals/cli.py tests/fundamentals/test_cli.py
git commit -m "forward_walk: CLI wiring"
git push
```

---

## Full suite (after Phase 5, before opening the PR)

```bash
pytest -q -n 8
```

Expect green across the board. Then open the PR from
`feature/signal-forward-walk` into the corrector line (or main, once the
corrector merges).

---

## Order of operations recap

0. drop quality_class writes → 1. migrate → 2. pure core → 3. runner →
4. orchestration → 5. CLI. Phases 2 and 3 can be developed in parallel but must
land in order (3 imports 2). Phase 1 must not be applied before Phase 0 commits.
