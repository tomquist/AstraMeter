"""Steering-quality evaluation for the active-control loop.

Runs CT002 against the simulated battery plant under a mock clock, so hours of
household activity take seconds. Each scenario (``eval_scenarios``) yields
reaction / oscillation / energy / cost metrics (``eval_metrics``), averaged
over several seeds run in parallel; ``--compare`` renders the base-vs-head
Markdown CI posts on PRs (``eval_compare``), ``--html`` the interactive report
(``eval_report``)::

    uv run python -m astrameter.simulator.evaluation --list
    uv run python -m astrameter.simulator.evaluation --scenario two_venus/fair \\
        --set balance_deadband=25 --seeds 10 --json head.json
    uv run python -m astrameter.simulator.evaluation --compare base.json \\
        --input head.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .eval_compare import _merge_seeds, render_markdown_compare, render_text
from .eval_harness import _run_tasks, run_scenario
from .eval_report import render_html_report
from .eval_scenarios import build_scenarios

__all__ = ["build_scenarios", "main", "run_scenario"]


def _parse_overrides(pairs: list[str]) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--set expects KEY=VALUE, got {pair!r}")
        overrides[key.strip()] = float(value)
    return overrides


def _run_all(
    names: list[str], seeds: list[int], overrides: dict[str, float]
) -> list[dict]:
    """Run the selected scenarios across all *seeds* concurrently, then collapse
    each scenario's per-seed results into one seed-averaged row."""
    scenarios = build_scenarios()
    unknown = [n for n in names if n not in scenarios]
    if unknown:
        raise SystemExit(
            f"unknown scenario(s): {', '.join(unknown)}; "
            f"available: {', '.join(sorted(scenarios))}"
        )
    selected = names or sorted(scenarios)
    raw = _run_tasks([(name, seed) for name in selected for seed in seeds], overrides)
    results = []
    for name in selected:
        merged = _merge_seeds([raw[(name, seed)] for seed in seeds])
        print(render_text([merged]), file=sys.stderr, flush=True)
        results.append(merged)
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m astrameter.simulator.evaluation",
        description="Steering-quality evaluation for the active-control loop.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run only this scenario (repeatable; default: all)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1,
        help="first seed (default: 1); with --seeds N, runs seed..seed+N-1",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=5,
        metavar="N",
        help="number of seeds to run per scenario and average over, in "
        "parallel across CPU cores (default: 5; 1 disables seed averaging)",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a CT002/balancer config knob, e.g. balance_deadband=25",
    )
    parser.add_argument("--json", metavar="PATH", help="write results JSON to PATH")
    parser.add_argument(
        "--input",
        metavar="PATH",
        help="load results from PATH instead of running scenarios",
    )
    parser.add_argument(
        "--compare",
        metavar="BASELINE_JSON",
        help="compare results against a baseline JSON",
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        help="write the self-contained interactive HTML report to PATH "
        "(uses --compare's baseline when given, else head-only)",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, sc in sorted(build_scenarios().items()):
            print(f"{name:<28} {sc.description}")
        return

    if args.input:
        with open(args.input) as fh:
            results = json.load(fh)
    else:
        if args.seeds < 1:
            raise SystemExit("--seeds must be >= 1")
        overrides = _parse_overrides(args.overrides)
        seeds = list(range(args.seed, args.seed + args.seeds))
        results = _run_all(args.scenario, seeds, overrides)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)

    base = None
    if args.compare:
        with open(args.compare) as fh:
            base = json.load(fh)
        print(render_markdown_compare(base, results, report_available=bool(args.html)))
    elif not args.input:
        print(render_text(results))

    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html_report(base, results))


if __name__ == "__main__":
    main()
