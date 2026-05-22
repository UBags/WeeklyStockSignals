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

---

## 16. Phase 3.A addendum: quarterly financials via yfinance

Original Phase 3 plan: yfinance `quarterly_financials` for revenue
and EBITDA, four quarters, simple PK. The plan landed with two
modifications:

- **Time-series PK** (`ticker_id, fiscal_quarter, fetched_at`)
  rather than single-row, so restatements and yfinance data
  revisions are detectable. Operator's call; the cost is the same
  PK-multiplication issue we hit on shares (need
  `COUNT(DISTINCT t.ticker_id)` in coverage queries).
- **EBITDA computation deferred to Phase 3.B.** Phase 3.A stores
  whatever yfinance puts in the `EBITDA` or `Normalized EBITDA`
  rows of `quarterly_income_stmt`, NULL when both are missing.
  Phase 3.B (next session) will probe yfinance's actual line-item
  availability for Indian tickers, then implement
  `operating_income + depreciation_amortization` only after we
  verify those fields are reliably populated. Same discipline
  pattern as the bhavcopy / ETF / XBRL probes.

### What we shipped

- **Migration 0009**: `quarterly_financials` with composite PK
  `(ticker_id, fiscal_quarter, fetched_at)`. `source` column
  supports `YFINANCE` / `YFINANCE_COMPUTED` (reserved) / `NSE_XBRL`
  (reserved) / `MANUAL_OVERRIDE`. Two indexes: one for
  `DISTINCT ON (ticker_id, fiscal_quarter) ORDER BY fetched_at DESC`
  reads (the latest-per-quarter query), one on `fiscal_quarter DESC`
  for time-axis analytics ("everyone's Q3 2025").

- **financials_fetch.py**: yfinance fetcher with the typed-exception
  pattern. Tries multiple candidate names for the revenue and
  EBITDA rows because yfinance's labels vary across companies and
  yfinance versions (`Total Revenue` / `Revenues` /
  `Revenue From Operations`; `EBITDA` / `Normalized EBITDA`).
  Returns up to 4 quarters per ticker. Quarters with neither
  revenue nor ebitda populated are filtered out — no value in
  recording rows that are entirely empty.

- **run_financials_refresh()** in runners.py: mirrors
  `run_shares_refresh`. Each ticker can produce 0-4 quarter rows;
  the runner writes all of them in one transaction per ticker.
  Same checkpoint-every-50, same 80-day skip-if-recent, same
  rate-limit cool-off retry semantics.

- **QuarterlyFinancialsRepository**: `get_latest`,
  `get_latest_n_quarters`, `get_history`, `list_latest_for_exchange`,
  `coverage_counts_by_type`, `insert_snapshot`, `delete_for_ticker`.
  Coverage query reports `with_revenue` and `with_ebitda` as
  separate columns because that gap is the important quality
  signal — yfinance has revenue for most Indian tickers but EBITDA
  for far fewer.

- **CLI**: `refresh-financials --exchange NSE [--force] [--resume RUN_ID]`.
  `fundamentals-status` now reports three coverage sections:
  industry, shares, and financials.

- **HTTP**: `GET /v1/fundamentals/financials` (latest per ticker),
  `GET /v1/fundamentals/financials/{ticker_id}/quarters?n=4`
  (latest N quarters for one ticker),
  `GET /v1/fundamentals/financials/{ticker_id}/quarter/{date}/history`
  (restatement detection for one (ticker, quarter) pair),
  `POST /v1/fundamentals/refresh-financials` (fire-and-forget).

- **Scheduler**: `SCHEDULED_PHASES` now `('industry', 'shares',
  'financials')`. Each phase fires per exchange at quarter-end+
  1month windows. Industry first, shares second, financials third
  per exchange — Phase 4 / Phase 7 code that joins across the three
  sees them in a consistent freshness order.

### Realistic coverage expectation

