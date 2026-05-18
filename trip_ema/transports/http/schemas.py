"""HTTP request/response schemas.

These wrap (or unwrap) the internal `api.responses` types into JSON-friendly
Pydantic models. We deliberately separate the wire format from the internal
domain types so each can evolve independently.

The mapping is intentionally thin — most fields pass through 1:1. Where
internal types use enums by value (e.g. ChangeSignal) we expose them as
human-readable strings on the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...api import TerseResponse, VerboseResponse
from ...domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    OHLCVBar,
    TickerRef,
    Timeframe,
)


# ============================================================
# Requests
# ============================================================


class OHLCVBarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_domain(self) -> OHLCVBar:
        # OHLCVBar requires tz-aware timestamps; Pydantic parses ISO 8601
        # strings into tz-aware datetimes when the input has a tz suffix.
        # If the caller sent a naive timestamp, OHLCVBar's __post_init__
        # will raise, which becomes a 422 via FastAPI's exception handler.
        return OHLCVBar(
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class ComputeRequest(BaseModel):
    """Body for POST /v1/compute — function (a)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str = Field(min_length=1, max_length=16)
    symbol: str = Field(min_length=1, max_length=32)
    instrument: str = Field(default="EQ", max_length=16)
    timeframe: Timeframe = Timeframe.WEEKLY
    verbose: bool = False
    history: list[OHLCVBarPayload] = Field(default_factory=list)
    latest_bar: OHLCVBarPayload

    def ticker_ref(self) -> TickerRef:
        return TickerRef(
            exchange=self.exchange,
            symbol=self.symbol,
            instrument=self.instrument,
        )


# ============================================================
# Responses
# ============================================================


class TickerRefPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    exchange: str
    symbol: str
    instrument: str

    @classmethod
    def from_domain(cls, ref: TickerRef) -> "TickerRefPayload":
        return cls(
            exchange=ref.exchange, symbol=ref.symbol, instrument=ref.instrument,
        )


class BarStatePayload(BaseModel):
    """Serialized BarState. NaN floats are converted to None on the wire
    because JSON doesn't support NaN."""
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    med_ema: float | None
    long_ema: float | None
    longest_ema: float | None
    atr: float | None
    upper_inner: float | None
    lower_inner: float | None
    upper_mid: float | None
    lower_mid: float | None
    upper_outer: float | None
    lower_outer: float | None
    pierces_upper_mid: bool
    pierces_lower_mid: bool
    core_bg: str
    display_bg: str

    @classmethod
    def from_domain(cls, s: BarState) -> "BarStatePayload":
        def f(x: float) -> float | None:
            return None if x != x else x  # NaN -> None
        return cls(
            timestamp=s.timestamp,
            med_ema=f(s.med_ema),
            long_ema=f(s.long_ema),
            longest_ema=f(s.longest_ema),
            atr=f(s.atr),
            upper_inner=f(s.upper_inner),
            lower_inner=f(s.lower_inner),
            upper_mid=f(s.upper_mid),
            lower_mid=f(s.lower_mid),
            upper_outer=f(s.upper_outer),
            lower_outer=f(s.lower_outer),
            pierces_upper_mid=s.pierces_upper_mid,
            pierces_lower_mid=s.pierces_lower_mid,
            core_bg=CoreRegime(s.core_bg).name,
            display_bg=CoreRegime(s.display_bg).name,
        )


class TerseResponsePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: TickerRefPayload
    timeframe: str
    latest_bar_timestamp: datetime
    latest_change: str
    latest_price: float
    computed_at: datetime

    @classmethod
    def from_domain(cls, r: TerseResponse) -> "TerseResponsePayload":
        return cls(
            ticker=TickerRefPayload.from_domain(r.ticker),
            timeframe=r.timeframe.value,
            latest_bar_timestamp=r.latest_bar_timestamp,
            latest_change=r.latest_change.name,
            latest_price=r.latest_price,
            computed_at=r.computed_at,
        )


class VerboseResponsePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: TickerRefPayload
    timeframe: str
    bars: list[BarStatePayload]
    latest_change: str
    computed_at: datetime

    @classmethod
    def from_domain(cls, r: VerboseResponse) -> "VerboseResponsePayload":
        return cls(
            ticker=TickerRefPayload.from_domain(r.ticker),
            timeframe=r.timeframe.value,
            bars=[BarStatePayload.from_domain(b) for b in r.bars],
            latest_change=r.latest_change.name,
            computed_at=r.computed_at,
        )


