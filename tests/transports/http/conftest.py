"""HTTP test fixtures.

We build a real FastAPI app but inject fake state via dependency overrides.
This avoids spinning up the whole entry point (DB migrations, schedulers,
etc.) for tests that only need to verify route behavior.

Tests that DO need a real DB can use the `db_engine` fixture from the
persistence conftest — re-exported below.

# Step 9 note

TickerRegistry is gone (deleted in 9a). The fixture no longer
constructs one or overrides get_registry. Tests that previously
asserted "this many engines were primed" need to be rewritten to
assert against the cache snapshot count instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from trip_ema.cache import SignalCache
from trip_ema.config.schema import (
    ATRConfig,
    CacheConfig,
    Config,
    DatabaseConfig,
    EMAConfig,
    EMAStageConfig,
    HttpConfig,
    MarketHoursConfig,
    SchedulingConfig,
)
from trip_ema.transports.http import create_app
# === STEP 14B-FIXUP4-conftest-import: scheduler/distributor getters stripped ===
from trip_ema.transports.http.dependencies import (
    get_cache,
    get_config,
    get_db_engine,
    get_redis_queue,
)

# Re-export the persistence DB fixtures for tests that need a real DB.
from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)


def _build_test_config(admin_token_env: str = "TRIP_EMA_ADMIN_TOKEN") -> Config:
    """A minimal valid Config, suitable for HTTP tests.

    Doesn't need real DB credentials because tests override get_db_engine.
    Doesn't need real SMTP because tests override get_config when needed.
    """
    return Config(
        database=DatabaseConfig(
            host="ignored", database="ignored",
            user="ignored", password_env="IGNORED_ENV",
        ),
        ema=EMAConfig(
            med=EMAStageConfig(base=8, s1=4, s2=4, final=None),
            long=EMAStageConfig(base=15, s1=4, s2=4, final=3),
            longest=EMAStageConfig(base=28, s1=7, s2=5, final=3),
        ),
        atr=ATRConfig(),
        market_hours=[MarketHoursConfig(
            exchange="NSE", open="09:15", close="15:30",
            tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
        )],
        scheduling=SchedulingConfig(),
        cache=CacheConfig(),
        http=HttpConfig(admin_token_env=admin_token_env),
    )


# === STEP 14B-FIXUP4-conftest-fixture: compute_scheduler field removed ===
@dataclass
class HttpFixture:
    """Bundle of objects most HTTP tests need."""
    client: TestClient
    cache: SignalCache
    config: Config


@pytest.fixture
def http_app() -> Iterator[HttpFixture]:
    """A wired-up FastAPI TestClient with empty cache and no real DB.
    Sufficient for testing route behavior in isolation.

    The compute_scheduler dependency is overridden with a stub that
    reports 'never started, never completed' — this is healthy from
    /readyz's perspective (in tests we don't actually run schedulers).
    """
    # === STEP 14B-FIXUP4-conftest-body: scheduler stub + overrides removed ===
    cache = SignalCache()
    config = _build_test_config()

    app = create_app()
    app.dependency_overrides[get_cache] = lambda: cache
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_redis_queue] = lambda: None
    # db_engine is NOT overridden by default; tests that need it set
    # their own overrides.

    with TestClient(app) as client:
        yield HttpFixture(
            client=client, cache=cache, config=config,
        )
    app.dependency_overrides.clear()