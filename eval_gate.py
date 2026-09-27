"""Run the golden trajectories and fail the build when one regresses.

    python eval_gate.py                 # run every case in evals/golden.json
    python eval_gate.py --verbose       # print every check, not just failures
    python eval_gate.py --set other.json

Each case runs the demo agent, reads back the spans that run emitted, and grades
the trajectory. Nothing here calls a model, so it is fast enough to sit in CI on
every pull request.

Cases marked `expect_fail` are known-bad trajectories. They must fail, and the
gate reports an error if one starts passing — otherwise a broken evaluator looks
exactly like a healthy suite.
"""

import argparse
import json
import pathlib
import sys

import demo
from agent_otel import setup
from agent_otel.evals import Case, from_spans, grade

GOLDEN = pathlib.Path(__file__).parent / "evals" / "golden.json"


def load_cases(path: pathlib.Path) -> list[Case]:
    raw = json.loads(path.read_text())
    return [Case.from_dict(case) for case in raw["cases"]]


def run_case(case: Case):
    """Run one scenario and grade the trace it produced."""
    setup.clear()
    demo.run(case.scenario)
    return grade(from_spans(setup.finished_spans()), case)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", type=pathlib.Path, default=GOLDEN, help="golden set to run")
    parser.add_argument("--verbose", action="store_true", help="print passing checks too")
    args = parser.parse_args()

    setup.setup()
    regressions = []
    try:
        for case in load_cases(args.set):
            result = run_case(case)
            as_expected = result.passed is not case.expect_fail
            mark = "ok  " if as_expected else "FAIL"
            note = "  (expected to fail)" if case.expect_fail else ""
            print(f"{mark}  {case.name}{note}")

            for verdict in result.verdicts:
                if args.verbose or not verdict.passed:
                    print(f"        {'.' if verdict.passed else 'x'} {verdict.check:<10} {verdict.detail}")

            if not as_expected:
                regressions.append(case.name)
    finally:
        setup.shutdown()

    print()
    if regressions:
        print(f"{len(regressions)} case(s) did not behave as the golden set says they should:")
        for name in regressions:
            print(f"  - {name}")
        return 1
    print("all golden trajectories behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
