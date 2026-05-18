"""Correlation IDs — per-logical-operation identifiers that thread
through scheduler → cache write → distributor → Redis → worker → transport.

The ID is just a UUID4 string. The point isn't strict tracing (use
OpenTelemetry for that — see #9); it's grep-ability across log streams.
A trader reports "I got a duplicate BUY at 14:32"; you grep one
correlation_id across compute / worker / transport logs and see the
full lifecycle.

Design:
- A `contextvars.ContextVar` holds the current correlation ID.
  contextvars work correctly under threading and asyncio — different
  from threading.local which doesn't propagate into asyncio tasks.
- `with correlation_id(some_id):` sets the var for the block; the
  prior value is restored on exit (re-entrant).
- `current_correlation_id()` returns the active ID, or None if
  none is set.
- `new_correlation_id()` mints a fresh UUID4 and returns it (caller
  is responsible for setting it via the context manager).

The JSON log formatter (in `logging_config.py`) picks the ID up
automatically by reading the contextvar, so callers don't need to
pass it explicitly to log calls.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_correlation_id_var: ContextVar[str | None] = ContextVar(
    "correlation_id", default=None,
)


def current_correlation_id() -> str | None:
    """Return the correlation ID active in the current context, or None."""
    return _correlation_id_var.get()


def new_correlation_id() -> str:
    """Mint a fresh UUID4 correlation ID. Caller is responsible for
    pushing it onto the context via `with correlation_id(...)`."""
    return str(uuid.uuid4())


@contextmanager
def correlation_id(cid: str | None) -> Iterator[str | None]:
    """Push `cid` onto the context for the duration of the with-block.

    Re-entrant: nested calls save the prior value and restore it on
    exit. Passing None explicitly clears the ID inside the block
    (useful when crossing a boundary into code that shouldn't be
    correlated with the outer logical operation).
    """
    token = _correlation_id_var.set(cid)
    try:
        yield cid
    finally:
        _correlation_id_var.reset(token)


def get_or_mint_correlation_id() -> str:
    """Return the current ID if set; otherwise mint a new one and
    return it WITHOUT pushing it onto the context. Use when you want
    an ID to put into a log record but don't want to alter the
    current context.

    For propagation across a boundary (e.g., enqueueing a Redis job),
    prefer this; the receiver will push the ID onto its own context
    via `correlation_id(...)`.
    """
    existing = current_correlation_id()
    if existing is not None:
        return existing
    return new_correlation_id()
