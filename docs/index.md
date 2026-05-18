# trip_ema

Python port of the Weekly Triple-EMA + ATR-band Pine indicator,
plus a production-quality service that:

- Computes signals from real OHLCV bars stored in Postgres
- Distributes notifications via email, webhook, or Slack
- Exposes an HTTP API for stateless compute, cached queries, and
  admin operations
- Runs an ETL pipeline (`trip-ema-etl`) against yfinance + NSE
  listings to keep the bar data fresh
- Offers a PySide6 desktop operator console for monitoring and
  manual interventions

## Where to start

- **Operators**: see the [Configuration](operator/configuration.md)
  and [Running the service](operator/running.md) pages
- **Developers**: see the [Architecture overview](architecture/index.md)
  and the [Invariants](architecture/invariants.md)
- **Reviewers / managers**: see the [Phase 11 changelog](changelog/phase-11.md)
  for the most recent feature delivery

## Quick links

- Source: see the repository README
- Conversation history (provenance): see
  `pine_indicator_port_conversation.docx` in the repo root —
  the long-form thinking and decisions that produced each phase

## Design principles

The codebase is governed by 14 architectural invariants — short
written rules that encode hard-won lessons from each phase. They
live in [`architecture/invariants.md`](architecture/invariants.md)
and are referenced inline in code comments.

The principle behind the principles: **make the obvious-but-wrong
thing impossible at the type or process level**, not merely
discouraged in review. The Pine parity harness, the single-writer
invariant for `SignalCache`, the per-process DB roles added in
Phase 12, and the test conftest's deferred-deletion fixtures are
all examples.
