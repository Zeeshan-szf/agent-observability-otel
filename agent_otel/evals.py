"""Grading an agent run from the traces it already emits.

A tool-calling agent fails in four ways, and a single-turn eval catches only the
first:

    selection   did it reach for the right tools, in a sane order?
    arguments   did it pass the arguments those tools needed?
    use         did it do anything with what came back?
    recovery    when a tool failed, did it recover instead of stopping?

Cost is the fifth. A run that retries a tool five times and still answers
correctly is a defect, not a pass, and no answer-matching eval will ever see it.

Everything below reads the spans that tracing.py and mcp.py already produce.
There is no second pipeline to keep in sync: the trace *is* the eval dataset.
"""

from dataclasses import dataclass, field

TOOL_OPERATION = "execute_tool"


@dataclass(frozen=True)
class Step:
    """One tool call, as read back from its span."""

    tool: str
    ok: bool
    error: str | None
    argument_keys: tuple[str, ...]
    used: bool  # a model call started after this tool returned


@dataclass
class Trajectory:
    """What an agent actually did, reconstructed from one trace."""

    agent: str
    steps: list[Step] = field(default_factory=list)
    replans: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0

    @property
    def tools(self) -> list[str]:
        return [step.tool for step in self.steps]

    @property
    def failures(self) -> list[Step]:
        return [step for step in self.steps if not step.ok]


def from_spans(spans) -> Trajectory:
    """Build a Trajectory from finished spans.

    Only the agent's own spans count. The MCP server emits a second span for
    every tools/call, on the same trace but under its own service.name, and
    counting both would double every MCP tool call.
    """
    roots = [s for s in spans if s.name.startswith("invoke_agent ")]
    if not roots:
        raise ValueError("no invoke_agent span: nothing to grade")
    root = min(roots, key=lambda s: s.start_time)
    service = root.resource.attributes["service.name"]
    mine = [s for s in spans if s.resource.attributes["service.name"] == service]

    chat_starts = sorted(s.start_time for s in mine if s.name.startswith("chat "))
    tool_spans = sorted(
        (s for s in mine if s.attributes.get("gen_ai.operation.name") == TOOL_OPERATION),
        key=lambda s: s.start_time,
    )

    steps = []
    for span in tool_spans:
        error = span.attributes.get("error.type")
        keys = span.attributes.get("app.tool.argument_keys") or ""
        steps.append(
            Step(
                tool=span.attributes["gen_ai.tool.name"],
                ok=error is None,
                error=error,
                argument_keys=tuple(k for k in keys.split(",") if k),
                used=any(start > span.end_time for start in chat_starts),
            )
        )

    return Trajectory(
        agent=root.attributes["gen_ai.agent.name"],
        steps=steps,
        replans=root.attributes.get("app.agent.replans", 0),
        tool_calls=root.attributes.get("app.agent.tool_calls", 0),
        cost_usd=root.attributes.get("app.agent.cost_usd", 0.0),
    )


@dataclass
class Case:
    """One golden case: the scenario to run, and what a good trajectory looks like.

    `expect_tools` is an ordered subsequence, not an exact list. Pinning the
    exact sequence makes the suite fail every time the agent finds a shorter
    route, which trains people to delete the eval.
    """

    name: str
    scenario: str
    expect_tools: list[str] = field(default_factory=list)
    expect_arguments: dict[str, list[str]] = field(default_factory=dict)
    max_tool_calls: int = 8
    max_cost_usd: float = 0.20
    must_recover: bool = False
    expect_fail: bool = False  # a known-bad trajectory the gate must catch

    @classmethod
    def from_dict(cls, raw: dict) -> "Case":
        return cls(**raw)


@dataclass(frozen=True)
class Verdict:
    check: str
    passed: bool
    detail: str


@dataclass
class Result:
    case: Case
    trajectory: Trajectory
    verdicts: list[Verdict]

    @property
    def passed(self) -> bool:
        return all(v.passed for v in self.verdicts)

    @property
    def failed_checks(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.passed]


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    it = iter(actual)
    return all(tool in it for tool in expected)


def grade(trajectory: Trajectory, case: Case) -> Result:
    """Score one trajectory against one case. Every check names what it saw."""
    verdicts = []

    verdicts.append(
        Verdict(
            "selection",
            _is_subsequence(case.expect_tools, trajectory.tools),
            f"expected {case.expect_tools} in order, called {trajectory.tools}",
        )
    )

    missing = {}
    for tool, required in case.expect_arguments.items():
        called = [s for s in trajectory.steps if s.tool == tool]
        if not called:
            missing[tool] = required
            continue
        absent = [key for key in required if key not in called[0].argument_keys]
        if absent:
            missing[tool] = absent
    verdicts.append(
        Verdict("arguments", not missing, "all required arguments passed" if not missing else f"missing {missing}")
    )

    unused = [s.tool for s in trajectory.steps if s.ok and not s.used]
    verdicts.append(
        Verdict(
            "use",
            not unused,
            "every tool result was read" if not unused else f"nothing followed {unused}",
        )
    )

    if case.must_recover:
        failed = trajectory.failures
        recovered = any(
            step.ok and any(f.tool == step.tool for f in failed) for step in trajectory.steps[len(failed) :]
        )
        verdicts.append(
            Verdict(
                "recovery",
                bool(failed) and recovered and trajectory.replans > 0,
                f"{len(failed)} failed call(s), {trajectory.replans} replan(s), retried successfully: {recovered}",
            )
        )

    within_calls = trajectory.tool_calls <= case.max_tool_calls
    within_cost = trajectory.cost_usd <= case.max_cost_usd
    verdicts.append(
        Verdict(
            "cost",
            within_calls and within_cost,
            f"{trajectory.tool_calls} tool calls (max {case.max_tool_calls}), "
            f"${trajectory.cost_usd:.4f} (max ${case.max_cost_usd:.2f})",
        )
    )

    return Result(case=case, trajectory=trajectory, verdicts=verdicts)
