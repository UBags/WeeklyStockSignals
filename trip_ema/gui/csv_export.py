"""CSV-export utility.

Prompts the user for a destination path with QFileDialog, writes a
list-of-rows to disk, and emits a status-bar message on success/
failure. Used by the Tickers, Subscribers, and Notification Log
tabs — the "Export to CSV" buttons all funnel here.

Kept tiny and Qt-aware so it can show error dialogs without the
caller having to remember.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable, Iterable

from PySide6.QtWidgets import QWidget

from .prompts import DEFAULT_PROMPTS, Prompts
from ..domain.market_session import market_now

logger = logging.getLogger(__name__)


def export_rows_to_csv(
    parent: QWidget,
    *,
    default_filename: str,
    columns: list[str],
    rows: Iterable[list],
    success_callback: Callable | None = None,
    prompts: Prompts = DEFAULT_PROMPTS,
) -> Path | None:
    """Prompt for a save path and write rows.

    Args:
      parent: Owning widget (for the file dialog parent).
      default_filename: Suggested filename, e.g. "tickers_2026-05-17.csv".
      columns: Header row labels.
      rows: Iterable of lists matching columns length. Each list is
        ONE CSV row. Values get str()-coerced; None becomes empty.
      success_callback: Optional callable(path: Path) -> None invoked
        on successful write.

    Returns the chosen path on success, None if the user cancelled
    or the write failed (the user has already seen an error dialog
    in that case)."""
    suggested = str(Path.cwd() / default_filename)
    path_str = prompts.get_save_filename(
        parent, "Export to CSV", suggested, "CSV files (*.csv);;All files (*)",
    )
    if not path_str:
        return None

    path = Path(path_str)
    try:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            row_count = 0
            for row in rows:
                writer.writerow([
                    "" if v is None else str(v) for v in row
                ])
                row_count += 1
        logger.info(
            "CSV export complete",
            extra={"path": str(path), "rows": row_count},
        )
    except Exception as e:
        logger.exception("CSV export failed")
        prompts.warning(
            parent, "Export failed",
            f"Could not write {path}:\n\n{e}",
        )
        return None

    if success_callback is not None:
        try:
            success_callback(path)
        except Exception:
            logger.exception("success_callback raised; ignoring")
    return path


def timestamped_filename(
    prefix: str, *, ext: str = "csv", exchange: str | None = None,
) -> str:
    """Build a default filename like 'tickers_20260517_140312.csv'."""
    # An export of NYSE signals stamped in Indian time is a lie about
    # when it was taken. With no exchange (un-provisioned database) fall
    # back to UTC — explicit, never the machine's local clock.
    stamp = market_now(exchange) if exchange else datetime.now(UTC)
    ts = stamp.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.{ext}"
