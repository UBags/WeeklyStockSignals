"""Regression: SQLAlchemy text() bind-name parsing in enrich_batch SQL.

Background:
  enrich_batch's _fetch_ticker_basics() uses Postgres `unnest()` with
  three parallel text[] arrays. The first version of that SQL wrote the
  casts as `:exchanges::text[]`. SQLAlchemy's text() parser detects bind
  parameter names with a `:(\\w+)(?!:)` pattern — the `(?!:)` lookahead
  requires the name NOT be followed by a colon. With `:exchanges::text[]`
  the lookahead fails, the parser backs off the greedy `\\w+`, and binds
  the SINGULAR `:exchange` instead. At execute time the params dict
  (plural keys) doesn't have the singular key, so SQLAlchemy raises
  "A value is required for bind parameter 'exchange'". That exception
  is caught by enrich_batch's broad try/except, EMPTY_ENRICHMENT is
  returned, and every row in every email gets rendered with '—' /
  NEUTRAL for mcap / sector / zone / direction / last-5.

  The bug shipped silently because:
    a) the log went to /tmp/trip-ema.log, not the expected /var/log path
    b) there was no test for enrich_batch SQL parsing at all
    c) the manual smoke-test SQL was run via psql with literal values,
       which doesn't exercise the SQLAlchemy bind path

This test pins both halves of the contract:
  (1) the static SQL has bind names matching what _fetch_ticker_basics
      passes as the params dict
  (2) execute() is reachable with the plural keys without a parameter-
      not-found error
"""

from unittest.mock import MagicMock


def test_fetch_ticker_basics_uses_plural_bind_names():
    """The SQL must declare bind params named exchanges / symbols /
    instruments (plural). If a future edit drops the parens around any
    `:foo::text[]` cast, SQLAlchemy will back off to the singular name
    and this assertion fails — caught at collection-time, not in
    production where the symptom is empty enrichment in every email.
    """
    from trip_ema.notifications.enrichment import _fetch_ticker_basics
    from trip_ema.domain import TickerRef

    captured: dict = {}

    def fake_execute(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        return result

    conn = MagicMock()
    conn.execute = fake_execute

    refs = [TickerRef(exchange="NSE", symbol="BAJFINANCE", instrument="EQ")]
    _fetch_ticker_basics(conn, refs)

    sql = captured["sql"]
    binds_in_sql = set(sql._bindparams.keys())
    assert binds_in_sql == {"exchanges", "symbols", "instruments"}, (
        f"Expected plural bind names in _fetch_ticker_basics SQL; "
        f"got {sorted(binds_in_sql)}. This usually means a `:foo::text[]` "
        f"cast was written without parens; the fix is `(:foo)::text[]`."
    )

    # The params dict must match the bind names exactly — otherwise
    # SQLAlchemy raises 'A value is required for bind parameter ...'
    # and enrich_batch's broad except swallows it.
    assert set(captured["params"].keys()) == binds_in_sql


def test_fetch_ticker_basics_compiles_with_supplied_params():
    """Compile the SQL against the Postgres dialect. A parser drift
    that introduces a name mismatch (or drops a bind) will show up
    here in milliseconds, before the SQL ever reaches a real DB."""
    from sqlalchemy.dialects import postgresql
    from trip_ema.notifications.enrichment import _fetch_ticker_basics
    from trip_ema.domain import TickerRef

    captured: dict = {}

    def fake_execute(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        return result

    conn = MagicMock()
    conn.execute = fake_execute
    refs = [
        TickerRef(exchange="NSE", symbol="BAJFINANCE", instrument="EQ"),
        TickerRef(exchange="NSE", symbol="RELIANCE", instrument="EQ"),
    ]
    _fetch_ticker_basics(conn, refs)

    rendered = str(captured["sql"].compile(dialect=postgresql.dialect()))
    # The compiled output references all three plural names in psycopg
    # paramstyle (`%(exchanges)s` etc.) — not the singular variants the
    # buggy version produced.
    assert "exchanges" in rendered
    assert "symbols" in rendered
    assert "instruments" in rendered
