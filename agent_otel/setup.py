"""OpenTelemetry providers for the demo: one for the agent, one for the MCP server.

With OTEL_EXPORTER_OTLP_ENDPOINT set (e.g. http://localhost:4317) spans and
metrics go to the collector. Without it, spans are kept in memory and printed
as a tree when the program exits, so the demo runs with no infrastructure.
"""

import os

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# The reasoning-loop alert reads the le="12" bucket, so 12 must be an edge.
VIEWS = [
    View(
        instrument_name="app.agent.run.tool_calls",
        aggregation=ExplicitBucketHistogramAggregation([1, 2, 4, 8, 12, 16, 24, 32]),
    ),
    View(
        instrument_name="app.agent.run.cost_usd",
        aggregation=ExplicitBucketHistogramAggregation([0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2]),
    ),
]

_memory = InMemorySpanExporter()
_providers: list[TracerProvider] = []
server_tracer_provider: TracerProvider | None = None


def _span_processor():
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return BatchSpanProcessor(OTLPSpanExporter())
    return SimpleSpanProcessor(_memory)


def setup(agent_service: str = "support-agent", server_service: str = "crm-mcp-server") -> None:
    """Install the global (agent) providers and a separate provider for the MCP server."""
    global server_tracer_provider

    agent = TracerProvider(resource=Resource.create({"service.name": agent_service}))
    agent.add_span_processor(_span_processor())
    trace.set_tracer_provider(agent)

    # In production the MCP server is its own process; a second provider with its
    # own service.name shows the trace crossing that boundary.
    server_tracer_provider = TracerProvider(resource=Resource.create({"service.name": server_service}))
    server_tracer_provider.add_span_processor(_span_processor())
    _providers[:] = [agent, server_tracer_provider]

    readers = []
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5000))
    metrics.set_meter_provider(
        MeterProvider(
            resource=Resource.create({"service.name": agent_service}),
            metric_readers=readers,
            views=VIEWS,
        )
    )


def shutdown() -> None:
    for provider in _providers:
        provider.shutdown()
    metrics.get_meter_provider().shutdown()


def finished_spans():
    return _memory.get_finished_spans()
