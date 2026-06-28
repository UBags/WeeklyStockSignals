"""Stdout parser for `trip-ema-etl cycle` output.

cmd_cycle (13d-1) emits one-line progress markers shaped like:

    V1 cycle starting: exchange=NSE kind=intraday market_open=True
    compute_runs row: run_id=42
      [fetch_bars] running
      [fetch_bars] done
      [stock_state] running
      [stock_state] done
      [stock_enrichment] running
      [stock_enrichment] done
      [rotation] running
      [rotation] done
      [derive] running
      [derive] done
      [iiv] running
      [iiv] done
      [industry_state] running
      [industry_state] done
      [industry_enrichment] running
      [industry_enrichment] done
      [notify] running
      [notify] done
    cycle complete (exchange=NSE, kind=intraday, run_id=42)

On stage failure, the line shape is:

      [iiv] failed: <exception_msg>

And the cycle ends with a stderr line:

    cycle FAILED in iiv: <exception_msg>

This module turns those raw lines into the (label, status, raw_msg)
tuples the GUI's StageProgressList consumes. Three internal stages
collapse into the "Compute Signals" GUI label:
rotation, derive, cache_rebuild.
"""
from __future__ import annotations


# Internal-name → GUI-label mapping.
# Order matters here: callers iterate this for the canonical stage
# order when applying the "all earlier stages done" heuristic.
STAGE_NAME_TO_LABEL: dict[str, str] = {
    "fetch_bars": "Fetch Stock OHLCV",
    "stock_state": "Compute Stock Indicators",
    "stock_enrichment": "Compute Stock Enrichment",
    "rotation": "Compute Signals",
    "derive": "Compute Signals",
    "cache_rebuild": "Compute Signals",
    "iiv": "Refresh Industry Index Values",
    "industry_state": "Compute Industry Indicators",
    "industry_enrichment": "Compute Industry Enrichment",
    "notify": "Notify Subscribers",
}


# Canonical GUI label ordering (must match LIVE_CYCLE_STAGES in
# trip_ema.gui.views.widgets.stage_progress).
LIVE_CYCLE_LABEL_ORDER: tuple[str, ...] = (
    "Fetch Stock OHLCV",
    "Compute Stock Indicators",
    "Compute Stock Enrichment",
    "Compute Signals",
    "Refresh Industry Index Values",
    "Compute Industry Indicators",
    "Compute Industry Enrichment",
    "Notify Subscribers",
)


def parse_stage_line(line: str) -> tuple[str, str, str] | None:
    """Parse a single stdout line emitted by `cmd_cycle`.

    Args:
        line: a single line from cmd_cycle's stdout (typically the
            leading two-space indent + bracketed stage name + message)

    Returns:
        (label, status, raw_message) where:
          - label: the GUI-facing label from LIVE_CYCLE_LABEL_ORDER
          - status: one of "running", "done", "failed"
          - raw_message: the original message body (e.g.
            "failed: connection refused", "done: skipped (no signals)")
        Returns None if the line is not a stage marker.

    Examples:
        >>> parse_stage_line("  [fetch_bars] running")
        ('Fetch Stock OHLCV', 'running', 'running')
        >>> parse_stage_line("  [iiv] failed: connection lost")
        ('Refresh Industry Index Values', 'failed',
         'failed: connection lost')
        >>> parse_stage_line(
        ...     "  [notify] done: skipped (no signals)")
        ('Notify Subscribers', 'done',
         'done: skipped (no signals)')
        >>> parse_stage_line("cycle complete")  # not a stage line

    Notes:
        - Lines for the rotation/derive/cache_rebuild internal
          stages all map to the same "Compute Signals" label, since
          the GUI surfaces them as a single user-facing stage.
        - Unknown internal stage names return None (graceful).
        - Lines with neither "running" / "done*" / "failed*" status
          return None.
    """
    stripped = line.strip()
    if not (stripped.startswith("[") and "]" in stripped):
        return None
    closing = stripped.find("]")
    internal_name = stripped[1:closing]
    msg = stripped[closing + 1:].strip()

    label = STAGE_NAME_TO_LABEL.get(internal_name)
    if label is None:
        return None

    if msg == "running":
        status = "running"
    elif msg.startswith("done"):
        # Covers "done" and "done: skipped (no signals)" etc.
        status = "done"
    elif msg.startswith("failed"):
        # Covers "failed: <error>"
        status = "failed"
    else:
        return None

    return (label, status, msg)


def labels_before(label: str) -> list[str]:
    """Return all canonical labels that come BEFORE the given label
    in LIVE_CYCLE_LABEL_ORDER.

    Used to mark earlier stages as done when a later stage emits
    "running" — protects against missing emits (e.g. if a stage
    emits "running" but never gets a "done" because the next stage
    started, the GUI still transitions it).
    """
    if label not in LIVE_CYCLE_LABEL_ORDER:
        return []
    idx = LIVE_CYCLE_LABEL_ORDER.index(label)
    return list(LIVE_CYCLE_LABEL_ORDER[:idx])


def parse_cycle_failure_summary(
    stderr_text: str,
) -> tuple[str | None, str]:
    """Find the last `cycle FAILED in <stage>: <msg>` line in stderr.

    Args:
        stderr_text: full captured stderr from the QProcess.

    Returns:
        (failed_stage, error_message). If no failure summary line
        is present, returns (None, "").

        `failed_stage` is the INTERNAL stage name (e.g. "iiv",
        "industry_state"); use STAGE_NAME_TO_LABEL to translate.

    Examples:
        >>> parse_cycle_failure_summary(
        ...     "cycle FAILED in iiv: connection refused\\n")
        ('iiv', 'connection refused')
        >>> parse_cycle_failure_summary("")
        (None, '')
        >>> parse_cycle_failure_summary("some other error\\n")
        (None, '')
    """
    prefix = "cycle FAILED in "
    for raw in reversed(stderr_text.splitlines()):
        line = raw.strip()
        if line.startswith(prefix):
            rest = line[len(prefix):]
            if ":" in rest:
                stage, msg = rest.split(":", 1)
                return (stage.strip(), msg.strip())
            # Edge case: malformed line — return what we can
            return (rest.strip() or None, "")
    return (None, "")


def is_cycle_complete_line(line: str) -> bool:
    """Return True if `line` is the cmd_cycle success summary.

    Shape: `cycle complete (exchange=NSE, kind=intraday, run_id=42)`
    """
    return line.strip().startswith("cycle complete")
