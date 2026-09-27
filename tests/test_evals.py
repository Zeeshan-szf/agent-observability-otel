"""Trajectories read back from traces, and the gate that grades them."""

import unittest

import demo
import eval_gate
from agent_otel import setup
from agent_otel.evals import Case, Step, Trajectory, from_spans, grade


def trajectory_for(scenario: str) -> Trajectory:
    setup.clear()
    demo.run(scenario)
    return from_spans(setup.finished_spans())


class TrajectoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup.setup()

    def test_mcp_tool_call_is_counted_once_not_twice(self):
        """The MCP server emits its own span for every tools/call, on the same trace."""
        trajectory = trajectory_for("normal")
        self.assertEqual(trajectory.tools, ["search_docs", "crm_lookup", "pricing_api"])
        self.assertEqual(trajectory.tool_calls, 3)

    def test_failed_call_and_its_retry_both_appear(self):
        trajectory = trajectory_for("tool-failure")
        self.assertEqual(trajectory.tools, ["search_docs", "crm_lookup", "crm_lookup", "pricing_api"])
        self.assertEqual([step.error for step in trajectory.failures], ["timeout"])
        self.assertEqual(trajectory.replans, 1)

    def test_argument_names_are_recorded_and_values_are_not(self):
        trajectory = trajectory_for("normal")
        crm = next(step for step in trajectory.steps if step.tool == "crm_lookup")
        self.assertEqual(crm.argument_keys, ("customer_id",))
        spans = [s for s in setup.finished_spans() if s.attributes.get("app.tool.argument_keys")]
        self.assertTrue(spans)
        for span in spans:
            self.assertNotIn("C-1042", str(dict(span.attributes)))

    def test_runs_with_no_agent_span_are_rejected(self):
        with self.assertRaises(ValueError):
            from_spans([])


class GradingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup.setup()
        cls.cases = {case.scenario: case for case in eval_gate.load_cases(eval_gate.GOLDEN)}

    def test_golden_normal_run_passes_every_check(self):
        result = grade(trajectory_for("normal"), self.cases["normal"])
        self.assertTrue(result.passed, [v.detail for v in result.failed_checks])

    def test_recovery_check_sees_the_replan(self):
        result = grade(trajectory_for("tool-failure"), self.cases["tool-failure"])
        recovery = next(v for v in result.verdicts if v.check == "recovery")
        self.assertTrue(recovery.passed, recovery.detail)

    def test_reasoning_loop_is_caught_on_selection_and_cost(self):
        """The loop answers correctly. Only the trajectory shows it cost five times as much."""
        result = grade(trajectory_for("loop"), self.cases["loop"])
        self.assertFalse(result.passed)
        self.assertEqual({v.check for v in result.failed_checks}, {"selection", "cost"})

    def test_expected_tools_are_a_subsequence_not_an_exact_match(self):
        """A shorter route that still hits the expected tools in order must pass."""
        trajectory = Trajectory(
            agent="support-agent",
            steps=[
                Step("search_docs", True, None, ("query",), True),
                Step("audit_log", True, None, (), True),
                Step("crm_lookup", True, None, ("customer_id",), True),
            ],
            tool_calls=3,
            cost_usd=0.01,
        )
        case = Case(name="t", scenario="normal", expect_tools=["search_docs", "crm_lookup"], max_tool_calls=5)
        self.assertTrue(grade(trajectory, case).passed)

    def test_right_tools_in_the_wrong_order_fail_selection(self):
        trajectory = Trajectory(
            agent="support-agent",
            steps=[
                Step("crm_lookup", True, None, ("customer_id",), True),
                Step("search_docs", True, None, ("query",), True),
            ],
            tool_calls=2,
            cost_usd=0.01,
        )
        case = Case(name="t", scenario="normal", expect_tools=["search_docs", "crm_lookup"], max_tool_calls=5)
        self.assertEqual([v.check for v in grade(trajectory, case).failed_checks], ["selection"])

    def test_missing_argument_is_named(self):
        trajectory = Trajectory(
            agent="support-agent",
            steps=[Step("crm_lookup", True, None, (), True)],
            tool_calls=1,
            cost_usd=0.01,
        )
        case = Case(name="t", scenario="normal", expect_arguments={"crm_lookup": ["customer_id"]})
        arguments = next(v for v in grade(trajectory, case).verdicts if v.check == "arguments")
        self.assertFalse(arguments.passed)
        self.assertIn("customer_id", arguments.detail)

    def test_tool_result_nothing_reads_fails_use(self):
        """An agent that gathers data and then stops has not answered anyone."""
        trajectory = Trajectory(
            agent="support-agent",
            steps=[Step("search_docs", True, None, ("query",), False)],
            tool_calls=1,
            cost_usd=0.01,
        )
        use = next(v for v in grade(trajectory, Case(name="t", scenario="normal")).verdicts if v.check == "use")
        self.assertFalse(use.passed)
        self.assertIn("search_docs", use.detail)


class GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup.setup()

    def test_every_golden_case_behaves_as_the_set_says(self):
        for case in eval_gate.load_cases(eval_gate.GOLDEN):
            with self.subTest(case=case.name):
                result = eval_gate.run_case(case)
                self.assertEqual(result.passed, not case.expect_fail, [v.detail for v in result.failed_checks])


if __name__ == "__main__":
    unittest.main()
