"""Standalone Prometheus metrics server.

Lives on a separate port from the main FastAPI app (typically 9090).
Three reasons for separate port:
  1. Standard ops pattern — firewall scrape port separately from
     public API.
  2. Can be disabled in dev without affecting the main API.
  3. Survives if the main app is mid-shutdown.

Implementation uses `prometheus_client.start_http_server` which spins
up a background thread serving `/metrics`. We wrap it so that:
  - The server can be stopped cleanly during graceful shutdown.
  - A None port disables the server entirely (dev / test default).
  - Errors during startup log loudly but don't kill the process.
  - If prometheus_client isn't installed at all, start_metrics_server
    returns None (mirrors the disabled-port path).

The lifecycle is managed by `app.py`: at startup, call
`start_metrics_server(port)` and store the returned `MetricsServer`
in `AppState`. The graceful-shutdown callback (item #8) then calls
`metrics_server.stop()` first.
"""

from __future__ import annotations

import logging

from .metrics import REGISTRY

logger = logging.getLogger(__name__)


class MetricsServer:
    """A running Prometheus exposition HTTP server. Provided by
    `start_metrics_server`; consumed by the graceful-shutdown
    callback. The underlying prometheus_client.start_http_server
    returns (HTTPServer, Thread); we hold both so stop() can shut
    down cleanly."""

    def __init__(self, http_server, thread, port: int):
        self._server = http_server
        self._thread = thread
        self.port = port
        self._stopped = False

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the server. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            logger.exception("Error stopping metrics server")
        try:
            self._thread.join(timeout=timeout)
        except Exception:
            logger.exception("Error joining metrics server thread")


def start_metrics_server(port: int | None) -> MetricsServer | None:
    """Start a Prometheus /metrics server on `port` (separate from
    the main API). Return a MetricsServer handle, or None if port is
    None (metrics disabled) or prometheus_client is not installed.

    Errors during startup are logged but DO NOT propagate — metrics
    failure must never prevent the main service from starting. Better
    to run without metrics than not at all.
    """
    if port is None:
        logger.info("Metrics server disabled (port=None)")
        return None
    try:
        from prometheus_client import start_http_server
    except ImportError:
        logger.info(
            "prometheus_client not installed; metrics server disabled "
            "(install prometheus_client to enable)"
        )
        return None
    try:
        http_server, thread = start_http_server(port, registry=REGISTRY)
        logger.info("Metrics server started on port %d", port)
        return MetricsServer(http_server, thread, port)
    except Exception:
        logger.exception(
            "Failed to start metrics server on port %d; continuing without metrics",
            port,
        )
        return None
