"""Structured logging configuration.

`configure_logging(level, json_output)` replaces the root logger's
handlers with a single handler that writes:
  - JSON to stderr if json_output=True (production)
  - Plain text to stderr otherwise (dev)

Both formats include the correlation_id (from observability.correlation)
if one is set in the current context. Both also surface any `extra=`
fields passed to log calls (the standard Python logging convention).

Usage:
    from trip_ema.observability.logging_config import configure_logging
    configure_logging(level="INFO", json_output=True)

    logger.info("computed signal", extra={
        "ticker": "NSE:NIFTY", "regime": "BULLISH",
    })

The JSON output looks like:
    {"ts": "...", "level": "INFO", "logger": "...",
     "message": "computed signal",
     "correlation_id": "abc-123",
     "ticker": "NSE:NIFTY", "regime": "BULLISH"}
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .correlation import current_correlation_id


# Standard LogRecord attributes that we DON'T want to spam into JSON
# output. Everything else passed via `extra={...}` will appear.
_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "taskName",  # 3.12+
})


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line.

    Includes the current correlation_id automatically. Picks up any
    `extra=` fields the caller passed (Python logging surfaces them
    as direct attributes on the LogRecord).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = current_correlation_id()
        if cid is not None:
            payload["correlation_id"] = cid
        # Spread any `extra=` fields the caller passed
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            # Coerce non-JSON-serializable values to str rather than
            # blowing up on the log call.
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-friendly format for dev. Includes correlation_id inline
    when present so a tail-following dev still sees the linkage."""

    _BASE_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def __init__(self):
        super().__init__(self._BASE_FMT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        cid = current_correlation_id()
        if cid is not None:
            line = f"{line}  [cid={cid[:8]}]"
        # Append extra fields if any
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED_RECORD_ATTRS and not k.startswith("_")
        }
        if extras:
            extra_str = " ".join(f"{k}={v!r}" for k, v in extras.items())
            line = f"{line}  {extra_str}"
        return line


def configure_logging(
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure the root logger with a single stderr handler using
    either JSON or text format. Idempotent — clears existing handlers
    first so repeated calls (e.g., in tests) don't compound."""
    root = logging.getLogger()
    # Clear existing handlers — this is the idempotency guarantee
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root.addHandler(handler)
    # Accept either a string ("INFO", "DEBUG") or an int (already a
    # logging level constant — what argparse-counted `-v` flags produce
    # after being mapped to logging.DEBUG/logging.INFO/etc.)
    if isinstance(level, int):
        root.setLevel(level)
    else:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