Based on yfinance's data quality for Indian tickers:
  - **Revenue**: likely 80-90% coverage. Most NSE tickers yfinance
    knows about have at least one quarter of revenue.
  - **EBITDA**: likely 40-60% coverage. Mid/small-caps frequently
    have NULL EBITDA. This gap motivates the Phase 3.B computation
    work.

These will be confirmed when the operator runs the first
`refresh-financials --exchange NSE` against the real yfinance.

### What's deliberately NOT in Phase 3.A

- **EBITDA computation** from operating income + D&A. Reserved
  for Phase 3.B after probing yfinance's field reliability.
- **Other income-statement line items** (gross profit, operating
  income, net income, etc.). Phase 3.A is scoped to revenue +
  EBITDA only, per the original design doc §1.
- **Balance-sheet items** (assets, liabilities, equity). Out of
  scope for this entire fundamentals vertical slice; would be a
  Phase 10+ addition if ever needed.
- **Annual financials**. yfinance exposes them via `financials`
  (annual) vs `quarterly_financials`; we only fetch quarterly.

### Verification

After running `refresh-financials --exchange NSE`:

```sql
SELECT
    t.exchange,
    t.instrument_type,
    COUNT(DISTINCT t.ticker_id) AS total_tickers,
    COUNT(DISTINCT qf.ticker_id) FILTER (
        WHERE qf.revenue IS NOT NULL
    ) AS with_revenue,
    COUNT(DISTINCT qf.ticker_id) FILTER (
        WHERE qf.ebitda IS NOT NULL
    ) AS with_ebitda,
    ROUND(100.0 * COUNT(DISTINCT qf.ticker_id) FILTER (
        WHERE qf.ebitda IS NOT NULL
    ) / NULLIF(COUNT(DISTINCT t.ticker_id), 0), 1) AS ebitda_pct
FROM tickers t
LEFT JOIN quarterly_financials qf ON qf.ticker_id = t.ticker_id
WHERE t.is_active
GROUP BY t.exchange, t.instrument_type
ORDER BY t.exchange, t.instrument_type;
```

The `ebitda_pct` column is the input that drives whether Phase 3.B's
EBITDA-computation work is worth doing — if it's >80% we may not
bother; if it's <50% we definitely need to compute.

---

## 17. Empirical Phase 1-3 findings + Phase 4A foundation

After running the full Phase 1, 2, and 3 refreshes against the
NSE universe (2347 active equities + 320 ETFs), the coverage
numbers came back materially better than the predictions in §1-3
of this doc:

| Metric | Empirical | Original prediction |
|---|---|---|
| Industry (EQ) | 96.2% | 60-85% |
| Shares (EQ)   | 95.4% | 85-90% |
| Revenue (EQ)  | 95.1% | 80-90% |
| EBITDA (EQ)   | **91.9%** | 40-60% |

The EBITDA number is the surprise. yfinance has materially better
EBITDA coverage for Indian equities than its general reputation
suggests. The EBITDA-to-revenue ratio is **96.5%** — of the
equities yfinance gave us *any* financial data for, almost all
also had a populated EBITDA row.

**Implication: Phase 3.B (computed EBITDA from operating income +
D&A) is explicitly NOT pursued.** The premise was "EBITDA gap large
enough to justify computation despite silent-failure risk." A 4%
gap (77 tickers out of 2347) doesn't justify it. Those 77 tickers
join the long tail addressed via the operator override pattern
that Phase 1 established for industry classification.

### Trust profile of Phase 2 shares data

Real shares_source breakdown after the first NSE refresh:
  - YFINANCE         : 2239 tickers
  - YFINANCE_IMPLIED :    1 ticker

The fallback path effectively never fires. Phase 4's downstream
code can treat all shares as roughly equal-trust without filtering
on shares_source.

### Phase 4A foundation (this session)

Phase 4A is split across multiple sessions. This session shipped
the foundation:

