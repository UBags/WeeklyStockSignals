# Fundamentals, Industry Indices & Recommendation Layer

**Phases 1–8 design document. Phase 9 (per-stock corporate-filings panel) is deferred to its own design session.**

---

## 1. Purpose and scope

This document is the design contract for a new vertical slice of trip_ema that adds:

- Per-ticker **industry/sector classification** sourced from yfinance, with manual operator overrides.
- Per-ticker **shares outstanding** sourced from NSE's XBRL shareholding-pattern filings (for NSE) and yfinance (for everything else).
- Per-ticker **quarterly revenue and EBITDA** for the last 4 reported quarters from yfinance.
- **Daily market cap** per ticker, computed from close × shares-outstanding (with yfinance's historical market cap used as a backfill for dates older than the data we can compute ourselves).
- **Per-industry indices** of the top 20 stocks by market cap, full-market-cap-weighted, semi-annually rebalanced.
- **The trip_ema indicator stack** (3 EMAs, ATR, background regime) applied to the daily index series.
- **GUI display** of fundamentals and the industry index in existing views.
- **db_diagnostics.sh sorting** by market cap.

**Phase 7's recommendation logic is deferred** to its own discovery session once Phases 1–6 are live and the data quality is visible. Phase 9 is also deferred.

The document is written so that each phase is independently deliverable. A phase ships when:

1. Its DB tables and migration exist and are tested.
2. Its CLI command works against the real DB.
3. Its HTTP endpoint works against the real API.
4. Its scheduled-trigger hook (where applicable) is wired into the orchestrator.
5. Tests cover the new code paths.
6. A verification query in this document produces sensible numbers.

---

## 2. Non-goals and explicit out-of-scope items

The following are NOT being built and should not be added without an explicit decision:

- **Free-float-adjusted market cap.** We use *full* market cap. The schema has `free_float_factor REAL NULL` for forward compatibility, but no code path uses it. Documented deviation from NIFTY methodology.
- **Sub-day refresh.** All fundamentals refresh quarterly (at Q-end + 1 month). Market cap recomputes daily after EoD close. Index values recompute daily.
- **Backfilling shares-outstanding history.** Phase 2 stores *today's* shares outstanding. Historical market cap before Phase 2's start date comes from yfinance's historical market cap series (computed from yfinance's split-adjusted history). The assumption is documented: market cap for dates before Phase 2 went live uses approximated historical shares-outstanding.
- **GICS / NIC classification.** Yfinance's taxonomy is used as-is. The Groww.in fallback you mentioned is deferred to a Phase 1.5 enhancement; Phase 1 ships with yfinance-only classification, which is sufficient for ~70–85% of NSE+BSE coverage.
- **Corporate actions adjustment of historical market cap.** When yfinance's historical market cap is unavailable for a date, we approximate as `close × today's shares_outstanding`. This is wrong for any company that has done a split/bonus/buyback between then and now. Documented limitation.
- **Phase 7's recommendation logic.** Phase 7 ships with display only; the actual heuristic for combining "industry signal + industry momentum + industry fundamentals + stock signal + stock fundamentals" is a separate design conversation after data is visible.
- **Phase 9 (NSE corporate filings panel per stock).** Deferred entirely.

---

## 3. Database schema

One migration per phase. All migrations are additive (no destructive changes to existing tables). Naming follows the existing `000N_short_description.py` convention.

### 3.1. Migration `0006_instrument_industry.py` (Phase 1)

```sql
CREATE TABLE instrument_industry (
    ticker_id            INTEGER PRIMARY KEY
                                 REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    sector               VARCHAR(64),     -- yfinance "sector"  (broad bucket)
    industry             VARCHAR(128),    -- yfinance "industry" (fine bucket)
    classification_source VARCHAR(32) NOT NULL DEFAULT 'YFINANCE',
                                          -- 'YFINANCE' | 'GROWW' | 'MANUAL_OVERRIDE'
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_instrument_industry_industry ON instrument_industry (industry);
CREATE INDEX idx_instrument_industry_sector   ON instrument_industry (sector);

CREATE TABLE industry_override (
    ticker_id    INTEGER PRIMARY KEY
                         REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    sector       VARCHAR(64)  NOT NULL,
    industry     VARCHAR(128) NOT NULL,
    note         TEXT,
    set_by       VARCHAR(64),
    set_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE fundamentals_run (
    run_id        BIGSERIAL PRIMARY KEY,
    phase         VARCHAR(32) NOT NULL,    -- 'industry' | 'shares' | 'financials' | 'market_cap' | 'index'
    exchange      VARCHAR(16),             -- NULL for cross-exchange phases (e.g. index)
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    status        VARCHAR(16) NOT NULL DEFAULT 'RUNNING'
                              CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')),
    succeeded     INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    progress_json JSONB,                    -- resumability cursor
    notes         TEXT
);
CREATE INDEX idx_fundamentals_run_phase_ex
    ON fundamentals_run (phase, exchange, started_at DESC);
```

**Why a single `fundamentals_run` table for all phases.** Each phase is the same shape — a long-running fetch loop over tickers, exchange-scoped, with progress and outcome. One table keeps the operations dashboard simple. Discriminator is `phase`.

**Why no `last_industry_refresh_per_exchange` column.** That data is derivable as `MAX(completed_at) FROM fundamentals_run WHERE phase='industry' AND exchange=X AND status='SUCCESS'`. We don't denormalize it.

### 3.2. Migration `0007_instrument_shares.py` (Phase 2)

```sql
CREATE TABLE instrument_shares (
    ticker_id             INTEGER PRIMARY KEY
                                  REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    shares_outstanding    BIGINT,
    free_float_factor     REAL,            -- Phase 2: NULL; populated later
    shares_source         VARCHAR(16) NOT NULL,
                                          -- 'NSE_XBRL' | 'YFINANCE'
    as_of_date            DATE NOT NULL,   -- date the source itself reports
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    xbrl_url              TEXT             -- when shares_source='NSE_XBRL'
);
```

### 3.3. Migration `0008_quarterly_financials.py` (Phase 3)

```sql
CREATE TABLE quarterly_financials (
    ticker_id      INTEGER NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    fiscal_quarter DATE    NOT NULL,        -- quarter-end date (e.g. 2025-12-31)
    revenue        NUMERIC(18, 2),
    ebitda         NUMERIC(18, 2),
    currency       VARCHAR(8)  NOT NULL DEFAULT 'INR',
    source         VARCHAR(16) NOT NULL DEFAULT 'YFINANCE',
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker_id, fiscal_quarter)
);
CREATE INDEX idx_quarterly_financials_ticker ON quarterly_financials (ticker_id);
```

### 3.4. Migration `0009_daily_market_cap.py` (Phase 4 prep)

```sql
CREATE TABLE daily_market_cap (
    ticker_id    INTEGER NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    trade_date   DATE    NOT NULL,
    close_price  DOUBLE PRECISION NOT NULL,
    shares_used  BIGINT,            -- NULL if estimated from yfinance hist mcap
    market_cap   NUMERIC(20, 2) NOT NULL,
    source       VARCHAR(24) NOT NULL,
                              -- 'COMPUTED' | 'YFINANCE_HIST_MCAP' | 'APPROX_TODAYS_SHARES'
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker_id, trade_date)
);
CREATE INDEX idx_daily_market_cap_date ON daily_market_cap (trade_date, market_cap DESC);
```

### 3.5. Migration `0010_industry_indices.py` (Phase 4)

```sql
CREATE TABLE industry_index (
    industry_index_id  SERIAL PRIMARY KEY,
    industry           VARCHAR(128) NOT NULL,
    methodology        VARCHAR(32)  NOT NULL DEFAULT 'FULL_MCAP_TOP20',
    base_date          DATE         NOT NULL,
    base_value         NUMERIC(12, 4) NOT NULL DEFAULT 1000.0000,
    rebalance_cadence  VARCHAR(16)  NOT NULL DEFAULT 'SEMIANNUAL',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (industry, methodology)
);

