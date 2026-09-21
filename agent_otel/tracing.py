"""Agent spans and metrics that follow the OpenTelemetry GenAI semantic conventions.

Span names and gen_ai.* attributes come from
https://github.com/open-telemetry/semantic-conventions-genai (status: Development):

    invoke_agent {agent name}   one span per agent run (the trace root)
    chat {model}                one span per model call
    execute_tool {tool name}    one span per local tool call

Cost, tool calls per run and replans have no convention yet, so they use the
app.* namespace. Swap it for your own.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

PROVIDER = "anthropic"

# USD per million tokens (input, output). Keep this in config in real code.
PRICES = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

tracer = trace.get_tracer("agent_otel")


class ToolError(Exception):
    """A tool call that failed. `kind` becomes the span's error.type."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunStats:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    replans: int = 0
    cost_usd: float = 0.0

    def add(self, model: str, usage: Usage) -> None:
        price_in, price_out = PRICES[model]
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cost_usd += (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000


@cache
def _instruments():
    meter = metrics.get_meter("agent_otel")
    return {
        # Standard GenAI client metric.
        "tokens": meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}", description="Tokens used per model call"
        ),
        # No convention yet: app.* namespace.
        "run_cost": meter.create_histogram(
            "app.agent.run.cost_usd", description="Estimated cost of one agent run in USD"
        ),
        "run_tool_calls": meter.create_histogram(
            "app.agent.run.tool_calls", description="Tool calls made during one agent run"
        ),
        "tool_calls": meter.create_counter("app.agent.tool.calls", description="Tool calls by outcome"),
    }


def count_tool_call(tool: str, outcome: str) -> None:
    _instruments()["tool_calls"].add(1, {"gen_ai.tool.name": tool, "outcome": outcome})


@contextmanager
def agent_run(agent_name: str, model: str, conversation_id: str):
    """Root span for one agent run. Token totals and cost land on it when the run ends."""
    stats = RunStats()
    with tracer.start_as_current_span(f"invoke_agent {agent_name}", kind=SpanKind.INTERNAL) as span:
        span.set_attributes(
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": PROVIDER,
                "gen_ai.agent.name": agent_name,
                "gen_ai.request.model": model,
                "gen_ai.conversation.id": conversation_id,
            }
        )
        try:
            yield stats
        finally:
            span.set_attributes(
                {
                    "gen_ai.usage.input_tokens": stats.input_tokens,
                    "gen_ai.usage.output_tokens": stats.output_tokens,
                    "app.agent.tool_calls": stats.tool_calls,
                    "app.agent.replans": stats.replans,
                    "app.agent.cost_usd": round(stats.cost_usd, 6),
                }
            )
            labels = {"gen_ai.agent.name": agent_name, "gen_ai.request.model": model}
            _instruments()["run_cost"].record(stats.cost_usd, labels)
            _instruments()["run_tool_calls"].record(stats.tool_calls, labels)


@contextmanager
def llm_call(stats: RunStats, model: str):
    """Span for one model call. Fill in the yielded Usage from the provider's response."""
    with tracer.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT) as span:
        span.set_attributes(
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": PROVIDER,
                "gen_ai.request.model": model,
            }
        )
        usage = Usage()
        yield usage
        span.set_attributes(
            {
                "gen_ai.usage.input_tokens": usage.input_tokens,
                "gen_ai.usage.output_tokens": usage.output_tokens,
            }
        )
        stats.add(model, usage)
        base = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": PROVIDER, "gen_ai.request.model": model}
        _instruments()["tokens"].record(usage.input_tokens, {**base, "gen_ai.token.type": "input"})
        _instruments()["tokens"].record(usage.output_tokens, {**base, "gen_ai.token.type": "output"})


@contextmanager
def tool_call(stats: RunStats, tool: str, call_id: str):
    """Span for one local tool call. Raise ToolError inside the block to mark it failed."""
    stats.tool_calls += 1
    with tracer.start_as_current_span(
        f"execute_tool {tool}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attributes(
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool,
                "gen_ai.tool.call.id": call_id,
                "gen_ai.tool.type": "function",
            }
        )
        try:
            yield span
        except ToolError as err:
            span.set_attribute("error.type", err.kind)
            span.set_status(Status(StatusCode.ERROR, str(err)))
            count_tool_call(tool, "error")
            raise
        count_tool_call(tool, "ok")