- **Migration 0010**: `daily_market_cap` table with PK
  `(ticker_id, trade_date)`, columns close_price, shares_used,
  market_cap NUMERIC(22,2), source VARCHAR(24) with allowed values
  COMPUTED / YFINANCE_HIST_MCAP / APPROX_TODAYS_SHARES /
  MANUAL_OVERRIDE, computed_at. Index on (trade_date,
  market_cap DESC) for Phase 4B's "top-N tickers by mcap on date X"
  query.
- **Domain types**: MARKET_CAP_SOURCES tuple + DailyMarketCap
  dataclass (frozen, slots).
- **Test coverage**: domain layer tests pinning the contract.
  Repo / fetcher / runner / CLI / HTTP tests deferred to subsequent
  sessions.

### Phase 4A still-to-build (next session)

- DailyMarketCapRepository (get_latest, get_at, list_for_date,
  upsert_snapshot, coverage_counts_by_date, delete_for_date) +
  tests.
- market_cap_compute.py — computes one day's mcap for all active EQ
  tickers, joining ohlcv_bars × instrument_shares.
- yfinance historical mcap fetcher for backfill of dates older
  than the earliest instrument_shares row. Needs a probe-first to
  verify the API shape before committing to the integration.
- Runner run_market_cap_backfill walking the 1-year date range
  (the operator's choice for Phase 4A backfill horizon).
- CLI: compute-market-cap [--date YYYY-MM-DD] [--backfill-from
  YYYY-MM-DD]. fundamentals-status extended to show mcap coverage.
- HTTP routes (3 endpoints).

### Phase 4A scheduler decision

Market cap is daily-cadence, not quarterly. The existing
QuarterlyFundamentalsScheduler doesn't fit. For Phase 4A, the
mcap compute is **operator-triggered only** (CLI). The natural
integration point for daily mcap is the bars-ingestion daemon, but
that integration belongs in Phase 4C alongside the other backfill/
integration work.

### Phase 4 design decisions (locked in)

These are settled and will be threaded through Phase 4B/C:

- **Index methodology**: modified Laspeyres.
  `value(t) = base × Σ_i [weight_i × close_i(t)/close_i(t_rebalance)]`
  Weights fixed at rebalance.
- **Mid-cycle delisting**: drop and re-normalize remaining weights.
- **Backfill horizon**: 1 year (methodology validation against
  NIFTY sectoral indices; not a long backtest).
- **Minimum constituents threshold**: 10 (changed from the design
  doc default of 20). Industries with fewer than 10 active
  classified tickers get no index. The 10 is operator-configurable
  via a runtime parameter, not a hardcode.
- **ETFs excluded from constituent selection** via
  `WHERE instrument_type = 'EQ'`.

---

## 18. Phase 4A part 2 (repo + compute layer)

Continuing from §17 (Phase 4A foundation). This session added the
repository, the COMPUTED-row compute logic, and the probe script
for the yfinance historical mcap API shape.

### Shipped this session

- **DailyMarketCapRepository** in fundamentals_repo.py: get_latest,
  get_at, list_for_date (the Phase 4B hot path, supporting both
  exchange filter and top-N limit), list_history,
  coverage_counts_by_date (per-date breakdown of COMPUTED vs
  YFINANCE_HIST_MCAP source counts), upsert_snapshot (idempotent
  via ON CONFLICT DO UPDATE), delete_for_date, delete_for_ticker.

  upsert_snapshot validates:
    - source is in MARKET_CAP_SOURCES;
    - market_cap >= 0;
    - shares_used is non-None for COMPUTED / APPROX_TODAYS_SHARES /
      MANUAL_OVERRIDE (audit trail requirement).

- **compute_market_cap_for_date()** in market_cap_compute.py:
  iterates active EQ tickers on an exchange, joins ohlcv_bars × 
  instrument_shares, writes daily_market_cap rows. Returns a
  ComputeResult with per-category counts (computed, approx,
  skipped_no_close, skipped_no_shares).

  Source policy in the compute layer:
    - Shares known as-of trade_date or earlier → COMPUTED row.
    - Shares known ONLY from a later date → APPROX_TODAYS_SHARES
      row (low-trust, marked for downstream filter). Operator-
      configurable via allow_approx_fallback parameter.
    - No shares at all → skipped_no_shares.
    - No bar for the date → skipped_no_close.

  ETFs excluded via WHERE instrument_type = 'EQ' in the selection
  query, matching the Phase 4 design decision in §17.

- **probe script** for yfinance historical mcap (scripts/
  probe_yfinance_hist_mcap.py). Tests 5 NSE tickers across cap
  tiers and reports which yfinance approaches surface a usable
  historical shares series:
    - Ticker.history() for prices
    - Ticker.info['marketCap'] for current snapshot
    - Ticker.get_shares_full() for historical shares (recently
      added; may not exist on older yfinance versions)
    - Manual close × current shares sanity check
  The probe runs against real yfinance and the operator pastes the
  output back; the fetcher design (next session) depends on which
  approach returns usable data.

### Test counts

- 15 new repo tests in test_fundamentals_repos.py
- 10 new integration tests in test_market_cap_compute.py
  (exercising the real ohlcv_bars × instrument_shares JOIN paths)
- All 270 prior fundamentals tests still pass

Total: 295 fundamentals-domain + migration tests pass.

### Still pending for Phase 4A (next session)

- Operator runs the yfinance hist-mcap probe; reports back the
  output.
- Design and build the historical mcap fetcher based on probe
  outcomes:
    - If get_shares_full works → fetch historical shares + history
      prices → compute COMPUTED-style rows even for backfill dates.
    - If get_shares_full fails → use yfinance's pre-baked
      historical mcap series (if available) or fall back to
      APPROX_TODAYS_SHARES.
- Runner `run_market_cap_backfill(db_engine, exchange, from_date,
  to_date)` walking the date range. Per-ticker outer loop so
  resume can pick up mid-backfill. Fundamentals_run row tracks
  progress.
- CLI command `compute-market-cap --exchange NSE [--date YYYY-MM-DD]
  [--backfill-from YYYY-MM-DD]`. Update fundamentals-status to
  show mcap coverage section.
- HTTP routes: GET /v1/fundamentals/market-cap?exchange=NSE&
  date=YYYY-MM-DD&limit=100&order=desc, GET /v1/fundamentals/
  market-cap/{ticker_id}/history, POST /v1/fundamentals/
  compute-market-cap.
- Per the §17 scheduler decision: mcap compute is operator-
  triggered for Phase 4A. Daily scheduling is a Phase 4C concern
  alongside bars-ingestion-daemon integration.

---

## 19. Phase 4A part 3 (fetcher + runner + CLI + HTTP, complete)

Continuing from §18. The operator's probe of yfinance's
historical mcap API (scripts/probe_yfinance_hist_mcap.py, run
2026-05-22 against 5 NSE tickers across cap tiers) returned a
clean result:

- `Ticker.get_shares_full()` returns ~150-250 historical shares
  snapshots per ticker covering ~16 months back, for ALL cap
  tiers (large/mid/small).
- `Ticker.history(period='1y')` returns daily OHLCV for the same
  range.
- The manual `close × current_shares` sanity check matched
  yfinance's reported `marketCap` to within 0.0000001 ratio for
  all 5 samples — confirming yfinance's mcap is just close ×
  shares.

This is much cleaner than expected. **All backfill rows now get
source=COMPUTED**, computed from yfinance-historical-shares ×
yfinance-historical-close. There's no need for the separate
YFINANCE_HIST_MCAP source path or APPROX_TODAYS_SHARES fallback
on backfill — both remain in the schema for forward compatibility
but aren't written by the Phase 4A runner.

### Shipped this session

- **market_cap_fetch.py**: `fetch_historical_mcap_for_ticker(
  symbol, exchange, from_date, to_date) → list[HistoricalMcapPoint]`.
  Calls Ticker.history() and Ticker.get_shares_full(), joins them
  per-date via an asof merge cursor (O(n+m), not O(n*m)), filters
  to the requested range. Same typed-exception pattern as the
  other Phase 2/3 fetchers. 16 fetcher tests.

- **run_market_cap_backfill()** in runners.py: walks active EQ
  tickers (ETFs excluded via instrument_type='EQ' filter), one
  yfinance HTTP per ticker covering the full date range. Same
  checkpoint-every-50, 80-day skip-if-recent, expected_phase=
  'market_cap' for resume. Persists the date range in
  progress_json so resume uses the same range as the original.
  11 runner tests.

- **CLI**: `compute-market-cap --exchange NSE` with two modes:
    - Single-day: `--date YYYY-MM-DD` (default today) calls
      compute_market_cap_for_date — uses local ohlcv_bars ×
      instrument_shares JOIN, no yfinance HTTP.
    - Backfill: `--backfill-from YYYY-MM-DD [--backfill-to ...]`
      calls run_market_cap_backfill — yfinance per-ticker over
      the range.
  Single-day mode returns rc=1 if nothing was written (most
  likely cause: bars not yet ingested for the date).
  `fundamentals-status` now reports a "Daily market cap coverage"
  section showing per-date breakdown of COMPUTED vs
  YFINANCE_HIST_MCAP source counts. 5 new CLI tests.

- **HTTP**: GET /v1/fundamentals/market-cap?trade_date=YYYY-MM-DD
  (Phase 4B's top-N hot path read endpoint, optional exchange
  filter, configurable limit), GET /v1/fundamentals/market-cap/
  {ticker_id}/history, POST /v1/fundamentals/compute-market-cap
  (fire-and-forget backfill — single-day not exposed via HTTP
  since it's cheap and local-only). 12 new HTTP tests.

### Test counts

339 fundamentals-domain + migration tests pass. Up from 295 by:
+ 16 fetcher tests
+ 11 runner tests
+ 5 CLI tests
+ 12 HTTP tests
+ (Plus the existing 295 still pass.)

### Operator's test of compute_market_cap_for_date today

The operator ran `compute_market_cap_for_date(eng, 'NSE',
date.today())` and got `skipped_no_close=2347` — every NSE EQ
ticker was skipped because no bar was found.

Most likely cause: bars for today haven't been ingested yet.
Markets close 15:30 IST; if the bars ingestion daemon runs after
EoD and the test was run before that, no bars exist. The simplest
diagnostic is the SQL provided in this session's exchange:

```sql
SELECT trade_date, COUNT(*) FROM (
    SELECT DATE(bar_timestamp) AS trade_date
    FROM ohlcv_bars
    WHERE timeframe = '1d'
      AND bar_timestamp >= NOW() - INTERVAL '7 days'
) sub
GROUP BY trade_date
ORDER BY trade_date DESC;
```

If today shows in the list, my SQL has a timezone bug and needs
investigation. If today is missing, the compute is correct and
the operator just needs to wait for bars ingestion (or run with
--date=yesterday).

### What's next

Phase 4A is functionally complete. The natural next step:
operator runs `compute-market-cap --backfill-from 2025-05-22`
to populate the 1-year backfill. Should take ~60-90 min (similar
shape to the other Phase 1-3 refreshes). After that, we have a
year of daily market cap data per active EQ ticker, ready for
Phase 4B (index construction).

### Phase 4B entry point

"start Phase 4B with the index construction layer" — covers:
- Migration for industry_index, industry_index_constituent,
  industry_index_value tables.
- Repos for each.
- select_top_n_constituents(industry, rebalance_date, n=10) —
  the top-N selection logic with the threshold=10 minimum.
- compute_index_value(index_id, date) — modified Laspeyres
  formula with drop-and-renormalize on mid-cycle delisting.
- CLI: build-industry-indices, compute-index-values.
- HTTP endpoints.
- Tests.
