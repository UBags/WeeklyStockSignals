# Invariants

The 14 invariants are documented inline in
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) §3. They're the rules
that govern code review and architecture decisions.

Quick reference:

| # | Name | What it protects |
|---|---|---|
| 3.1 | ETL is the source of truth for bar availability | Engine never invents bars |
| 3.2 | Single writer to `SignalCache` | Race-free cache reads |
| 3.3 | Pine Script numerical fidelity | Indicator values match TradingView |
| 3.4 | Domain types are immutable | No accidental mutation across threads |
| 3.5 | All timestamps are UTC timezone-aware | No naive-datetime bugs |
| 3.6 | Provisional ↔ final bar semantics | Don't emit on incomplete bars |
| 3.7 | DST-correct week boundaries | Correct week-bar windowing year-round |
| 3.8 | No subscriber-visible STALE_DATA notifications | Don't surface diagnostic state |
| 3.9 | `stale_since` is the FIRST observation | Recovery doesn't reset detection |
| 3.10 | Notification dedup at application layer | Multi-machine deploys don't double-send |
| 3.11 | Cache bounded by ticker count, not time | Memory is predictable |
| 3.12 | Observability is additive — never load-bearing | The system runs without metrics |
| 3.13 | Circuit breakers wrap transports | Bad downstream ≠ failed worker |
| 3.14 | Engine/ETL/GUI process boundaries | Each process has its responsibility |

If you're proposing a change that appears to violate one, the
default is to rethink the change. Genuine reasons to break an
invariant exist (§6 of ARCHITECTURE.md covers them) but the bar
is high.
