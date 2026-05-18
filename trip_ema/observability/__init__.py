"""Observability — structured logging, correlation IDs, Prometheus metrics, OpenTelemetry tracing."""

from .correlation import (
    correlation_id,
    current_correlation_id,
    get_or_mint_correlation_id,
    new_correlation_id,
)
from .logging_config import configure_logging
from . import metrics
from .metrics_server import MetricsServer, start_metrics_server
from .tracing import (
    add_correlation_id_to_span,
    configure_tracing,
    get_tracer,
    set_span_attribute_if_active,
)

__all__ = [
    "correlation_id",
    "current_correlation_id",
    "get_or_mint_correlation_id",
    "new_correlation_id",
    "configure_logging",
    "metrics",
    "MetricsServer",
    "start_metrics_server",
    "add_correlation_id_to_span",
    "configure_tracing",
    "get_tracer",
    "set_span_attribute_if_active",
]
