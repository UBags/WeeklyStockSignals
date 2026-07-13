"""Phase 12 — DB role separation: URL building for the
optional per-process credentials. No DB needed — these tests just
verify the URL construction picks the right user/password/env-var
combination."""

from __future__ import annotations

import pytest

from trip_ema.config.schema import (
    ATRConfig, CacheConfig, Config, DatabaseConfig,
    DbRoleCredentials, DbRolesConfig,
    EMAConfig, EMAStageConfig, HttpConfig, MarketHoursConfig,
    SchedulingConfig,
)
from trip_ema.persistence.connection import (
    build_db_url, build_db_url_for_role,
)


def _make_config(db: DatabaseConfig) -> Config:
    return Config(
        database=db,
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
        http=HttpConfig(admin_token_env="ADMIN_TOK"),
    )


class TestBackwardCompat:
    """When roles is None, every role falls back to the top-level
    user/password — preserves existing deployments."""

    def test_no_roles_means_top_level_user_for_all(self):
        env = {"T_PWD": "secret"}
        db = DatabaseConfig(
            host="h.example", database="trip_ema",
            user="trip_app", password_env="T_PWD",
        )
        config = _make_config(db)
        top = build_db_url(config, env=env)
        assert "trip_app:secret@" in top
        # All three roles return the same URL
        for role in ("etl", "engine", "gui"):
            url = build_db_url_for_role(config, role, env=env)
            assert url == top, f"role={role} should fall back when unset"


class TestRoleOverride:
    """When a role is configured, it uses its own user + password env."""

    def test_etl_role_uses_etl_credentials(self):
        env = {"T_PWD": "topsecret", "ETL_PWD": "etlsecret"}
        db = DatabaseConfig(
            host="h.example", database="trip_ema",
            user="trip_app", password_env="T_PWD",
            roles=DbRolesConfig(
                etl=DbRoleCredentials(
                    user="trip_etl_user", password_env="ETL_PWD",
                ),
            ),
        )
        config = _make_config(db)
        etl_url = build_db_url_for_role(config, "etl", env=env)
        assert "trip_etl_user:etlsecret@h.example:5432/trip_ema" in etl_url

    def test_unconfigured_role_falls_back_even_when_other_roles_set(self):
        """Only etl is configured; engine and gui must still fall
        back to the top-level credentials."""
        env = {"T_PWD": "topsecret", "ETL_PWD": "etlsecret"}
        db = DatabaseConfig(
            host="h.example", database="trip_ema",
            user="trip_app", password_env="T_PWD",
            roles=DbRolesConfig(
                etl=DbRoleCredentials(
                    user="trip_etl_user", password_env="ETL_PWD",
                ),
            ),
        )
        config = _make_config(db)
        engine_url = build_db_url_for_role(config, "engine", env=env)
        assert "trip_app:topsecret@" in engine_url
        gui_url = build_db_url_for_role(config, "gui", env=env)
        assert "trip_app:topsecret@" in gui_url

    def test_missing_password_env_raises(self):
        """If the role's password_env is missing from environment,
        we want a clear error — not a silent fallback or a connection
        attempt with empty password."""
        # ETL_PWD intentionally absent from the env mapping.
        env = {"T_PWD": "topsecret"}
        db = DatabaseConfig(
            host="h.example", database="trip_ema",
            user="trip_app", password_env="T_PWD",
            roles=DbRolesConfig(
                etl=DbRoleCredentials(
                    user="trip_etl_user", password_env="ETL_PWD",
                ),
            ),
        )
        config = _make_config(db)
        with pytest.raises(RuntimeError, match="ETL_PWD"):
            build_db_url_for_role(config, "etl", env=env)
