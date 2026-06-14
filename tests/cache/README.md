# `tests/cache/` — Tests for `trip_ema/cache/`

Covers `SignalCache`'s read/write semantics, thread-safety, and key
handling.

## Files

| File | Tests |
|---|---|
| `test_signal_cache.py` | Cache read/write, multi-key handling, concurrent reads, single-writer invariant |

## Needs

- No DB
- No Qt
- Fast — runs in < 1s

## Invariant under test

- **3.2 single-writer.** The tests confirm that concurrent reads are
  safe but parallel writes (simulated multi-process scenarios) would
  break the cache. The tests don't try to "fix" this; they document
  the contract
