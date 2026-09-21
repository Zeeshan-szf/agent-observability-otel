"""Trace context across an MCP tools/call, per the MCP 2026-07-28 spec and the
OpenTelemetry MCP semantic conventions.

The client injects W3C trace context (traceparent, tracestate, baggage) into the
request's params._meta. The server extracts it and uses it as the remote parent,
so one trace runs from the agent into the tool server.

Check whether your MCP SDK already does this before adding it yourself.
"""

from contextlib import contextmanager
from typing import Any, Callable

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import setup
from .tracing import RunStats, ToolError, count_tool_call

PROTOCOL_VERSION = "2026-07-28"

tracer = trace.get_tracer("agent_otel.mcp")


def _span_attributes(tool: str, request_id: int) -> dict[str, Any]:
    return {
        "mcp.method.name": "tools/call",
        "mcp.protocol.version": PROTOCOL_VERSION,
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool,
        "jsonrpc.request.id": str(request_id),
    }


@contextmanager
def mcp_tool_call(stats: RunStats, tool: str, arguments: dict, request_id: int):
    """Client span for an MCP tool call. Yields the JSON-RPC request to send."""
    stats.tool_calls += 1
    with tracer.start_as_current_span(
        f"tools/call {tool}", kind=SpanKind.CLIENT, record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attributes(_span_attributes(tool, request_id))
        meta: dict[str, str] = {}
        inject(meta)  # traceparent, plus tracestate/baggage when present
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": meta},
        }
        try:
            yield request
        except ToolError as err:
            span.set_attribute("error.type", err.kind)
            span.set_status(Status(StatusCode.ERROR, str(err)))
            count_tool_call(tool, "error")
            raise
        count_tool_call(tool, "ok")


def serve_tools_call(request: dict, handler: Callable[[dict], dict]) -> dict:
    """Server side: continue the caller's trace, then run the tool."""
    params = request["params"]
    parent = extract(params.get("_meta", {}))
    server_tracer = setup.server_tracer_provider.get_tracer("crm_mcp_server")
    with server_tracer.start_as_current_span(
        f"tools/call {params['name']}",
        context=parent,
        kind=SpanKind.SERVER,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attributes(_span_attributes(params["name"], request["id"]))
        try:
            return {"jsonrpc": "2.0", "id": request["id"], "result": handler(params["arguments"])}
        except ToolError as err:
            span.set_attribute("error.type", err.kind)
            span.set_status(Status(StatusCode.ERROR, str(err)))
            raise