CREATE TABLE industry_index_constituent (
    industry_index_id  INTEGER NOT NULL REFERENCES industry_index(industry_index_id)
                                        ON DELETE CASCADE,
    ticker_id          INTEGER NOT NULL REFERENCES tickers(ticker_id)
                                        ON DELETE CASCADE,
    effective_from     DATE    NOT NULL,         -- rebalancing start
    effective_to       DATE,                     -- NULL = current period
    weight             NUMERIC(8, 6) NOT NULL,   -- e.g. 0.082345 = 8.2345%
    PRIMARY KEY (industry_index_id, ticker_id, effective_from)
);
CREATE INDEX idx_index_const_active
    ON industry_index_constituent (industry_index_id, effective_to)
    WHERE effective_to IS NULL;

CREATE TABLE industry_index_value (
    industry_index_id  INTEGER NOT NULL REFERENCES industry_index(industry_index_id)
                                        ON DELETE CASCADE,
    trade_date         DATE    NOT NULL,
    value              NUMERIC(14, 4) NOT NULL,
    total_mcap         NUMERIC(20, 2),
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (industry_index_id, trade_date)
);
```

### 3.6. Migration `0011_industry_index_signals.py` (Phase 5)

```sql
CREATE TABLE industry_index_indicators (
    industry_index_id  INTEGER NOT NULL REFERENCES industry_index(industry_index_id)
                                        ON DELETE CASCADE,
    trade_date         DATE    NOT NULL,
    ema_short          DOUBLE PRECISION,
    ema_mid            DOUBLE PRECISION,
    ema_long           DOUBLE PRECISION,
    atr                DOUBLE PRECISION,
    background_regime  VARCHAR(16),     -- BULLISH | BEARISH | NEUTRAL (reuses domain enum)
    PRIMARY KEY (industry_index_id, trade_date)
);
```

No new table for "industry index signal events" — Phase 5 ships indicator values; signal-event generation on indices is deferred and may or may not happen depending on whether Phase 7 needs it.

---

## 4. Module layout

```
trip_ema/
  fundamentals/                           ← new package
    __init__.py
    industry_fetch.py        — yfinance Ticker.info → sector/industry
    shares_fetch_nse.py      — NSE XBRL pipeline (Phase 2; spike first)
    shares_fetch_yf.py       — yfinance sharesOutstanding (Phase 2 fallback)
    financials_fetch.py      — yfinance quarterly_financials (Phase 3)
    market_cap_compute.py    — daily mcap from close × shares (Phase 4)
    market_cap_backfill.py   — yfinance hist mcap backfill (Phase 4)
    index_build.py           — top-20-per-industry constituent selection (Phase 4)
    index_compute.py         — daily index value computation (Phase 4)
    index_indicators.py      — EMA/ATR on index series (Phase 5)
    runners.py               — orchestration: idempotent, resumable
    cli.py                   — argparse subcommands for each phase

  persistence/                            ← extended
    fundamentals_repo.py    — InstrumentIndustryRepository,
                              IndustryOverrideRepository,
                              InstrumentSharesRepository,
                              QuarterlyFinancialsRepository,
                              DailyMarketCapRepository,
                              IndustryIndexRepository,
                              IndustryIndexConstituentRepository,
                              IndustryIndexValueRepository,
                              IndustryIndexIndicatorsRepository,
                              FundamentalsRunRepository

  transports/http/routes/                 ← extended
    fundamentals.py         — /v1/fundamentals/*  endpoints

  scheduler/                              ← extended
    fundamentals_jobs.py    — quarterly refresh trigger, daily mcap/index trigger

tests/
  fundamentals/                           ← new
    test_industry_fetch.py
    test_shares_fetch_*.py
    test_financials_fetch.py
    test_market_cap_compute.py
    test_index_build.py
    test_index_compute.py
    test_index_indicators.py
    test_runners.py
  persistence/
    test_fundamentals_repos.py
  transports/http/
    test_fundamentals_routes.py
```

---

## 5. CLI surface

All commands under `trip-ema-etl`. Same conventions as existing subcommands: `--exchange`, `--config`, `--verbose`, exit codes 0/1/2.

```
trip-ema-etl refresh-industry            --exchange NSE [--force]
trip-ema-etl refresh-shares              --exchange NSE [--force] [--resume RUN_ID]
trip-ema-etl refresh-financials          --exchange NSE [--force] [--resume RUN_ID]
trip-ema-etl compute-market-cap          --exchange NSE [--date YYYY-MM-DD] [--backfill-from YYYY-MM-DD]
trip-ema-etl build-industry-indices      [--rebalance-date YYYY-MM-DD] [--force]
trip-ema-etl compute-index-values        [--date YYYY-MM-DD] [--backfill-from YYYY-MM-DD]
trip-ema-etl compute-index-indicators    [--date YYYY-MM-DD]
trip-ema-etl set-industry-override       --exchange NSE --symbol RELIANCE \
                                         --industry "Oil & Gas" \
                                         [--sector "Energy"] [--note "..."]
trip-ema-etl fundamentals-status         [--exchange NSE] [--phase industry]
```

Conventions:

- Every command writes a `fundamentals_run` row (RUNNING → SUCCESS/PARTIAL/FAILED).
- `--resume RUN_ID` reads `progress_json` and picks up where it left off. Used when yfinance rate-limits or the process dies.
- `--force` ignores "was already run this quarter" guards.
- Failure of a single ticker is logged and counted, never fatal to the run.

---

## 6. HTTP surface

All endpoints behind the same admin-token auth as existing `admin_*` routes. Read endpoints exposed to `trip_gui_user`.

```
GET  /v1/fundamentals/industry              ?exchange=NSE
GET  /v1/fundamentals/industry/{ticker_id}
GET  /v1/fundamentals/shares                ?exchange=NSE
GET  /v1/fundamentals/financials/{ticker_id}
GET  /v1/fundamentals/market-cap            ?exchange=NSE&date=YYYY-MM-DD
                                            &order=desc&limit=100
GET  /v1/fundamentals/indices
GET  /v1/fundamentals/indices/{industry_index_id}
                                            ?from=YYYY-MM-DD&to=YYYY-MM-DD
GET  /v1/fundamentals/indices/{id}/constituents
GET  /v1/fundamentals/indices/{id}/indicators
                                            ?from=YYYY-MM-DD&to=YYYY-MM-DD
GET  /v1/fundamentals/runs                  ?phase=industry&exchange=NSE&limit=20

POST /v1/fundamentals/refresh-industry      {exchange, force?}
POST /v1/fundamentals/refresh-shares        {exchange, force?, resume_run_id?}
POST /v1/fundamentals/refresh-financials    {exchange, force?, resume_run_id?}
POST /v1/fundamentals/compute-market-cap    {exchange, date?, backfill_from?}
POST /v1/fundamentals/build-indices         {rebalance_date?, force?}
POST /v1/fundamentals/compute-index-values  {date?, backfill_from?}
POST /v1/fundamentals/compute-index-indicators {date?}
POST /v1/fundamentals/override-industry     {ticker_id, sector, industry, note?}
DELETE /v1/fundamentals/override-industry/{ticker_id}
```

POST endpoints are async: they return the `run_id` of the newly-created `fundamentals_run` row immediately; the actual work happens in a background thread (using the existing `BackgroundWorker` plumbing, server-side). Status polled via `GET /v1/fundamentals/runs`.

---

## 7. Phase-by-phase plan

### Phase 1 — Industry classification (this session)

**Goal:** populate `instrument_industry` and `industry_override` for all configured exchanges, refreshed quarterly.

**Pieces:**

1. Migration `0006_instrument_industry.py`.
2. `fundamentals/industry_fetch.py`:
   - `fetch_industry_for_ticker(symbol, exchange) -> IndustryInfo | None` — wraps `yf.Ticker(yf_symbol).info` and pulls `sector`, `industry`.
   - 2-second sleep per ticker default; rate-limit-aware (catches `YFRateLimitError`, sleeps and retries).
3. `fundamentals/runners.py::run_industry_refresh(exchange, *, resume_run_id=None, force=False)`:
   - Creates `fundamentals_run` row.
   - Loads ticker list, filters out those refreshed this quarter unless `force`.
   - Iterates with progress checkpointing to `progress_json` every 50 tickers.
   - Honors operator overrides: never overwrites a ticker that has an `industry_override` row.
4. `persistence/fundamentals_repo.py`: `InstrumentIndustryRepository`, `IndustryOverrideRepository`, `FundamentalsRunRepository`.
5. `fundamentals/cli.py`: `refresh-industry`, `set-industry-override`, `fundamentals-status`.
6. `transports/http/routes/fundamentals.py`: the four industry-related endpoints.
7. `scheduler/fundamentals_jobs.py`: quarterly trigger (Q-end + 1 month — 30 April, 31 July, 31 October, 31 January). Just kicks `run_industry_refresh` for each configured exchange.
8. Tests:
   - Repo CRUD against Postgres fixture.
   - `industry_fetch` against a mocked yfinance.
   - Runner integration: real DB, mocked yfinance, full flow including resume.
   - CLI smoke tests.
   - HTTP route tests.

**Verification query:**

```sql
SELECT
    t.exchange,
    COUNT(*) AS total,
    COUNT(ii.ticker_id) FILTER (WHERE ii.industry IS NOT NULL) AS classified,
    COUNT(io.ticker_id) AS overridden
FROM tickers t
LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
LEFT JOIN industry_override io ON io.ticker_id = t.ticker_id
WHERE t.is_active
GROUP BY t.exchange
ORDER BY t.exchange;
```

Expectation: 60–85% classified per exchange after one successful run.

**Estimated size:** ~700 lines + tests.

---

### Phase 2 — Shares outstanding

**Goal:** populate `instrument_shares` for all configured exchanges. NSE from XBRL filings; others from yfinance.

**Sub-plan because XBRL is the genuinely uncertain part:**

1. **Spike first (in `scripts/spike_nse_xbrl.py`):** ~150 lines, throwaway. Bootstrap an NSE session with cookies, hit the JSON API behind https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern, download one filing's XBRL, parse out `NumberOfFullyPaidUpEquityShares`. Goal: prove it works end-to-end for 5 NSE tickers.
2. **Pause and review** the spike output with you before writing production code. If the spike reveals more complexity than expected (multiple taxonomy versions, malformed XML, anti-bot hardening), we discuss before continuing.
3. **Productionize:** turn the spike into `fundamentals/shares_fetch_nse.py` with proper retry/backoff, taxonomy version handling, error categorization.
4. yfinance fallback in `fundamentals/shares_fetch_yf.py`.
5. Runner, repo, CLI, HTTP, scheduler hook.

**Estimated size:** ~600 lines if XBRL is well-behaved; up to ~1000 if it's not. **This is the phase most likely to need its own follow-up session.** Worth flagging up-front.

---

### Phase 3 — Quarterly financials

**Goal:** populate `quarterly_financials` (revenue, EBITDA for last 4 reported quarters).

**Pieces:**

1. Migration `0008_quarterly_financials.py`.
2. `fundamentals/financials_fetch.py`: wraps `yf.Ticker(symbol).quarterly_financials`. Honestly, this is a small wrapper — yfinance does the heavy lifting.
3. Runner, repo, CLI, HTTP, scheduler hook.
4. Tests.

**Realistic expectation:** large-cap NSE = 3–4 of 4 quarters populated; mid-cap = 1–2; small-cap/BSE-only = often nothing. Phase 7 will need to handle the NULL case gracefully.

**Estimated size:** ~400 lines.

---

### Phase 4 — Industry indices

**Goal:** build per-industry indices and populate their daily values.

**Sub-pieces:**

#### 4.1 Daily market cap

Done first. For each `(ticker, trade_date)`:
- If `trade_date < shares.as_of_date - 180 days` AND yfinance has historical mcap for that date → use it, source `YFINANCE_HIST_MCAP`.
- Else if `shares.shares_outstanding` is known → `close × shares`, source `COMPUTED`.
- Else if approximating with today's shares → `close × today_shares`, source `APPROX_TODAYS_SHARES` (used for backfill where shares-outstanding wasn't populated yet).

The 180-day threshold is the "boundary" you specified: use yfinance hist mcap for dates more than 6 months back, computed for recent dates.

#### 4.2 Index construction

For each industry with ≥20 active tickers:
- Sort by latest market cap, take top 20.
- Compute weights as `mcap / sum(mcaps)`.
- Insert constituent rows with `effective_from = rebalance_date`, `effective_to = NULL`.
- On rebalance, close out previous constituents (set their `effective_to = rebalance_date - 1`).

Rebalance dates: **1 April and 1 October** (semi-annual, aligned to Indian FY). Operator-triggerable mid-cycle via `--force`.

#### 4.3 Daily index value

For each industry index on each trade_date in range:
```
value(d) = base_value × Σ (weight_i × close_i(d) / close_i(effective_from))
```

This is the "modified Laspeyres" form used by NSE/BSE for sectoral indices. Weights are fixed at rebalance; daily value moves with constituent prices.

**Backfill strategy:** for the first build, walk every trade date from the earliest date for which we have market cap for at least 20 tickers in the industry, forward to today. This is potentially years of data per index.

#### 4.4 Tests

- Top-20 selection with various tie-break scenarios
- Weight computation sums to 1.0 ± 1e-6
- Rebalance correctly closes old constituents
- Daily value matches a hand-computed example
- Backfill is idempotent (re-running produces identical results)

**Estimated size:** ~1000 lines including comprehensive tests.

---

### Phase 5 — Indicator stack on indices

**Goal:** apply the same 3-EMA + ATR + background regime computation to each industry index series.

**Pieces:**

1. Migration `0011_industry_index_indicators.py`.
2. `fundamentals/index_indicators.py`: takes an `industry_index_id` and a date range, reads from `industry_index_value`, applies the existing indicator stack from `trip_ema.engine`, writes to `industry_index_indicators`.
3. Tests: confirm the same indicator function produces the same outputs when given the same data, regardless of whether the input is a ticker bar series or an index value series.

**Key insight:** the indicator code already takes a numeric series — we don't need to re-implement anything. Just adapt the input/output.

**Estimated size:** ~300 lines.

---

### Phase 6 — GUI display of fundamentals + indices

**Goal:** surface the new data in existing views.

**Pieces:**

1. **Tickers tab detail panel** gets new rows: Industry / Sector, Market Cap, Revenue (Q-4 .. Q-1), EBITDA (Q-4 .. Q-1).
2. **New Indices tab** (between Signals and Subscribers in the sidebar): per-industry list, sortable, click for chart of index value + indicators.
3. HTTP client methods in `trip_ema/gui/http_client.py` for the new endpoints.
4. Tests for the new views, following the patterns in `tests/gui/test_signals.py`.

**Estimated size:** ~800 lines.

---

### Phase 7 — Stock chart shows industry index underneath

**Goal:** under any stock chart, show the matching industry index chart with EMAs/ATR/regime overlay.

**Display only this phase — no recommendation logic yet.**

**Pieces:**

1. `SignalsView`'s existing chart widget extended with a second pyqtgraph PlotItem below the main one, linked X-axis.
2. New HTTP client method to fetch industry index values + indicators for a given ticker (server-side joins ticker → industry → index).
3. Tests: pyqtgraph chart rendering is hard to test directly, so we test the data plumbing and let the chart be smoke-tested by `pytest-qt`.

**Estimated size:** ~500 lines.

---

### Phase 8 — db_diagnostics.sh sorted by market cap

**Goal:** modify `scripts/db_diagnostics.sh` so each section that lists tickers orders them by market cap descending.

**Pieces:**

1. Modify the existing SQL queries to `LEFT JOIN daily_market_cap` for the latest available date and `ORDER BY market_cap DESC NULLS LAST`.
2. Document the dependency (sections will show "MCAP unknown" for tickers without a Phase 4 row).

**Estimated size:** ~50 lines of bash + a documentation note.

---

## 8. Scheduling

Three triggers, all wired into the existing `MarketOrchestrator`:

| Trigger | Cadence | Phases run |
|---|---|---|
| Quarterly fundamentals refresh | 30 Apr, 31 Jul, 31 Oct, 31 Jan @ 03:00 IST | 1, 2, 3 — for all configured exchanges, in series per exchange (yfinance rate limiting) |
| Daily market cap & index | After EoD bar load completes (existing event) | 4 (daily values), 5 (indicators) |
| Semi-annual index rebalance | 1 Apr, 1 Oct @ 04:00 IST | 4 (constituent rebuild) |

Operator can trigger any of these manually via CLI or HTTP. The scheduler hook is just a thin wrapper that calls the same runner functions.

---

## 9. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| yfinance industry classification has wide gaps for Indian mid/small caps | High | Manual override table; Groww.in fallback as Phase 1.5 if coverage is poor |
| yfinance rate-limits during a large refresh | High | 2-second sleep per ticker, resumable runs, `progress_json` checkpointing every 50 tickers |
| NSE XBRL fetch is harder than expected (anti-bot, taxonomy versions) | Medium-High | Spike first, review with operator, only productionize after spike clean |
| yfinance quarterly financials are mostly NULL for Indian small caps | High | Documented limitation; Phase 7 must handle NULL gracefully |
| Historical market cap from yfinance is inconsistent for stocks with corporate actions | Medium | `daily_market_cap.source` column records the approximation used; analytics can filter |
| Index construction produces weird results when an industry has only 3–4 active tickers | Medium | Minimum-constituent threshold of 20; industries below that get no index |
| Index backfill is slow (years × industries × tickers) | Low-Medium | Backfill once on first run; incremental thereafter; runs at 03:00 IST off-cycle |
| Phase 7 chart rendering blocks the GUI thread for large date ranges | Low | Use existing `BackgroundWorker` plumbing; chart in a worker, signal back to main thread |

---

## 10. Verification harness

Each phase has a verification query in its phase section above. **Phase 4 also gets a cross-check against published indices:**

```sql
-- Our "Information Technology" index vs NIFTY IT
SELECT
    iiv.trade_date,
    iiv.value AS our_it,
    -- compare against a manually-loaded NIFTY IT close (operator stores
    -- a few sample dates in a temp table; see verification SQL in
    -- docs/FUNDAMENTALS_VERIFICATION.md after Phase 4 ships)
FROM industry_index_value iiv
JOIN industry_index ii USING (industry_index_id)
WHERE ii.industry = 'Information Technology'
ORDER BY iiv.trade_date DESC
LIMIT 30;
```

Expectation: our index will differ from NIFTY IT by 5–25% in absolute level (because we use full mcap, not free-float; and our top-20 may not match NIFTY's exact constituents), but **day-over-day percentage changes should correlate strongly** (r > 0.9). If they don't, the data plumbing is wrong.

---

## 11. Decision log

Decisions that were made during the design conversation and shouldn't be revisited without a deliberate trigger:

1. **yfinance as default fundamentals source.** Properly licensed, integrated, sufficient for MVP. Better sources can be layered in later via `classification_source` / `shares_source` columns.
2. **No Screener.in, MoneyControl, RupeeVest scraping.** ToS violations, fragile, silent-failure risk into trading signals.
3. **Full market cap, not free-float.** Documented deviation from NIFTY methodology. Schema carries `free_float_factor` for forward compatibility.
4. **Semi-annual index rebalancing.** Aligned to Indian FY (1 Apr / 1 Oct). NIFTY uses similar.
5. **Phase 7 recommendation logic deferred** to its own session after data is visible.
6. **Phase 9 (per-stock corporate filings panel) deferred** entirely.
7. **One alembic migration per phase**, additive only.
8. **CLI + HTTP for every phase command**, sharing the same runner functions.
9. **`fundamentals_run` table tracks all phase runs** (no per-phase last-refresh column).
10. **Quarterly fundamentals refresh at Q-end + 1 month** (when filings are public).

---

## 12. What I want from you before I start coding

1. Sign off on this document (or push back on specifics).
2. Confirm Phase 1 is what to build this session.
3. Confirm I should stop and review with you before Phase 2's XBRL productionization (spike first).

---

## 13. Post-Phase-1 addendum: ETF classification

Added after Phase 1 ships. The original design treated all configured-
exchange tickers as equities. The first NSE refresh revealed that
~10-15% of the unclassified tail were ETFs (instrument_type='ETF'),
not equities yfinance failed on. Classification decision tree:

### What we built

- **Migration 0007**: adds `'INSTRUMENT_TYPE'` as an allowed value
  for `instrument_industry.classification_source`. Adds nullable
  `etf_long_name VARCHAR(256)` column.
- **Runner branching**: `run_industry_refresh()` checks `instrument_type`
  per ticker. ETFs go through `_classify_etf()` which writes
  `(sector='ETF', industry='ETF', source='INSTRUMENT_TYPE')` plus
  whatever yfinance returns for `longName` (the only useful ETF
  metadata yfinance provides for Indian ETFs).
- **Repo additions**: `upsert_etf()`, `coverage_counts_by_type()`.
- **CLI**: `fundamentals-status` now shows EQ vs ETF coverage
  separately. New `backfill-etf-classification` command for
  one-time fix-up after the ETF branch landed.

### What we deliberately did NOT build

These are real deferred items, not casual omissions. They have
intentional schema-level placeholders so they can be added later
without breaking compatibility:

- **Richer ETF taxonomy** (etf_category, etf_fund_family,
  etf_underlying, etf_inception_date, etf_expense_ratio).
  Reason: yfinance reconnaissance (`scripts/probe_etf_yfinance.py`)
  showed Indian ETFs return essentially nothing on these fields.
  Building empty columns is technical debt. Defer until a real ETF
  data source is identified (paid API, NSE indices document parser,
  systematic manual entry).
- **HTTP routes for ETFs**: `/v1/fundamentals/etf` GET endpoints
  (list, by_ticker_id, by_fund_family). The existing
  `/v1/fundamentals/industry` endpoint already returns ETF rows
  alongside equity rows (since they share the same table); a
  dedicated ETF endpoint waits until there's a richer schema and
  a consumer.
- **CLI list/query commands for ETFs**: `list-etfs`, `etf-by-family`,
  etc. Same reasoning — wait until there's a richer schema.

### Reconciling with the original Phase 1 design

The original Section 7 / Phase 1 contract specified "yfinance for all
configured exchanges, refreshed quarterly." That remains correct. The
ETF branch is an internal refinement of HOW the refresh works for
non-equity instruments, not a change to the external contract.

The quarterly scheduler (Section 8) is unaffected. The same scheduler
hook fires `run_industry_refresh` for each configured exchange; the
runner internally branches on `instrument_type` per ticker.

### Verification (post-backfill)

```sql
SELECT
    t.exchange,
    t.instrument_type,
    COUNT(*) AS total,
    COUNT(ii.ticker_id) FILTER (WHERE ii.industry IS NOT NULL) AS classified,
    COUNT(ii.ticker_id) FILTER (WHERE ii.classification_source = 'INSTRUMENT_TYPE') AS etf_bucketed,
    COUNT(ii.etf_long_name)                                    AS with_etf_long_name
FROM tickers t
LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
WHERE t.is_active
GROUP BY t.exchange, t.instrument_type
ORDER BY t.exchange, t.instrument_type;
```

Expectation after a complete refresh + ETF backfill: ETF rows have
`industry='ETF'`, `classification_source='INSTRUMENT_TYPE'`, and
most (but not all) have `etf_long_name` populated.

---

## 14. Phase 2 addendum: shares-outstanding via yfinance

Original Phase 2 plan was NSE XBRL primary, yfinance fallback for
non-NSE exchanges. The NSE XBRL spike (scripts/spike_nse_xbrl.py)
revealed that NSE sits behind Akamai Bot Manager, which fingerprints
at the TLS layer (JA3/JA4). Python `requests` is rejected with HTTP
403 at the homepage before any cookies are issued — bypassing
requires browser-impersonating libraries (curl_cffi) that are in
an explicit arms-race with Akamai and break on a 6-12 month cycle.

For a quant trading system where bad shares-outstanding feeds bad
market caps feed bad signals, "works today, might not next quarter"
is unacceptable on the critical path. The XBRL ambition is deferred
to a sidecar cross-validation feature (not blocking critical path).

### What we shipped

- **Migration 0008**: `instrument_shares` time-series table with
  composite PK `(ticker_id, as_of_date)`, supporting
  `shares_source` values `YFINANCE` / `YFINANCE_IMPLIED` /
  `NSE_XBRL` / `MANUAL_OVERRIDE`. Time-series shape (not single-row)
  so corporate actions, refreshes, and the future XBRL sidecar can
  coexist without overwriting each other.

- **shares_fetch.py**: yfinance fetcher with the same typed-exception
  pattern as industry_fetch (`SharesFetchRateLimited` /
  `SharesFetchTransient` / `SharesFetchPermanent`). Prefers
  `sharesOutstanding` (source `YFINANCE`); falls back to
  `impliedSharesOutstanding` (source `YFINANCE_IMPLIED`) only when
  the direct field is NULL. The implied number is back-derived by
  yfinance from `marketCap / price`, so analytics can filter on
  `shares_source` to distinguish higher-trust direct numbers from
  the derived fallback.

- **run_shares_refresh()** in runners.py: mirrors
  `run_industry_refresh` — serial yfinance calls, checkpointed every
  50 tickers, resumable via `progress_json`, 80-day skip-if-recent
  guard, KeyboardInterrupt → PARTIAL close. Differences:
    - No instrument-type branching; ETFs have meaningful
      `sharesOutstanding` (units issued) and are included.
    - No operator-override skip table; the override path is "insert
      a row with shares_source='MANUAL_OVERRIDE'" — the time-series
      shape accommodates operator-set values without a separate table.
    - Inserts (not upserts-in-place); the composite PK gives
      same-day idempotency via `ON CONFLICT DO UPDATE`.

- **InstrumentSharesRepository** in fundamentals_repo.py:
  `get_latest`, `get_as_of(date)`, `get_at(date)`, `list_history`,
  `list_latest_for_exchange`, `coverage_counts_by_type`,
  `insert_snapshot`, `delete_snapshot`. The `coverage_counts_by_type`
  query uses `COUNT(DISTINCT t.ticker_id)` rather than `COUNT(*)`
  because a LEFT JOIN against the time-series table multiplies rows
  by history depth.

- **CLI**: `refresh-shares --exchange NSE [--force] [--resume RUN_ID]`,
  plus `fundamentals-status` now reports shares coverage in a
  separate section alongside industry coverage.

- **HTTP**: `GET /v1/fundamentals/shares`,
  `GET /v1/fundamentals/shares/{ticker_id}`,
  `GET /v1/fundamentals/shares/{ticker_id}/history`,
  `POST /v1/fundamentals/refresh-shares` (fire-and-forget via
  AppState.etl_executor; returns 202 immediately).

- **Scheduler**: `QuarterlyFundamentalsScheduler` now fires both
  `industry` and `shares` refreshes at quarter-end+1month. Same
  60-day window, same 80-day skip-if-recent per (phase, exchange).
  Industry runs first per exchange so that downstream Phase 4 code
  reading industry-then-shares doesn't see stale industry data.

### What we deliberately did NOT build

- **NSE XBRL primary source.** Akamai blocks naked Python requests
  at the TLS layer. We chose not to put TLS-fingerprint evasion on
  the critical path. The XBRL pipeline is deferred to a future
  "shares cross-validation sidecar" — see §15 below.

- **A separate `shares_override` table.** Operator overrides use
  `shares_source='MANUAL_OVERRIDE'` on an `insert_snapshot` call.
  The runner's quarterly refresh always inserts a new row, never
  modifies prior rows, so manual snapshots persist. Analytics that
  want to ignore manual rows filter on `shares_source`.

- **Free-float factor population.** `free_float_factor` is reserved
  for the XBRL sidecar; yfinance has no equivalent field for Indian
  tickers. Phase 4's index construction uses full market cap as
  documented in §2.

### Verification

After running `refresh-shares --exchange NSE`:

```sql
SELECT
    t.exchange,
    t.instrument_type,
    COUNT(DISTINCT t.ticker_id) AS total_tickers,
    COUNT(DISTINCT s.ticker_id) FILTER (
        WHERE s.shares_outstanding IS NOT NULL
    ) AS with_shares,
    COUNT(DISTINCT s.ticker_id) FILTER (
        WHERE s.shares_source = 'YFINANCE'
    ) AS direct_source,
    COUNT(DISTINCT s.ticker_id) FILTER (
        WHERE s.shares_source = 'YFINANCE_IMPLIED'
    ) AS implied_source
FROM tickers t
LEFT JOIN instrument_shares s ON s.ticker_id = t.ticker_id
WHERE t.is_active
GROUP BY t.exchange, t.instrument_type
ORDER BY t.exchange, t.instrument_type;
```

Realistic expectation for NSE:
  - EQ: ~85-90% with_shares, mostly via YFINANCE direct.
    YFINANCE_IMPLIED expected at 5-10% of named for mid/small-caps
    where the direct field is NULL.
  - ETF: ~95-100% with_shares (unit counts are reliable on yfinance
    for these).

### Phase 4 implications

Phase 4's "compute historical market cap" path now has the choice
of three sources for shares as-of a date:

  1. `instrument_shares.get_as_of(date)` — the latest snapshot at or
     before the requested date, via composite PK.
  2. `yfinance.Ticker.history(period='max', actions=True)` — split-
     adjusted price + split events, from which a shares-history can
     be reconstructed for dates predating our Phase 2 data.
  3. Approximation: `close × current_shares` for dates where we have
     no better signal.

Per the design doc §2 the boundary is 180 days: dates older than
that use yfinance historical mcap directly; newer dates use
`close × shares_outstanding` from our Phase 2 data. Phase 4 will
implement this; Phase 2 just lays the data.

---

## 15. Deferred: NSE XBRL shares cross-validation sidecar

Reserved for a future session. Sketch:

- Separate from the quarterly refresh path. Operator-triggered.
- Uses curl_cffi or equivalent to bypass Akamai TLS fingerprinting.
- Fetches SHP filings quarterly per ticker. Parses XBRL via lxml
  with localname-tolerant XPath against the SEBI INDAS taxonomy.
- Writes rows with `shares_source='NSE_XBRL'` to the same
  `instrument_shares` table. The time-series shape means XBRL rows
  coexist with yfinance rows for the same ticker at different dates.
- Cross-validation query flags any (ticker, quarter) pair where
  yfinance and XBRL disagree by more than 5%.
- Will populate `free_float_factor` from the SHP filing's promoter
  vs public breakdown.
- Will populate `xbrl_url` for audit trail.

The sidecar is OFF the critical path — if Akamai breaks it, Phase
2's yfinance flow keeps working and Phase 4 keeps building indices.