# ============================================================
# Admin / ops responses
# ============================================================


class ComputeRunSummaryPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    started_at: datetime
    finished_at: datetime | None
    tickers_examined: int
    tickers_with_new_bars: int
    bars_ingested: int
    events_fired: int
    errors: list[tuple[str, str]]


class LifecycleResultPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    tickers_primed: int
    tickers_skipped_no_bars: int
    tickers_failed: int
    cache_entries_populated: int


class HealthPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: str
    registry_size: int
    cache_size: int
    compute_scheduler_alive: bool | None = None
    distributor_alive: bool | None = None
    compute_last_completed_at: datetime | None = None
    distributor_last_completed_at: datetime | None = None
    redis_reachable: bool | None = None
    stale_tickers_count: int = 0
    """Phase 9 A2: number of tickers currently flagged as stale due to
    data gaps (ETL hasn't filled all expected closed weekly buckets).
    Reported on /readyz but does NOT contribute to 503 — the service
    is operationally healthy; it's upstream data that's behind. Ops
    monitoring should alert on this field; the load balancer should
    not depool on it."""
    issues: list[str] = []
    """List of human-readable problem descriptions. Empty when fully healthy.
    Populated even on 200 responses if there are minor concerns (e.g.,
    'compute_scheduler hasn't completed a run yet')."""


class ErrorPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    error: str
    detail: str | None = None


# ---------- Phase 9 A2: stale-tickers admin endpoint ---------- #


class StaleTickerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    ticker: TickerRefPayload
    timeframe: str
    stale_since: datetime
    last_good_bar_timestamp: datetime
    last_good_computed_at: datetime
    last_known_signal: str


class StaleTickersPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    count: int
    entries: list[StaleTickerEntry]


# ---------- reprime-ticker admin endpoint ---------- #


class RepriumeResultPayload(BaseModel):
    """Outcome of /v1/admin/reprime-ticker/{exchange}/{symbol}.

    `succeeded=True` means the engine was rebuilt and the cache was
    refreshed. `succeeded=False` means the ticker exists but has no
    bars in the DB; the engine was removed from the registry.
    """
    model_config = ConfigDict(frozen=True)
    ticker: TickerRefPayload
    timeframe: str
    succeeded: bool
    bars_loaded: int
    refreshed_metadata: bool
    notes: str


# ---------- circuit-breaker admin endpoint ---------- #


class CircuitBreakerConfigPayload(BaseModel):
    """Tunables in effect for a given breaker."""
    model_config = ConfigDict(frozen=True)
    failure_threshold: int
    reset_timeout_seconds: float
    half_open_max_successes: int


class CircuitBreakerEntry(BaseModel):
    """Runtime state of one transport's circuit breaker."""
    model_config = ConfigDict(frozen=True)
    name: str
    state: str  # "closed" | "open" | "half_open"
    consecutive_failures: int
    opened_at_monotonic: float | None
    """time.monotonic() at the last CLOSED→OPEN transition. The caller
    can subtract from time.monotonic() to compute 'how long has this
    been open?' Wall-clock time isn't recorded because the breaker uses
    monotonic time internally to be correct across NTP adjustments."""
    config: CircuitBreakerConfigPayload


class CircuitBreakersPayload(BaseModel):
    """Outcome of GET /v1/admin/circuit-breakers.

    `mode='legacy'` means the distributor is dispatching transports
    in-process, so we can read the breaker dict directly.
    `mode='queue'` means the breakers live in the separate notifier
    worker process; we can't inspect them from here. In that case
    `breakers` is empty and `notes` explains.
    `mode='disabled'` means circuit breakers are turned off in config
    (transports.circuit_breaker.enabled=False).
    """
    model_config = ConfigDict(frozen=True)
    mode: str
    breakers: list[CircuitBreakerEntry]
    snapshot_taken_at: datetime
    notes: str


# ---------- ETL admin endpoints ---------- #


