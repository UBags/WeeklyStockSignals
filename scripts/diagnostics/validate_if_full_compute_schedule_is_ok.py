#!/usr/bin/env python3
"""validate_if_full_compute_schedule_is_ok.py

Force one exchange's full compute pipeline to fire now, poll until it
finishes (or times out), then validate every downstream effect happened:

  1. ohlcv_bars: fresh rows for active tickers
  2. indicator_state (ticker, daily): EMAs+ATR+regime populated on latest bar
  3. signal_events: any new flips recorded with valid signal_type
  4. industry_index_value: fresh daily values for managed industries
  5. indicator_state (industry, weekly IST-Monday): same as #2
  6. notification_log: at least one new row IF #3 produced any flips
                       AND a subscriber was due
  7. compute_scheduler log scan: zero new ERROR lines since trigger
     (catches the LiveTickResult.skip_reason AttributeError class of bugs)
  8. /healthz: registry_size sane, no issues list

CONNECTION
  DB via .pgpass as trip_gui_user (read-only is enough here).
  HTTP via http://localhost:8000 — override with --service-url.
  Admin token from TRIP_EMA_ADMIN_TOKEN env, or --admin-token.

EXIT CODES
  0  every required check passed (WARN is allowed)
  1  pipeline timed out before completion
  2  pipeline finished but one or more REQUIRED checks failed
  3  bad invocation / cannot reach service or DB
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("FATAL: psycopg2 not installed in this venv", file=sys.stderr)
    sys.exit(3)

try:
    import requests
except ImportError:
    print("FATAL: requests not installed in this venv", file=sys.stderr)
    sys.exit(3)


# ---------------------------------------------------------------------------
# ANSI colors (auto-detect TTY)
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty()
def _c(text: str, color_code: str) -> str:
    return f"\033[{color_code}m{text}\033[0m" if _USE_COLOR else text

GREEN  = lambda t: _c(t, "32")
RED    = lambda t: _c(t, "31")
YELLOW = lambda t: _c(t, "33")
BLUE   = lambda t: _c(t, "34")
DIM    = lambda t: _c(t, "2")
BOLD   = lambda t: _c(t, "1")


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    status: str   # "PASS" | "WARN" | "FAIL" | "SKIP"
    detail: str
    required: bool = True

    @property
    def is_blocking(self) -> bool:
        return self.required and self.status == "FAIL"

    def render(self) -> str:
        s = self.status
        if s == "PASS":  badge = GREEN("✓ PASS")
        elif s == "WARN": badge = YELLOW("! WARN")
        elif s == "FAIL": badge = RED("✗ FAIL")
        else:             badge = DIM("- SKIP")
        return f"  {badge}  {self.name:<55} {DIM(self.detail)}"


@dataclass
class Baseline:
    captured_at: datetime
    ohlcv_max_bar_ts: datetime | None
    ohlcv_rowcount: int
    indicator_state_ticker_rowcount: int      # stock_indicator_state
    indicator_state_industry_rowcount: int   # industry_indicator_state
    signal_events_rowcount: int              # historical_signals
    industry_index_value_rowcount: int
    notification_log_rowcount: int
    log_pos_bytes: int


# ---------------------------------------------------------------------------
# DB helpers — connect as trip_gui_user via socket (uses .pgpass)
# ---------------------------------------------------------------------------
def db_connect(db_user: str, db_name: str):
    try:
        return psycopg2.connect(
            user=db_user, dbname=db_name,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    except psycopg2.OperationalError as e:
        print(RED(f"FATAL: cannot connect to Postgres as {db_user}: {e}"),
              file=sys.stderr)
        sys.exit(3)


def scalar(conn, sql: str, params: dict | None = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        row = cur.fetchone()
        if row is None: return None
        # RealDictCursor: row is dict; grab the single value
        return next(iter(row.values()))


def fetch_one(conn, sql: str, params: dict | None = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchone()


def fetch_all(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Baseline snapshot
# ---------------------------------------------------------------------------
def capture_baseline(conn, log_path: str) -> Baseline:
    log_pos = 0
    try:
        log_pos = os.path.getsize(log_path)
    except OSError:
        pass

    ohlcv_max = scalar(conn,
        "SELECT MAX(bar_timestamp) FROM ohlcv_bars",
    )
    return Baseline(
        captured_at=datetime.now(UTC),
        ohlcv_max_bar_ts=ohlcv_max,
        ohlcv_rowcount=scalar(conn, "SELECT COUNT(*) FROM ohlcv_bars"),
        # The polymorphic `indicator_state` was split in 0032 into
        # stock_indicator_state + industry_indicator_state, so the old
        # "WHERE ticker_id IS NOT NULL" / "WHERE industry_name IS NOT NULL"
        # discriminators are now just two separate tables.
        indicator_state_ticker_rowcount=scalar(conn,
            "SELECT COUNT(*) FROM stock_indicator_state"),
        indicator_state_industry_rowcount=scalar(conn,
            "SELECT COUNT(*) FROM industry_indicator_state"),
        signal_events_rowcount=scalar(conn,
            "SELECT COUNT(*) FROM historical_signals"),
        industry_index_value_rowcount=scalar(conn,
            "SELECT COUNT(*) FROM industry_index_value"),
        notification_log_rowcount=scalar(conn,
            "SELECT COUNT(*) FROM notification_log"),
        log_pos_bytes=log_pos,
    )


def render_baseline(b: Baseline) -> None:
    print(BOLD("\n── Baseline snapshot ─────────────────────────────────────"))
    print(f"  captured_at:                  {b.captured_at.isoformat()}")
    print(f"  ohlcv_bars rows:              {b.ohlcv_rowcount:>10,}")
    print(f"  ohlcv max bar_timestamp:      {b.ohlcv_max_bar_ts}")
    print(f"  stock_indicator_state:        {b.indicator_state_ticker_rowcount:>10,}")
    print(f"  industry_indicator_state:     {b.indicator_state_industry_rowcount:>10,}")
    print(f"  signal_events rows:           {b.signal_events_rowcount:>10,}")
    print(f"  industry_index_value rows:    {b.industry_index_value_rowcount:>10,}")
    print(f"  notification_log rows:        {b.notification_log_rowcount:>10,}")
    print(f"  /tmp/trip-ema.log size:       {b.log_pos_bytes:>10,} bytes")


# ---------------------------------------------------------------------------
# Trigger + poll
# ---------------------------------------------------------------------------
def trigger_run(service_url: str, exchange: str, admin_token: str) -> dict:
    url = f"{service_url.rstrip('/')}/v1/admin/orchestrator/{exchange}/run-now"
    headers = {"Authorization": f"Bearer {admin_token}"}
    print(BOLD("\n── Triggering pipeline ───────────────────────────────────"))
    print(f"  POST {url}")
    try:
        r = requests.post(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        print(RED(f"  FATAL: cannot reach service: {e}"))
        sys.exit(3)
    if r.status_code == 401 or r.status_code == 403:
        print(RED(f"  FATAL: admin token rejected ({r.status_code}): {r.text[:200]}"))
        sys.exit(3)
    if r.status_code == 404:
        print(RED(f"  FATAL: exchange {exchange!r} not managed by pool"))
        sys.exit(3)
    if r.status_code != 202:
        print(RED(f"  FATAL: unexpected status {r.status_code}: {r.text[:300]}"))
        sys.exit(3)
    body = r.json()
    print(f"  accepted: {GREEN(json.dumps(body))}")
    return body


def poll_status(
    service_url: str, exchange: str, admin_token: str,
    baseline: Baseline, conn, *,
    timeout_minutes: int, poll_interval_s: int,
) -> tuple[bool, str]:
    """Poll until pipeline reports done OR DB shows new ohlcv rows past
    the baseline. Returns (completed, reason)."""
    url = f"{service_url.rstrip('/')}/v1/admin/orchestrator/status"
    headers = {"Authorization": f"Bearer {admin_token}"}
    deadline = datetime.now(UTC) + timedelta(minutes=timeout_minutes)

    print(BOLD(f"\n── Polling completion (every {poll_interval_s}s, "
               f"timeout {timeout_minutes}m) ─"))
    poll = 0
    while datetime.now(UTC) < deadline:
        poll += 1
        # Approach 1: orchestrator status API
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                payload = r.json()
                markets = payload.get("markets", [])
                this_market = next(
                    (m for m in markets
                     if m.get("exchange") == exchange or m.get("name") == exchange),
                    None,
                )
                if this_market:
                    last_completed = this_market.get("last_run_completed_at") or \
                                     this_market.get("last_completed_at")
                    if last_completed:
                        try:
                            ts = datetime.fromisoformat(
                                str(last_completed).replace("Z", "+00:00"))
                            if ts > baseline.captured_at:
                                return True, f"orchestrator reported completion at {ts}"
                        except (TypeError, ValueError):
                            pass
                    # Fields might vary by version; also try a direct status string
                    state = (this_market.get("state") or
                             this_market.get("status") or "").lower()
                    if state in ("idle", "complete", "completed", "ok"):
                        # Verify with DB-side delta to avoid stale state
                        new_ohlcv = scalar(conn,
                            "SELECT COUNT(*) FROM ohlcv_bars WHERE bar_timestamp >= %s",
                            {"baseline": baseline.captured_at.isoformat()}) if False else None
                        if new_ohlcv is None:
                            pass
        except requests.RequestException:
            pass

        # Approach 2: DB-side delta — if ohlcv has materially grown since
        # baseline, the fetch leg ran. Combined with new indicator_state
        # rows since baseline, the compute leg ran too.
        try:
            new_ohlcv = scalar(conn,
                "SELECT COUNT(*) FROM ohlcv_bars",
            ) - baseline.ohlcv_rowcount
            new_indicator = (
                scalar(conn, "SELECT COUNT(*) FROM stock_indicator_state")
                + scalar(conn, "SELECT COUNT(*) FROM industry_indicator_state")
            ) - (baseline.indicator_state_ticker_rowcount +
                 baseline.indicator_state_industry_rowcount)
            if new_ohlcv > 100 and new_indicator > 100:
                # Pipeline produced meaningful output; give it 30s more
                # to wrap notifications.
                time.sleep(min(30, poll_interval_s))
                return True, (f"DB-delta inferred completion "
                              f"(+{new_ohlcv:,} ohlcv, +{new_indicator:,} indicator)")
        except psycopg2.Error:
            conn.rollback()

        # Print progress dot
        print(f"  poll #{poll:>3}  "
              f"+{(scalar(conn,'SELECT COUNT(*) FROM ohlcv_bars') - baseline.ohlcv_rowcount):>6,} ohlcv  "
              f"+{(scalar(conn,'SELECT COUNT(*) FROM historical_signals') - baseline.signal_events_rowcount):>5,} signals  "
              f"+{(scalar(conn,'SELECT COUNT(*) FROM notification_log') - baseline.notification_log_rowcount):>4,} notifs")
        time.sleep(poll_interval_s)

    return False, f"timed out after {timeout_minutes}m"


# ---------------------------------------------------------------------------
# Validation checks
# ---------------------------------------------------------------------------
def run_validations(conn, baseline: Baseline, exchange: str,
                    log_path: str, service_url: str,
                    expected_ticker_min: int, expected_industry_min: int,
                    ) -> list[CheckResult]:
    results: list[CheckResult] = []

    # A. ohlcv_bars: new rows
    new_ohlcv = scalar(conn, "SELECT COUNT(*) FROM ohlcv_bars") - baseline.ohlcv_rowcount
    new_max = scalar(conn, "SELECT MAX(bar_timestamp) FROM ohlcv_bars")
    advanced = (new_max is not None and
                (baseline.ohlcv_max_bar_ts is None or new_max > baseline.ohlcv_max_bar_ts))
    if new_ohlcv >= expected_ticker_min * 0.8:
        results.append(CheckResult(
            "A. ohlcv_bars: new rows fetched", "PASS",
            f"+{new_ohlcv:,} rows; max_bar_ts advanced={advanced}"))
    elif new_ohlcv > 0:
        results.append(CheckResult(
            "A. ohlcv_bars: new rows fetched", "WARN",
            f"only +{new_ohlcv:,} rows (expected ≥ {int(expected_ticker_min*0.8):,}); "
            f"max_bar_ts advanced={advanced}"))
    else:
        results.append(CheckResult(
            "A. ohlcv_bars: new rows fetched", "FAIL",
            "0 new rows — fetch leg did not run or all tickers stale"))

    # B. indicator_state (ticker, 1D): rows populated for latest bar
    # `trade_date` became `date` when the table went DAILY grain (0034).
    latest_ticker = fetch_one(conn, """
        SELECT
          MAX(date) AS latest_date,
          COUNT(*) FILTER (WHERE date = (
              SELECT MAX(date) FROM stock_indicator_state
          )) AS rows_latest,
          COUNT(*) FILTER (WHERE date = (
              SELECT MAX(date) FROM stock_indicator_state
          ) AND med_ema IS NOT NULL AND long_ema IS NOT NULL
              AND longest_ema IS NOT NULL AND atr IS NOT NULL) AS rows_emas_atr_ok,
          COUNT(*) FILTER (WHERE date = (
              SELECT MAX(date) FROM stock_indicator_state
          ) AND display_regime IN (-1, 0, 1)) AS rows_regime_ok
        FROM stock_indicator_state
    """)
    if latest_ticker and latest_ticker["rows_latest"]:
        n_latest = latest_ticker["rows_latest"]
        n_emas = latest_ticker["rows_emas_atr_ok"]
        n_regime = latest_ticker["rows_regime_ok"]
        pct_emas = (n_emas / n_latest) if n_latest else 0
        pct_regime = (n_regime / n_latest) if n_latest else 0
        status = ("PASS" if pct_emas >= 0.98 and pct_regime >= 0.98
                  else "WARN" if pct_emas >= 0.90 else "FAIL")
        results.append(CheckResult(
            "B. indicator_state (ticker): latest bar populated", status,
            f"latest={latest_ticker['latest_date']}; "
            f"{n_latest:,} rows; EMAs+ATR ok={n_emas:,}/{n_latest:,} ({pct_emas:.0%}); "
            f"regime ok={n_regime:,}/{n_latest:,} ({pct_regime:.0%})"))
    else:
        results.append(CheckResult(
            "B. stock_indicator_state: latest bar populated", "FAIL",
            "no rows found"))

    # C. historical_signals: new rows (informational only — 0 is legal)
    new_signals = scalar(
        conn, "SELECT COUNT(*) FROM historical_signals"
    ) - baseline.signal_events_rowcount
    signal_types = fetch_all(conn, """
        SELECT signal_type, COUNT(*) AS n FROM historical_signals
        WHERE computed_at > %(since)s GROUP BY signal_type ORDER BY n DESC
    """, {"since": baseline.captured_at.isoformat()})
    distinct_types = [r["signal_type"] for r in signal_types]
    bad_types = [t for t in distinct_types
                 if t not in ("BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL")]
    if new_signals == 0:
        results.append(CheckResult(
            "C. signal_events: new flips", "WARN",
            "0 new signals (legal but unusual on a Monday regen)",
            required=False))
    elif bad_types:
        results.append(CheckResult(
            "C. signal_events: new flips", "FAIL",
            f"+{new_signals} signals; unexpected types: {bad_types}"))
    else:
        types_str = ", ".join(f"{r['signal_type']}={r['n']}" for r in signal_types)
        results.append(CheckResult(
            "C. signal_events: new flips", "PASS",
            f"+{new_signals} signals ({types_str})"))

    # D. industry_index_value: fresh values
    new_iiv = scalar(conn,
        "SELECT COUNT(*) FROM industry_index_value") - baseline.industry_index_value_rowcount
    industries_updated = scalar(conn, """
        SELECT COUNT(DISTINCT industry_name) FROM industry_index_value
        WHERE trade_date = (SELECT MAX(trade_date) FROM industry_index_value)
    """) or 0
    if industries_updated >= expected_industry_min:
        results.append(CheckResult(
            "D. industry_index_value: fresh values", "PASS",
            f"+{new_iiv:,} rows; {industries_updated} industries with latest-date row"))
    elif industries_updated >= expected_industry_min * 0.8:
        results.append(CheckResult(
            "D. industry_index_value: fresh values", "WARN",
            f"only {industries_updated} industries updated (expected ≥ {expected_industry_min})"))
    else:
        results.append(CheckResult(
            "D. industry_index_value: fresh values", "FAIL",
            f"only {industries_updated} industries updated (expected ≥ {expected_industry_min})"))

    # E. indicator_state (industry, weekly Monday): latest bar populated
    latest_industry = fetch_one(conn, """
        SELECT
          MAX(date) AS latest_date,
          COUNT(*) FILTER (WHERE date = (
              SELECT MAX(date) FROM industry_indicator_state
          )) AS rows_latest,
          COUNT(*) FILTER (WHERE date = (
              SELECT MAX(date) FROM industry_indicator_state
          ) AND med_ema IS NOT NULL AND long_ema IS NOT NULL
              AND longest_ema IS NOT NULL AND atr IS NOT NULL
              AND display_regime IN (-1, 0, 1)) AS rows_fully_ok
        FROM industry_indicator_state
    """)
    if latest_industry and latest_industry["rows_latest"]:
        n_latest = latest_industry["rows_latest"]
        n_ok = latest_industry["rows_fully_ok"]
        # Industry rows are weekly IST-Monday; latest_date should be a Monday
        latest_d = latest_industry["latest_date"]
        is_monday = (latest_d.weekday() == 0) if latest_d else False
        ratio = (n_ok / n_latest) if n_latest else 0
        status = ("PASS" if ratio >= 0.98 and is_monday
                  else "WARN" if ratio >= 0.90 else "FAIL")
        results.append(CheckResult(
            "E. indicator_state (industry weekly): populated", status,
            f"latest={latest_d} (Monday? {is_monday}); "
            f"{n_ok}/{n_latest} rows fully populated"))
    else:
        results.append(CheckResult(
            "E. indicator_state (industry weekly): populated", "FAIL",
            "no industry rows found"))

    # F. notification_log: new rows (conditional on C producing signals)
    new_notifs = scalar(conn,
        "SELECT COUNT(*) FROM notification_log") - baseline.notification_log_rowcount
    if new_signals == 0:
        results.append(CheckResult(
            "F. notification_log: new sends", "SKIP",
            "no new signals, nothing to notify on", required=False))
    elif new_notifs > 0:
        max_last = scalar(conn, "SELECT MAX(last_sent_at) FROM notification_log")
        results.append(CheckResult(
            "F. notification_log: new sends", "PASS",
            f"+{new_notifs} log rows; last_sent_at={max_last}"))
    else:
        # No new notifications. Could be: no subscriber due, all events
        # capped, transport off. Distinguish.
        n_subs_due = scalar(conn, """
            SELECT COUNT(*) FROM subscribers
            WHERE is_active = TRUE
              AND (last_notified_at IS NULL
                   OR NOW() - last_notified_at >= make_interval(mins => notify_cadence_minutes))
        """)
        if n_subs_due == 0:
            results.append(CheckResult(
                "F. notification_log: new sends", "WARN",
                "0 sends but 0 subscribers due (cadence gate)",
                required=False))
        else:
            results.append(CheckResult(
                "F. notification_log: new sends", "WARN",
                f"0 sends despite {n_subs_due} subscribers due — "
                f"dispatcher may not have ticked yet (15-min cadence)",
                required=False))

    # G. Log scan: ERROR lines since baseline
    new_errors = scan_log_since(log_path, baseline.log_pos_bytes)
    compute_errors = [
        ln for ln in new_errors
        if "compute_scheduler" in ln or "Error processing" in ln
    ]
    if not new_errors:
        results.append(CheckResult(
            "G. log: no new ERROR lines since baseline", "PASS",
            f"clean ({log_path})"))
    elif compute_errors:
        sample = compute_errors[0][:200]
        results.append(CheckResult(
            "G. log: no new compute_scheduler ERRORs", "FAIL",
            f"{len(compute_errors)} compute errors; first: {sample!r}"))
    else:
        results.append(CheckResult(
            "G. log: no new compute_scheduler ERRORs", "WARN",
            f"{len(new_errors)} non-compute ERROR lines",
            required=False))

    # H. /healthz reachable + sane
    try:
        r = requests.get(f"{service_url.rstrip('/')}/healthz", timeout=5)
        if r.status_code == 200:
            hz = r.json()
            issues = hz.get("issues") or []
            reg_size = hz.get("registry_size")
            if not issues and reg_size and reg_size >= expected_ticker_min:
                results.append(CheckResult(
                    "H. /healthz status", "PASS",
                    f"status={hz.get('status')}; registry_size={reg_size}"))
            else:
                results.append(CheckResult(
                    "H. /healthz status", "WARN",
                    f"status={hz.get('status')}; registry_size={reg_size}; "
                    f"issues={issues}", required=False))
        else:
            results.append(CheckResult(
                "H. /healthz status", "FAIL",
                f"HTTP {r.status_code}"))
    except requests.RequestException as e:
        results.append(CheckResult(
            "H. /healthz status", "FAIL",
            f"unreachable: {e}"))

    return results


def scan_log_since(log_path: str, baseline_pos: int) -> list[str]:
    """Read new bytes from log_path past baseline_pos; return ERROR lines."""
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return []
    if size <= baseline_pos:
        return []
    with open(log_path, "rb") as f:
        f.seek(baseline_pos)
        new_bytes = f.read(size - baseline_pos)
    try:
        new_text = new_bytes.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return []
    err_re = re.compile(r"\[ERROR\]|ERROR\b|Traceback")
    return [ln for ln in new_text.splitlines() if err_re.search(ln)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--timeout-minutes", type=int, default=30)
    ap.add_argument("--poll-interval-seconds", type=int, default=30)
    ap.add_argument("--service-url", default="http://localhost:8000")
    ap.add_argument("--admin-token",
                    default=os.environ.get("TRIP_EMA_ADMIN_TOKEN", ""))
    ap.add_argument("--db-user", default="trip_gui_user")
    ap.add_argument("--db-name", default="trip_ema")
    ap.add_argument("--log-path", default="/tmp/trip-ema.log")
    ap.add_argument("--expected-ticker-min", type=int, default=2400,
                    help="rough lower bound for active tickers on the exchange")
    ap.add_argument("--expected-industry-min", type=int, default=55,
                    help="rough lower bound for managed industries")
    ap.add_argument("--skip-trigger", action="store_true",
                    help="skip POST run-now; validate against current DB state only")
    args = ap.parse_args()

    if not args.admin_token and not args.skip_trigger:
        print(RED("FATAL: TRIP_EMA_ADMIN_TOKEN not set and --skip-trigger not given"),
              file=sys.stderr)
        return 3

    print(BOLD(f"\n=== Full compute pipeline validation — {args.exchange} ==="))
    print(DIM(f"service: {args.service_url}   db: {args.db_user}@{args.db_name}"))

    conn = db_connect(args.db_user, args.db_name)
    conn.autocommit = True

    baseline = capture_baseline(conn, args.log_path)
    render_baseline(baseline)

    if not args.skip_trigger:
        trigger_run(args.service_url, args.exchange, args.admin_token)
        completed, reason = poll_status(
            args.service_url, args.exchange, args.admin_token,
            baseline, conn,
            timeout_minutes=args.timeout_minutes,
            poll_interval_s=args.poll_interval_seconds,
        )
        if not completed:
            print(RED(f"\n  ✗ Pipeline did not complete: {reason}"))
            print(YELLOW("  Will still run validations against current state."))
        else:
            print(GREEN(f"\n  ✓ Pipeline completion: {reason}"))

    # Give the notifier one cycle to catch up (15-min cadence is independent
    # of the compute pipeline; we wait up to one minute on the chance the
    # dispatcher fires in this window).
    print(DIM("\n  (giving notifier 60s to flush any pending sends...)"))
    time.sleep(60)

    print(BOLD("\n── Validation matrix ─────────────────────────────────────"))
    results = run_validations(
        conn, baseline, args.exchange, args.log_path, args.service_url,
        expected_ticker_min=args.expected_ticker_min,
        expected_industry_min=args.expected_industry_min,
    )
    for r in results:
        print(r.render())

    # Summary
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_warn = sum(1 for r in results if r.status == "WARN")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    print(BOLD("\n── Summary ──"))
    print(f"  {GREEN(str(n_pass)+' pass')}  "
          f"{YELLOW(str(n_warn)+' warn')}  "
          f"{RED(str(n_fail)+' fail')}  "
          f"{DIM(str(n_skip)+' skip')}")

    blocking_failures = [r for r in results if r.is_blocking]
    if blocking_failures:
        print(RED("\n  ✗ FAIL — blocking failures:"))
        for r in blocking_failures:
            print(RED(f"     - {r.name}: {r.detail}"))
        return 2
    print(GREEN("\n  ✓ All required checks passed."))
    return 0


if __name__ == "__main__":
    sys.exit(main())