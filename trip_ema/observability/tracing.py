"""OpenTelemetry tracing — optional, off-by-default.

We use the official OpenTelemetry API. The pattern: every span site
calls `tracer.start_as_current_span(...)`. If no tracer provider has
been initialized, OTel uses a no-op tracer that returns no-op spans
at near-zero cost — so the call sites are always present, and OTel
only does work when explicitly enabled.

To enable OTel:
    from trip_ema.observability.tracing import configure_tracing
    configure_tracing(service_name="trip_ema", exporter="console")
    # ... or exporter="otlp" with endpoint set via env var

To disable: don't call configure_tracing. The no-op behavior is the
default.

Defensive import: if the `opentelemetry` packages aren't installed at
all, `get_tracer()` returns a tiny in-module no-op tracer rather
than raising ImportError. This means a deployment that doesn't want
tracing doesn't need to install the OpenTelemetry SDK — and call
sites in the rest of the codebase (`get_tracer().start_as_current_span(...)`)
work either way.

Design:
- One module-level `tracer` exposed by `get_tracer()`. All instrumented
  sites pull from this so they share span hierarchy.
- `configure_tracing` is idempotent — repeated calls swap the
  provider.
- Spans carry the same correlation_id as our logs (we set it as a
  span attribute). Together with OTel's own trace_id/span_id, you
  get a complete lookup matrix: log → correlation_id → trace.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


# ---------- Local no-op fallback when opentelemetry isn't installed ----------


class _NoOpSpan:
    """Stand-in for opentelemetry.trace.Span when OTel isn't installed.
    All operations are no-ops. Returns False from is_recording so any
    conditional instrumentation skips."""

    def set_attribute(self, key, value): pass
    def set_attributes(self, attrs): pass
    def add_event(self, *args, **kwargs): pass
    def record_exception(self, *args, **kwargs): pass
    def set_status(self, *args, **kwargs): pass
    def update_name(self, name): pass
    def is_recording(self) -> bool: return False
    def end(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


class _NoOpTracer:
    """Stand-in for opentelemetry.trace.Tracer. Returns a _NoOpSpan
    from start_as_current_span which works as a context manager."""

    @contextmanager
    def start_as_current_span(self, name, *args, **kwargs):
        yield _NoOpSpan()


_NOOP_TRACER = _NoOpTracer()


def get_tracer(name: str = "trip_ema"):
    """Return a tracer for the given component name. Always safe to
    call.

    If opentelemetry is installed, returns the real tracer (which is
    OTel's own no-op when no provider is configured — see configure_tracing).
    If opentelemetry isn't installed at all, returns a local no-op
    tracer so call sites don't ImportError.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return _NOOP_TRACER
    return trace.get_tracer(name)


def configure_tracing(
    service_name: str = "trip_ema",
    exporter: str = "console",
    otlp_endpoint: Optional[str] = None,
) -> None:
    """Initialize OpenTelemetry. After this call, spans recorded by
    `get_tracer().start_as_current_span(...)` will be exported.

    Args:
        service_name: Used as the `service.name` resource attribute.
        exporter: 'console' (dev — prints spans to stderr) or 'otlp'
            (production — sends to an OTLP collector).
        otlp_endpoint: Required when exporter='otlp'. e.g.
            'http://otel-collector:4317'.

    Failures during setup are logged but DO NOT raise; if OTel can't
    initialize, the rest of the system still runs.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        if exporter == "console":
            span_exporter = ConsoleSpanExporter()
        elif exporter == "otlp":
            if not otlp_endpoint:
                logger.error(
                    "configure_tracing(exporter='otlp') requires otlp_endpoint"
                )
                return
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
                span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            except ImportError:
                logger.error(
                    "opentelemetry-exporter-otlp not installed; "
                    "install it or use exporter='console'"
                )
                return
        else:
            logger.error("Unknown exporter %r; tracing not configured", exporter)
            return

        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)
        logger.info(
            "OpenTelemetry tracing configured",
            extra={"service_name": service_name, "exporter": exporter},
        )
    except ImportError:
        logger.info(
            "OpenTelemetry packages not installed; tracing disabled "
            "(install opentelemetry-api + opentelemetry-sdk to enable)"
        )
    except Exception:
        logger.exception("Failed to configure tracing; continuing without it")


def set_span_attribute_if_active(key: str, value) -> None:
    """Set an attribute on the currently-active span, if any. Safe to
    call even when no span is active OR opentelemetry isn't installed
    (no-op in both cases).

    Use to enrich spans with domain context (ticker, timeframe, etc.)
    without forcing the caller to know about tracer state."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute(key, value)
    except ImportError:
        # OTel not installed — no-op
        pass
    except Exception:
        # Tracing must never break the caller
        pass


def add_correlation_id_to_span() -> None:
    """If a correlation_id is active in our context, attach it to the
    current span as an attribute. Call once near the start of a
    function that has both a correlation_id context and a span — the
    pairing lets you cross-reference logs and traces."""
    from .correlation import current_correlation_id
    cid = current_correlation_id()
    if cid is not None:
        set_span_attribute_if_active("correlation_id", cid)