class EtlActionAcceptedPayload(BaseModel):
    """Response from a POST that fires a background ETL action.
    `run_id` is the etl_runs row that was opened for this work — poll
    GET /v1/admin/etl/status to track progress."""
    model_config = ConfigDict(frozen=True)
    action: str
    """Which action was requested: 'refresh-listings', 'run-eod', etc."""
    run_id: int | None
    """The new etl_runs.run_id. None if no row was opened (e.g.,
    intraday skipped on a non-trading day)."""
    accepted_at: datetime
    notes: str


class EtlRunSummary(BaseModel):
    """One etl_runs row for the GUI's recent-runs table."""
    model_config = ConfigDict(frozen=True)
    run_id: int
    kind: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    tickers_attempted: int
    tickers_succeeded: int
    tickers_failed: int
    bars_written: int
    correlation_id: str | None
    notes: str | None


class EtlParkedTicker(BaseModel):
    """One parked-ticker row."""
    model_config = ConfigDict(frozen=True)
    ticker_id: int
    symbol: str
    parked_at: datetime | None
    parked_reason: str | None
    consecutive_failures: int
    last_error: str | None


class EtlTierCounts(BaseModel):
    """How many tickers per tier — drives the dashboard summary."""
    model_config = ConfigDict(frozen=True)
    tier_1: int
    tier_2: int
    tier_3: int


class EtlStatusPayload(BaseModel):
    """Response from GET /v1/admin/etl/status. The GUI polls this
    every 5s for the ETL Status tab."""
    model_config = ConfigDict(frozen=True)
    recent_runs: list[EtlRunSummary]
    parked_count: int
    tier_counts: EtlTierCounts
    snapshot_taken_at: datetime


class EtlParkedListPayload(BaseModel):
    """Response from GET /v1/admin/etl/parked."""
    model_config = ConfigDict(frozen=True)
    parked: list[EtlParkedTicker]
    total: int
    snapshot_taken_at: datetime


# ---------- Signal history (chart data) ---------- #


class HistoricalBarPayload(BaseModel):
    """One weekly bar with full indicator overlay state. Mirrors
    BarState plus the underlying OHLCV. The chart renders one row
    per entry in the response's `bars` list."""
    model_config = ConfigDict(frozen=True)
    bar_timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Indicator values
    med_ema: float | None
    long_ema: float | None
    longest_ema: float | None
    atr: float | None
    # Bands
    upper_inner: float | None
    lower_inner: float | None
    upper_mid: float | None
    lower_mid: float | None
    upper_outer: float | None
    lower_outer: float | None
    # Regime
    core_bg: str
    display_bg: str
    is_warmed_up: bool


class HistoricalSignalEventPayload(BaseModel):
    """One row from signal_events for the chart's marker layer."""
    model_config = ConfigDict(frozen=True)
    event_id: int
    bar_timestamp: datetime
    signal: str
    """'BUY' / 'SELL' / 'NO_CHANGE'."""
    price_at_signal: float


class SignalHistoryPayload(BaseModel):
    """Response from GET /v1/signals/{exchange}/{symbol}/history.

    The chart layer in the GUI is built primarily off this response:
      - `bars` → candles + EMA lines + ATR bands + regime fill (one
        polyline per indicator over the bar timestamps)
      - `signal_events` → BUY/SELL markers
      - `meta.warmup_bars_count` lets the GUI dim the warmup period
        on the chart if it wants

    `bars` are sorted oldest→newest. Times are UTC."""
    model_config = ConfigDict(frozen=True)
    exchange: str
    symbol: str
    instrument: str
    timeframe: str
    bars: list[HistoricalBarPayload]
    signal_events: list[HistoricalSignalEventPayload]
    warmup_bars_count: int
    """How many leading bars have NaN indicator values (Pine warmup).
    GUI may shade these or omit indicator overlays on them."""
    notes: str


# ---------- Subscribers (Slice 4) ---------- #


class SubscriptionFilterPayload(BaseModel):
    """One filter row attached to a subscriber. Either filters apply
    to ALL tickers (ticker_id=None, no symbol), or to a specific
    ticker (ticker_id required). signal_filter is a comma-separated
    list of regimes the subscriber wants (e.g. 'BUY,SELL'); NULL =
    all signals."""
    model_config = ConfigDict(frozen=True)
    subscription_id: int | None = None
    """None when posting a new filter; server-assigned on insert."""
    ticker_id: int | None = None
    ticker_symbol: str | None = None
    """Resolved server-side; clients send ticker_id. Read responses
    include both so the GUI can display the symbol without a separate
    lookup."""
    ticker_exchange: str | None = None
    signal_filter: str | None = None


