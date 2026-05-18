"""`trip-ema-console` — desktop operator console for trip_ema.

Reads:
  - --config (defaults to ./config.yaml) for DB connection info.
  - TRIP_EMA_ADMIN_TOKEN env var for the HTTP client. If unset, the
    GUI runs read-only — action buttons are disabled and the status
    bar explains why.
  - --base-url for the trip_ema main service (default http://localhost:8000).

Then it opens the main window and starts the Qt event loop.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from ..config.loader import load_config
from ..observability.logging_config import configure_logging
from .db import GuiDbSession
from .http_client import HttpClientConfig, TripEmaHttpClient

logger = logging.getLogger(__name__)


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point. Returns a process exit code."""
    args = _parse_args(argv)

    # PySide6 import is lazy so `--help` works without it installed.
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 not installed. Install with `pip install trip-ema[gui]`.",
            file=sys.stderr,
        )
        return 2

    configure_logging(
        level=logging.INFO if args.verbose else logging.WARNING,
        json_output=False,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"error: config not found at {config_path}", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"error: failed to load config: {e}", file=sys.stderr)
        return 2

    token = os.environ.get(args.token_env)
    http_cfg = HttpClientConfig(
        base_url=args.base_url, token=token,
    )

    # Qt application
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("trip_ema")
    app.setOrganizationName("trip_ema")

    db = GuiDbSession(config)
    http = TripEmaHttpClient(http_cfg)

    # Lazy MainWindow import — keeps `--help` import-light.
    from .main_window import MainWindow
    window = MainWindow(db=db, http=http, config_path=config_path)
    window.show()

    return app.exec()


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="trip-ema-console",
        description="Desktop operator console for trip_ema.",
    )
    p.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    p.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="trip_ema main-service URL (default: http://localhost:8000)",
    )
    p.add_argument(
        "--token-env",
        default="TRIP_EMA_ADMIN_TOKEN",
        help="Env var holding the admin bearer token (default: TRIP_EMA_ADMIN_TOKEN)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="INFO logging (DEBUG via repeat is not supported here)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
