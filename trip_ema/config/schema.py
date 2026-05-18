"""Config schemas — Pydantic models validating the YAML file structure.

The shape here mirrors the YAML one-to-one. The loader.py module reads the
file, resolves environment-variable references for secrets, and validates
into these types. Downstream code only ever sees validated instances.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain import IndicatorParams, Timeframe


# ---------- Database ---------- #


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    driver: Literal["postgresql"] = "postgresql"
    host: str
    port: int = 5432
    database: str
    user: str
    password_env: str = Field(
        description="Name of the env var containing the DB password. "
                    "Never put the password in the file itself."
    )
    tickers_table: str = "tickers"
    bars_table: str = "ohlcv_bars"

    # Connection pool. Defaults are SQLAlchemy's, surfaced here so they
    # can be tuned without code change. At current scale (single-thread
    # scheduler, ~10 notifier workers, occasional HTTP), pool_size=5 +
    # overflow=10 gives 15 concurrent connections at peak — comfortable.
    pool_size: int = Field(default=5, ge=1)
    pool_max_overflow: int = Field(default=10, ge=0)
    pool_timeout_seconds: int = Field(
        default=30, ge=1,
        description="How long to wait for a free connection before giving up.",
    )
    pool_recycle_seconds: int = Field(
        default=3600, ge=-1,
        description=(
            "Recycle connections older than this. -1 disables. The default "
            "of 3600s protects against managed-DB load balancers that close "
            "idle connections silently after ~30-60 minutes."
        ),
    )

    # Phase 12 — per-process least-privilege roles (optional).
    # When unset, all processes use the user/password above (back-compat).
    # When set, the listed roles override the default per process; see
    # alembic_pkg/versions/0002_db_roles.py for the GRANT model.
    roles: "DbRolesConfig | None" = Field(
        default=None,
        description="Optional per-process DB credentials. When None, all "
                    "processes use the top-level user/password. When set, "
                    "ETL/Engine/GUI processes use their respective roles.",
    )

    def url_without_password(self) -> str:
        """SQLAlchemy URL with password placeholder — used for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"


# ---------- DB role separation (Phase 12) ---------- #


class DbRoleCredentials(BaseModel):
    """Per-role DB user + password env var. host/port/database come
    from the parent DatabaseConfig — the roles all point at the same
    DB, just with different privileges.

    Why a separate model instead of three top-level fields?
    Per-role connection-pool tuning will likely diverge (the ETL
    role bursts heavily; the GUI role barely uses connections), so
    we leave room to add per-role pool overrides without restructuring.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str
    password_env: str


class DbRolesConfig(BaseModel):
    """Optional per-process DB credentials. Each role is independently
    optional; when a role is None, the process falls back to the
    top-level user/password.

    Typical production setup once all three are configured:

        database:
          host: db.example.com
          database: trip_ema
          user: trip_ema_app         # legacy / fallback
          password_env: TRIP_EMA_DB_PASSWORD
          roles:
            etl:
              user: trip_etl_user
              password_env: TRIP_EMA_DB_ETL_PASSWORD
            engine:
              user: trip_engine_user
              password_env: TRIP_EMA_DB_ENGINE_PASSWORD
            gui:
              user: trip_gui_user
              password_env: TRIP_EMA_DB_GUI_PASSWORD

    Migration ordering matters: alembic upgrade head should run as
    the top-level user (which has full privileges); the per-role
    users don't have privileges to alter schema. So whichever process
    runs migrations at startup keeps using the top-level credentials.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    etl: DbRoleCredentials | None = None
    engine: DbRoleCredentials | None = None
    gui: DbRoleCredentials | None = None


# ---------- Indicator (EMA + ATR) ---------- #


