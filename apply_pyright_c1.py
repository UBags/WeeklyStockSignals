"""Pyright C.1 — metrics shim type confusion (3 errors).

The metrics module exposes M.REGISTRY as CollectorRegistry |
_NoOpRegistry, but the call sites only run after prom_client was
successfully imported — meaning the no-op shim isn't reachable
at the call. Pyright can't see this; mypy already silently accepts
it. The proper fix is the queued metrics-shim refactor (make
_NoOpRegistry inherit from CollectorRegistry, or refactor both
to satisfy a Protocol). Until then, `# pyright: ignore` with TODOs.
"""
from __future__ import annotations
import pathlib

REPO = pathlib.Path.home() / "PycharmProjects" / "WeeklyStockSignals"


def patch(rel: str, old: str, new: str, *, label: str) -> None:
    p = REPO / rel
    text = p.read_text()
    n = text.count(old)
    assert n == 1, f"[{label}] expected 1 occurrence, found {n} in {rel}"
    p.write_text(text.replace(old, new))
    print(f"  ✓ {label}")


# circuit_breaker.py:94 — Gauge registry arg
patch(
    "trip_ema/notifications/circuit_breaker.py",
    label="circuit_breaker.gauge.registry.noqa",
    old=(
        "            _circuit_state_gauge = Gauge(\n"
        "                \"trip_ema_transport_circuit_state\",\n"
        "                \"Current circuit-breaker state per transport \"\n"
        "                \"(1 = active state, 0 otherwise; one row per state).\",\n"
        "                labelnames=(\"transport\", \"state\"),\n"
        "                registry=M.REGISTRY,\n"
        "            )\n"
    ),
    new=(
        "            _circuit_state_gauge = Gauge(\n"
        "                \"trip_ema_transport_circuit_state\",\n"
        "                \"Current circuit-breaker state per transport \"\n"
        "                \"(1 = active state, 0 otherwise; one row per state).\",\n"
        "                labelnames=(\"transport\", \"state\"),\n"
        "                # TODO: queued — refactor _NoOpRegistry to inherit\n"
        "                # CollectorRegistry. Until then, no-op shim is\n"
        "                # unreachable on this branch (prom_client imported).\n"
        "                registry=M.REGISTRY,  # pyright: ignore[reportArgumentType]\n"
        "            )\n"
    ),
)

# circuit_breaker.py:100 — Counter registry arg
patch(
    "trip_ema/notifications/circuit_breaker.py",
    label="circuit_breaker.counter.registry.noqa",
    old=(
        "            _circuit_transition_counter = Counter(\n"
        "                \"trip_ema_transport_circuit_transitions_total\",\n"
        "                \"Circuit-breaker state transitions per transport.\",\n"
        "                labelnames=(\"transport\", \"from_state\", \"to_state\"),\n"
        "                registry=M.REGISTRY,\n"
        "            )\n"
    ),
    new=(
        "            _circuit_transition_counter = Counter(\n"
        "                \"trip_ema_transport_circuit_transitions_total\",\n"
        "                \"Circuit-breaker state transitions per transport.\",\n"
        "                labelnames=(\"transport\", \"from_state\", \"to_state\"),\n"
        "                # TODO: queued — refactor _NoOpRegistry to inherit\n"
        "                # CollectorRegistry. Until then, no-op shim is\n"
        "                # unreachable on this branch (prom_client imported).\n"
        "                registry=M.REGISTRY,  # pyright: ignore[reportArgumentType]\n"
        "            )\n"
    ),
)

# metrics_server.py:83 — start_http_server registry arg
patch(
    "trip_ema/observability/metrics_server.py",
    label="metrics_server.registry.noqa",
    old="        http_server, thread = start_http_server(port, registry=REGISTRY)\n",
    new=(
        "        # TODO: queued — refactor _NoOpRegistry to satisfy Collector.\n"
        "        # Until then, the no-op shim is unreachable here (we just\n"
        "        # imported prometheus_client successfully).\n"
        "        http_server, thread = start_http_server(port, registry=REGISTRY)  # pyright: ignore[reportArgumentType]\n"
    ),
)

print("Done.")