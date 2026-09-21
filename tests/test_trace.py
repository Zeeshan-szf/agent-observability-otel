"""The demo's traces follow the GenAI and MCP conventions and cross into the MCP server."""

import unittest

import demo
from agent_otel import setup


class TraceShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup.setup()
        cls.stats = demo.run("tool-failure")
        cls.spans = setup.finished_spans()

    def by_name(self, name):
        return [s for s in self.spans if s.name == name]

    def test_root_span_carries_usage_and_cost(self):
        (root,) = self.by_name("invoke_agent support-agent")
        self.assertEqual(root.attributes["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(root.attributes["gen_ai.provider.name"], "anthropic")
        self.assertEqual(root.attributes["gen_ai.usage.input_tokens"], 18_420)
        self.assertEqual(root.attributes["gen_ai.usage.output_tokens"], 2_310)
        self.assertEqual(root.attributes["app.agent.tool_calls"], 4)
        self.assertAlmostEqual(root.attributes["app.agent.cost_usd"], 0.08991)

    def test_failed_tool_call_is_marked(self):
        failed = [s for s in self.spans if s.attributes.get("error.type") == "timeout"]
        self.assertEqual({s.resource.attributes["service.name"] for s in failed}, {"support-agent", "crm-mcp-server"})

    def test_mcp_server_span_joins_the_agent_trace(self):
        client = [s for s in self.by_name("tools/call crm_lookup") if s.resource.attributes["service.name"] == "support-agent"]
        server = [s for s in self.by_name("tools/call crm_lookup") if s.resource.attributes["service.name"] == "crm-mcp-server"]
        self.assertEqual(len(server), 2)
        for srv, cli in zip(server, client):
            self.assertEqual(srv.context.trace_id, cli.context.trace_id)
            self.assertEqual(srv.parent.span_id, cli.context.span_id)
            self.assertEqual(srv.attributes["mcp.method.name"], "tools/call")


if __name__ == "__main__":
    unittest.main()
