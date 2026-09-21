"""Simulated support agent that emits real OpenTelemetry traces and metrics.

No LLM or API key needed: model calls and tools are faked with fixed token
counts and short sleeps, so the telemetry shape is the point.

    python demo.py                            # one run, trace printed as a tree
    python demo.py --scenario loop            # a run that trips the reasoning-loop alert
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python demo.py --runs 200 --mix
"""

import argparse
import random
import time
import uuid

from agent_otel import setup
from agent_otel.mcp import mcp_tool_call, serve_tools_call
from agent_otel.tracing import RunStats, ToolError, agent_run, llm_call, tool_call

MODEL = "claude-sonnet-4-6"
AGENT = "support-agent"


def fake_llm(stats: RunStats, input_tokens: int, output_tokens: int, seconds: float) -> None:
    with llm_call(stats, MODEL) as usage:
        time.sleep(seconds)
        usage.input_tokens, usage.output_tokens = input_tokens, output_tokens


def crm_lookup_handler(fail: bool):
    def handler(arguments: dict) -> dict:
        time.sleep(0.25)
        if fail:
            raise ToolError("timeout", "CRM did not answer within 250 ms")
        return {"customer": arguments["customer_id"], "plan": "enterprise"}

    return handler


def crm_lookup(stats: RunStats, request_id: int, fail: bool) -> dict:
    """An MCP tool call: the tool runs on the (simulated) CRM MCP server."""
    with mcp_tool_call(stats, "crm_lookup", {"customer_id": "C-1042"}, request_id) as request:
        return serve_tools_call(request, crm_lookup_handler(fail))


def local_tool(stats: RunStats, name: str, call_id: str, seconds: float) -> None:
    with tool_call(stats, name, call_id):
        time.sleep(seconds)


def run(scenario: str) -> RunStats:
    with agent_run(AGENT, MODEL, conversation_id=f"conv_{uuid.uuid4().hex[:12]}") as stats:
        fake_llm(stats, 2_900, 180, 0.35)  # plan
        local_tool(stats, "search_docs", "call_1", 0.3)

        if scenario == "loop":
            # The agent keeps searching and never converges: no error, just cost.
            for i in range(2, 15):
                fake_llm(stats, 3_400, 120, 0.05)
                local_tool(stats, "search_docs", f"call_{i}", 0.05)
            fake_llm(stats, 9_100, 1_870, 0.2)
            return stats

        try:
            crm_lookup(stats, request_id=1, fail=scenario == "tool-failure")
        except ToolError:
            stats.replans += 1
            fake_llm(stats, 6_420, 260, 0.3)  # re-plan after the timeout
            crm_lookup(stats, request_id=2, fail=False)

        local_tool(stats, "pricing_api", "call_3", 0.2)
        fake_llm(stats, 9_100, 1_870, 0.5)  # final answer
    return stats


def print_tree() -> None:
    spans = sorted(setup.finished_spans(), key=lambda s: s.start_time)
    children: dict[int | None, list] = {}
    for s in spans:
        children.setdefault(s.parent.span_id if s.parent else None, []).append(s)

    def walk(span, depth: int) -> None:
        ms = (span.end_time - span.start_time) / 1e6
        service = span.resource.attributes["service.name"]
        failed = span.attributes.get("error.type")
        mark = f"  x {failed}" if failed else ""
        print(f"{'  ' * depth}{span.name:<{44 - 2 * depth}} {ms:7.0f} ms  [{service}]{mark}")
        for child in children.get(span.context.span_id, []):
            walk(child, depth + 1)

    for root in children.get(None, []):
        print(f"\ntrace {root.context.trace_id:032x}")
        walk(root, 0)
        keys = [k for k in root.attributes if k.startswith(("gen_ai.usage", "app."))]
        for key in keys:
            print(f"    {key} = {root.attributes[key]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=["tool-failure", "normal", "loop"], default="tool-failure")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--mix", action="store_true", help="random scenarios: mostly normal, some failures and loops")
    args = parser.parse_args()

    setup.setup()
    try:
        for _ in range(args.runs):
            scenario = args.scenario
            if args.mix:
                scenario = random.choices(["normal", "tool-failure", "loop"], weights=[80, 15, 5])[0]
            stats = run(scenario)
            print(f"{scenario:<13} tool_calls={stats.tool_calls:<3} cost=${stats.cost_usd:.4f}")
    finally:
        setup.shutdown()
    if setup.finished_spans():
        print_tree()


if __name__ == "__main__":
    main()
