"""OpenTelemetry metrics and traces.

Metrics (all prefixed opsrelay_):

    incidents_total{source}                 incidents opened
    incidents_closed_total{outcome}         resolved | escalated
    stage_duration_seconds{stage}           time spent in each lifecycle status (open, triaging, ...)
    agent_calls_total{agent,outcome}        delegations: ok | contract_violation | unavailable
    agent_latency_seconds{agent}            delegation round trip
    policy_decisions_total{decision}        ALLOW | APPROVAL_REQUIRED | DENY
    executions_total{outcome}               ok | failed | replayed | reconciled
    execution_latency_seconds{action}
    verification_failures_total
    alerts_total{source,outcome}            created | deduplicated | correlated | resolved_noted | ignored

Traces: spans for coordination runs, agent delegations, executions and alert intake, with the
incident id as an attribute (Strands adds spans for model and tool calls underneath).

Export: with OTEL_EXPORTER_OTLP_ENDPOINT set and the exporter installed (pip install
"opsrelay[otel]"), metrics and traces go to that OTLP endpoint (an ADOT/OpenTelemetry collector,
CloudWatch, Grafana, Honeycomb...). Otherwise the API calls are no-ops. The same numbers, computed
from the audit log, are available without any backend: see opsrelay.ops_metrics.
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace

log = logging.getLogger(__name__)
_lock = threading.RLock()  # re-entrant: creating an instrument configures the provider first
_provider: Any = None  # a MeterProvider we own (OTLP, or a test reader); else the global one
_instruments: dict[str, Any] = {}
_configured = False


def configure(reader: Any = None) -> None:
    """Set up export once. `reader` (e.g. InMemoryMetricReader) is for tests."""
    global _provider, _configured
    with _lock:
        if reader is not None:
            from opentelemetry.sdk.metrics import MeterProvider

            _provider = MeterProvider(metric_readers=[reader])
            _instruments.clear()
            _configured = True
            return
        if _configured:
            return
        _configured = True
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            log.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but the exporter isn't installed: pip install 'opsrelay[otel]'"
            )
            return
        resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "opsrelay")})
        _provider = MeterProvider(
            resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
        )
        if not isinstance(trace.get_tracer_provider(), TracerProvider):  # don't replace one set up by AgentCore/ADOT
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(tracer_provider)


def reset() -> None:
    """Forget test configuration."""
    global _provider, _configured
    with _lock:
        _provider, _configured = None, False
        _instruments.clear()


def _meter() -> Any:
    configure()
    return (_provider or metrics.get_meter_provider()).get_meter("opsrelay")


def _instrument(kind: str, name: str, unit: str = "1", description: str = "") -> Any:
    key = f"{kind}:{name}"
    with _lock:
        if key not in _instruments:
            meter = _meter()
            create = meter.create_counter if kind == "counter" else meter.create_histogram
            _instruments[key] = create(f"opsrelay_{name}", unit=unit, description=description)
        return _instruments[key]


def count(name: str, **attributes: str) -> None:
    try:
        _instrument("counter", name).add(1, attributes)
    except Exception:  # noqa: BLE001 - telemetry must never break the platform
        log.debug("metric %s failed", name, exc_info=True)


def observe(name: str, value: float, unit: str = "s", **attributes: str) -> None:
    try:
        _instrument("histogram", name, unit).record(value, attributes)
    except Exception:  # noqa: BLE001
        log.debug("metric %s failed", name, exc_info=True)


@contextmanager
def span(name: str, **attributes: Any):
    configure()
    with trace.get_tracer("opsrelay").start_as_current_span(name, attributes=attributes) as s:
        yield s