class SubscriberPayload(BaseModel):
    """Outbound representation of a subscriber row + its filters."""
    model_config = ConfigDict(frozen=True)
    subscriber_id: int
    transport: str
    """'email' | 'webhook' | 'slack' | 'sms'."""
    address: str
    notify_cadence_minutes: int
    is_active: bool
    last_notified_at: datetime | None
    created_at: datetime
    filters: list[SubscriptionFilterPayload]


class SubscriberCreatePayload(BaseModel):
    """Inbound for POST /v1/admin/subscribers. transport/address/
    cadence are required; filters are optional and default to a
    single 'all tickers, all signals' filter."""
    model_config = ConfigDict(frozen=True)
    transport: str = Field(pattern=r"^(email|webhook|slack|sms)$")
    address: str = Field(min_length=1, max_length=512)
    notify_cadence_minutes: int = Field(ge=15, le=1440)
    is_active: bool = True
    filters: list[SubscriptionFilterPayload] | None = None


class SubscriberUpdatePayload(BaseModel):
    """Inbound for PATCH /v1/admin/subscribers/{id}. All fields
    optional; only supplied ones change."""
    model_config = ConfigDict(frozen=True)
    transport: str | None = Field(default=None, pattern=r"^(email|webhook|slack|sms)$")
    address: str | None = Field(default=None, min_length=1, max_length=512)
    notify_cadence_minutes: int | None = Field(default=None, ge=15, le=1440)
    is_active: bool | None = None


class SubscribersListPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    subscribers: list[SubscriberPayload]
    total: int


class TestNotificationResultPayload(BaseModel):
    """Outcome of POST /v1/admin/subscribers/{id}/test-notification.
    The transport's send() returns bool; we surface it plus any
    error message captured."""
    model_config = ConfigDict(frozen=True)
    subscriber_id: int
    transport: str
    address: str
    succeeded: bool
    sent_at: datetime
    notes: str


# ---------- Notification log (Slice 4) ---------- #


class NotificationLogEntry(BaseModel):
    """One row from notification_log joined with subscribers + events
    + tickers for human-readable display."""
    model_config = ConfigDict(frozen=True)
    log_id: int
    last_sent_at: datetime
    subscriber_id: int
    subscriber_address: str
    subscriber_transport: str
    event_id: int
    event_signal: str
    event_bar_timestamp: datetime
    ticker_symbol: str | None
    ticker_exchange: str | None
    notification_count: int


class NotificationLogPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    entries: list[NotificationLogEntry]
    total: int
    has_more: bool
    snapshot_taken_at: datetime


# ---------- Notifier DLQ (Phase 12) ---------- #


class DlqEntry(BaseModel):
    """One entry in the notifier worker's dead-letter queue.

    Surfaces the raw payload plus the metadata Phase 12 added
    (`_dlq_reason`, `_dlq_at`, `_attempts`). The GUI's DLQ tab
    renders this for operator review and replay decisions.
    """
    model_config = ConfigDict(frozen=True)
    job_id: str
    transport: str
    subscriber_id: int | None = None
    event_id: int | None = None
    attempts: int
    failed_at: datetime
    reason: str
    raw_payload: dict


class DlqListPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    entries: list[DlqEntry]
    total: int
    snapshot_taken_at: datetime


class DlqActionResult(BaseModel):
    """Result of a replay or delete operation."""
    model_config = ConfigDict(frozen=True)
    job_id: str
    action: str   # "replay" | "delete"
    success: bool
    message: str | None = None


# ---------- System health snapshot (Slice 4) ---------- #


class HealthCardPayload(BaseModel):
    """One card on the health dashboard. status is 'ok' | 'warn' |
    'crit' | 'unknown'; the GUI renders the color from that."""
    model_config = ConfigDict(frozen=True)
    name: str
    status: str
    headline: str
    """Short, one-liner: '2300 active tickers'."""
    detail: str | None = None
    """Longer prose: '7 parked, 2 stale'. Optional."""


class HealthSnapshotPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    overall_status: str
    """Worst of all card statuses. 'ok' / 'warn' / 'crit' / 'unknown'."""
    cards: list[HealthCardPayload]
    snapshot_taken_at: datetime