class EMAStageConfig(BaseModel):
    """One of the three EMA stage configs (med / long / longest)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    base: int = Field(ge=2)
    s1: int = Field(ge=2)
    s2: int = Field(ge=2)
    final: int | None = Field(default=None)

    @field_validator("final")
    @classmethod
    def _final_in_range(cls, v: int | None) -> int | None:
        if v is not None and v < 2:
            raise ValueError("EMA final length must be >= 2 or null")
        return v


class EMAConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    med: EMAStageConfig
    long: EMAStageConfig
    longest: EMAStageConfig
    volume_sma_length: int = Field(
        default=2, ge=1, le=20,
        description="Length of the SMA applied to raw volume before "
                    "feeding it to the VW-EMA. Pine's Weekly_Triple_EMA "
                    "uses 2. Set to 1 for raw (unsmoothed) volume.",
    )


class ATRConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    length: int = Field(ge=1, default=14)
    inner_mult: float = Field(gt=0, default=0.2)
    mid_mult: float = Field(gt=0, default=2.0)
    outer_mult: float = Field(gt=0, default=4.0)


# ---------- Market hours ---------- #


class MarketHoursConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str
    open: str = Field(description="HH:MM in the exchange's timezone")
    close: str = Field(description="HH:MM in the exchange's timezone")
    tz: str = Field(description="IANA timezone, e.g. 'Asia/Kolkata'")
    weekdays: list[int] = Field(
        description="ISO weekdays open: 1=Monday..7=Sunday",
    )

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("at least one weekday required")
        if any(d < 1 or d > 7 for d in v):
            raise ValueError("weekdays must be 1..7")
        return sorted(set(v))


# ---------- Scheduler / scanning ---------- #


class SchedulingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    compute_cadence_minutes: int = Field(
        default=60, ge=15,
        description="How often the ComputeScheduler runs. Wall-clock anchored.",
    )
    subscriber_cadence_minutes: int = Field(
        default=15, ge=15,
        description="How often the SubscriberDistributor wakes. "
                    "Must be a divisor of every subscriber's cadence.",
    )
    default_timeframe: Timeframe = Timeframe.WEEKLY


# ---------- Cache ---------- #


class CacheConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_history_bars: int = Field(default=1000, ge=10)
    state_history_retain: int = Field(default=200, ge=1)


# ---------- HTTP ---------- #


class HttpConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8000
    admin_token_env: str = Field(
        default="TRIP_EMA_ADMIN_TOKEN",
        description="Name of env var holding the bearer token for admin "
                    "endpoints. If the env var is unset, admin endpoints "
                    "return 503.",
    )
    notifier_subprocess_shutdown_timeout_seconds: int = Field(
        default=15, ge=1,
        description=(
            "On graceful shutdown, how long to wait for the notifier "
            "subprocess to exit cleanly after SIGTERM before escalating "
            "to SIGKILL. Should be smaller than uvicorn's "
            "--timeout-graceful-shutdown (default 30s) by enough margin "
            "that the rest of the shutdown sequence (db.dispose, etc.) "
            "can complete. Bump if notifier dispatch can legitimately "
            "take longer than 15s per job (e.g., synchronous SMTP with "
            "a slow MX server)."
        ),
    )


class TracingConfig(BaseModel):
    """OpenTelemetry tracing settings. Tracing is off by default — set
    `enabled=True` and pick an exporter to turn it on."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    service_name: str = "trip_ema"
    exporter: Literal["console", "otlp"] = "console"
    """'console' prints spans to stderr (dev). 'otlp' sends to an
    OpenTelemetry collector via gRPC; requires `otlp_endpoint` and
    the `opentelemetry-exporter-otlp` package."""

    otlp_endpoint: str | None = None
    """e.g. 'http://otel-collector:4317'. Required when exporter='otlp'."""


class ObservabilityConfig(BaseModel):
    """Observability settings — logging, metrics, tracing.

    Renamed/regrouped from the earlier HttpConfig home: log_json and
    metrics_port are observability concerns, not HTTP concerns.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics_port: int | None = Field(
        default=None,
        description=(
            "Port for the Prometheus /metrics scrape endpoint. Runs as a "
            "separate HTTP server (not on the main api port). Set to None "
            "(default) to disable. Typical production value: 9090."
        ),
    )
    log_json: bool = Field(
        default=False,
        description=(
            "Whether to format logs as JSON (production) or as human-"
            "friendly text (dev). JSON output is required for log "
            "aggregators like Loki/ELK that parse fields."
        ),
    )
    tracing: TracingConfig = TracingConfig()


# ---------- Notification transports ---------- #


class CircuitBreakerConfig(BaseModel):
    """Per-transport circuit-breaker tunables. See
    `trip_ema.notifications.circuit_breaker` for state-machine semantics."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    failure_threshold: int = Field(default=5, ge=1)
    """Consecutive failures before tripping to OPEN. Lower = trips faster."""
    reset_timeout_seconds: float = Field(default=30.0, gt=0.0)
    """How long to stay OPEN before allowing one probe call."""
    half_open_max_successes: int = Field(default=1, ge=1)
    """Successful probes needed to return to CLOSED. 1 is standard CB."""


class TransportsConfig(BaseModel):
    """Settings for the notification transports."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()


# ---------- ETL ---------- #


class EtlConfig(BaseModel):
    """ETL layer configuration.

    Sourced under `etl:` in config.yaml. All fields have sensible
    defaults so a minimal config can still run the ETL.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_1_size: int = Field(default=200, ge=1)
    """Top-N tickers by 30-day ADV that go in Tier 1 (eligible for
    hourly intraday fetch during NSE market hours)."""

    tier_2_size: int = Field(default=500, ge=1)
    """Next-N tickers after Tier 1. The rest go in Tier 3."""

    park_failure_threshold: int = Field(default=3, ge=1)
    """Consecutive ETL failures before a ticker is parked. 3 is a
    reasonable default — transient yfinance hiccups don't trip; real
    delistings or persistent issues do."""

    yfinance_batch_size: int = Field(default=50, ge=1, le=200)
    """Symbols per yf.download() call. yfinance handles bigger batches
    but reliability drops past ~50."""

    yfinance_concurrency: int = Field(default=4, ge=1, le=16)
    """ThreadPoolExecutor worker count for parallel batches. Higher =
    faster + more rate-limit risk; lower = slower + safer."""

    yfinance_request_delay_seconds: float = Field(default=0.5, ge=0.0)
    """Jitter sleep between batches. Helps avoid Yahoo's rate-limit."""

    reconcile_window_days: int = Field(default=7, ge=0)
    """Trailing days of bars eligible for free overwrite. Older bars
    trigger pending_review divergence flags."""

    parked_dir: str = "var/etl/parked"
    """Where parked-failure CSVs live. Relative paths are relative to
    CWD at CLI invocation."""

    checkpoint_dir: str = "var/etl"
    """Where backfill checkpoint files live."""

    eod_run_hour_ist: int = Field(default=17, ge=0, le=23)
    eod_run_minute_ist: int = Field(default=30, ge=0, le=59)
    """When the EoD ETL should run, in IST (Asia/Kolkata). Default 17:30
    after NSE settles at ~17:00."""

    intraday_lookback_days: int = Field(default=5, ge=1, le=730)
    """How many days of recent hourly bars the intraday ETL pulls.
    yfinance limits intraday history to 730 days."""

    exchange: str = "NSE"
    """Which exchange the ETL operates on. NSE only for now; BSE
    would need its own listings parser."""


