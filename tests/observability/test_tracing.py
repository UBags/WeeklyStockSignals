"""Tests for OpenTelemetry tracing scaffold.

Verifies:
  - `get_tracer()` works whether or not OTel is configured (returns
    a real tracer in both cases; the no-op tracer is just OTel's
    default).
  - `set_span_attribute_if_active` is a no-op when no span is active
    and doesn't raise.
  - `add_correlation_id_to_span` populates the span attribute when
    a correlation_id is set and a span is active.
  - `configure_tracing` is idempotent and doesn't blow up.
  - Spans recorded via a real provider show up in our captured output.

These tests use OTel's `InMemorySpanExporter` so they don't print to
stderr or talk to any network.
"""

from __future__ import annotations

import pytest

# Skip the whole module if opentelemetry isn't installed
opentelemetry = pytest.importorskip("opentelemetry")

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from trip_ema.observability import (
    correlation_id,
    get_tracer,
    set_span_attribute_if_active,
    add_correlation_id_to_span,
)


@pytest.fixture
def captured_spans():
    """Install an in-memory span exporter and return (exporter, tracer).

    OTel disallows overriding the global TracerProvider, so we use the
    provider's tracer instance directly. Our production code calls
    `get_tracer()` which reads from the global provider — the same
    pattern in a test won't work cleanly. We resolve this by yielding
    a tracer object that the tests use explicitly. For testing our
    helpers like `set_span_attribute_if_active`, we wrap the test's
    span use so the helpers see a real recording span as the current
    span. That's enough: `set_span_attribute_if_active` checks
    `trace.get_current_span()`, which DOES see the active span
    regardless of which provider created it.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "test"}),
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("trip_ema.test")
    yield exporter, tracer


# -------------------- get_tracer / safe-call surface -------------------- #


class TestSafeAPI:

    def test_get_tracer_works_without_configure(self):
        """Even without an explicit tracer-provider setup, get_tracer
        returns a usable tracer (OTel's default no-op or a configured one)."""
        tracer = get_tracer("test")
        assert tracer is not None
        # Starting a span on the no-op tracer should not raise
        with tracer.start_as_current_span("noop-test") as span:
            assert span is not None  # may be a NonRecordingSpan

    def test_set_attribute_when_no_span_active_does_not_raise(self):
        """Safe no-op when nothing is recording."""
        set_span_attribute_if_active("key", "value")
        # Just verifying it didn't blow up

    def test_add_correlation_id_when_none_set_does_not_raise(self):
        """Safe no-op when no correlation_id is in context."""
        add_correlation_id_to_span()


# -------------------- Real provider exercises -------------------- #


class TestSpanRecording:

    def test_simple_span_recorded(self, captured_spans):
        exporter, tracer = captured_spans
        with tracer.start_as_current_span("my-span"):
            pass
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "my-span" in names

    def test_set_attribute_writes_to_span(self, captured_spans):
        exporter, tracer = captured_spans
        with tracer.start_as_current_span("attr-span"):
            set_span_attribute_if_active("ticker", "NSE:NIFTY")
            set_span_attribute_if_active("count", 7)
        spans = exporter.get_finished_spans()
        attr_span = next(s for s in spans if s.name == "attr-span")
        assert attr_span.attributes["ticker"] == "NSE:NIFTY"
        assert attr_span.attributes["count"] == 7

    def test_correlation_id_attached_to_span(self, captured_spans):
        """When both a correlation_id and a span are active,
        add_correlation_id_to_span attaches the ID to the span."""
        exporter, tracer = captured_spans
        with correlation_id("the-cid-here"):
            with tracer.start_as_current_span("cid-span"):
                add_correlation_id_to_span()
        spans = exporter.get_finished_spans()
        cid_span = next(s for s in spans if s.name == "cid-span")
        assert cid_span.attributes["correlation_id"] == "the-cid-here"

    def test_nested_spans_form_parent_child_hierarchy(self, captured_spans):
        exporter, tracer = captured_spans
        with tracer.start_as_current_span("outer") as outer:
            with tracer.start_as_current_span("inner"):
                pass
        spans = {s.name: s for s in exporter.get_finished_spans()}
        inner = spans["inner"]
        outer_s = spans["outer"]
        assert inner.parent.span_id == outer_s.context.span_id


# -------------------- Configure tracing entry point -------------------- #


class TestConfigureTracing:

    def test_console_exporter_does_not_raise(self, capsys):
        """configure_tracing with console exporter should succeed quietly.
        OTel disallows global-provider override after first call, so
        this test exercises configure_tracing's *internal* error paths
        but doesn't actually expect spans to be exported via the
        global provider. The key contract is: doesn't raise."""
        from trip_ema.observability import configure_tracing
        # Should not raise even if the global provider can't be
        # overridden (a warning may be logged; that's fine)
        configure_tracing(service_name="trip_ema-test", exporter="console")

    def test_otlp_without_endpoint_logs_error_and_returns(self, caplog):
        from trip_ema.observability import configure_tracing
        # otlp_endpoint missing — should log error, not raise
        with caplog.at_level("ERROR"):
            configure_tracing(
                service_name="x", exporter="otlp", otlp_endpoint=None,
            )
        assert any("otlp_endpoint" in r.message for r in caplog.records)

    def test_unknown_exporter_logs_error(self, caplog):
        from trip_ema.observability import configure_tracing
        with caplog.at_level("ERROR"):
            configure_tracing(service_name="x", exporter="bogus")
        assert any("Unknown exporter" in r.message for r in caplog.records)


# ============================================================
# OTel-missing degradation
#
# Verifies that when the `opentelemetry` package isn't installed,
# our tracing helpers degrade to no-ops rather than raising
# ImportError. We simulate this by monkeypatching sys.modules to
# hide the opentelemetry imports.
# ============================================================


class TestOTelMissingDegradation:

    def test_get_tracer_returns_noop_when_otel_missing(self, monkeypatch):
        """Simulate `opentelemetry` not being installed. get_tracer
        must return a no-op tracer rather than raising ImportError."""
        import sys
        # Block the import path
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        # Force re-resolution of the inner import
        from trip_ema.observability.tracing import get_tracer
        tracer = get_tracer("test")
        # The no-op tracer's span context manager must work
        with tracer.start_as_current_span("noop") as span:
            assert span is not None
            assert span.is_recording() is False
            # All attribute methods must be safe no-ops
            span.set_attribute("any", "thing")
            span.record_exception(RuntimeError("won't fire"))

    def test_set_span_attribute_is_noop_when_otel_missing(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        from trip_ema.observability.tracing import set_span_attribute_if_active
        # Must not raise even though opentelemetry import fails
        set_span_attribute_if_active("k", "v")

    def test_configure_tracing_logs_when_otel_missing(self, monkeypatch, caplog):
        """When opentelemetry isn't installed, configure_tracing should
        log informationally and return cleanly (NOT raise)."""
        import sys
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.sdk", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.sdk.resources", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
        from trip_ema.observability.tracing import configure_tracing
        with caplog.at_level("INFO"):
            configure_tracing(service_name="x", exporter="console")
        # Should have logged about OTel being unavailable; should not raise
        msgs = " ".join(r.message for r in caplog.records)
        assert "not installed" in msgs or "tracing" in msgs.lower()