# ---------- Root ---------- #


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database: DatabaseConfig
    ema: EMAConfig
    atr: ATRConfig
    market_hours: list[MarketHoursConfig]
    scheduling: SchedulingConfig = SchedulingConfig()
    cache: CacheConfig = CacheConfig()
    http: HttpConfig = HttpConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    transports: TransportsConfig = TransportsConfig()
    etl: EtlConfig = EtlConfig()
    redis: "RedisConfig | None" = None
    """Redis configuration for the notifier queue. If None, the
    SubscriberDistributor runs in legacy in-process mode (synchronous
    transport calls from the distributor thread). When set, the
    distributor enqueues jobs to Redis and a separate notifier worker
    process dispatches them."""
    notifier: "NotifierConfig | None" = None
    """Notifier worker process settings. Only consulted when `redis` is
    set."""

    def to_indicator_params(self) -> IndicatorParams:
        """Flatten the hierarchical YAML structure into the engine's flat
        IndicatorParams. Done at the config-loading boundary so downstream
        code never has to know about the YAML shape."""
        return IndicatorParams(
            med_base=self.ema.med.base,
            med_s1=self.ema.med.s1,
            med_s2=self.ema.med.s2,
            med_final=self.ema.med.final,
            long_base=self.ema.long.base,
            long_s1=self.ema.long.s1,
            long_s2=self.ema.long.s2,
            long_final=self.ema.long.final,
            longest_base=self.ema.longest.base,
            longest_s1=self.ema.longest.s1,
            longest_s2=self.ema.longest.s2,
            longest_final=self.ema.longest.final,
            atr_length=self.atr.length,
            inner_mult=self.atr.inner_mult,
            mid_mult=self.atr.mid_mult,
            outer_mult=self.atr.outer_mult,
            volume_sma_length=self.ema.volume_sma_length,
        )


# ---------- Redis + notifier (Phase 7) ---------- #


class RedisConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password_env: str | None = Field(
        default=None,
        description="Optional env var holding the Redis password. "
                    "Leave unset for an open dev Redis.",
    )
    queue_name: str = "trip_ema:notify:queue"
    dedup_hash_name: str = "trip_ema:notify:dedup"
    dedup_max_age_seconds: int = Field(
        default=3600,
        description="A dedup entry stuck longer than this is presumed to "
                    "belong to a crashed worker and gets swept by the "
                    "distributor's periodic cleanup. Should comfortably "
                    "exceed your worst expected end-to-end send latency.",
    )

    def url_without_password(self) -> str:
        return f"redis://{self.host}:{self.port}/{self.db}"


class NotifierConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_count: int = Field(
        default=4,
        ge=1, le=64,
        description="Number of threads inside the notifier process that "
                    "concurrently dispatch sends. The worker process pops "
                    "one job at a time and hands it to a ThreadPoolExecutor; "
                    "this caps in-flight network I/O.",
    )
    pop_timeout_seconds: int = Field(
        default=5,
        ge=1, le=60,
        description="BRPOP timeout. Shorter = more responsive shutdown; "
                    "longer = fewer Redis round-trips when idle.",
    )
    send_timeout_seconds: float = Field(
        default=10.0,
        description="Per-send transport timeout. Webhook/email calls that "
                    "exceed this are dropped (job not ack'd; the distributor "
                    "will re-enqueue on the next cycle).",
    )
    max_attempts: int = Field(
        default=5,
        ge=1, le=100,
        description="Phase 12 — maximum number of times the worker will "
                    "attempt to deliver a single job before moving it to "
                    "the dead-letter queue. The attempts counter persists "
                    "in Redis across distributor cycles, so this is a "
                    "global limit, not a per-cycle one. Operators can "
                    "list/replay/delete DLQ entries via the admin API.",
    )


# Resolve forward refs (RedisConfig and NotifierConfig are declared below
# Config but referenced inside it via string annotations).
DatabaseConfig.model_rebuild()
Config.model_rebuild()
