"""Compare two benchmark result files produced by ``benchmarks/run.py``.

Usage::

    python benchmarks/compare.py results/base.json results/head.json
    python benchmarks/compare.py results/base.json results/head.json \
        --fail-over 20

Prints a per-scenario table of latency percentiles, throughput and
memory, with percentage deltas from base to head. With ``--fail-over X``
the tool exits non-zero when any latency p50 or the throughput regresses
by more than X percent (memory is only reported, as it is noisy on CI
runners).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Latency metrics shown with base/head percentile deltas.
_LATENCY_METRICS = (
    "pair_verify_ms",
    "write_latency_ms",
    "read_latency_ms",
    "get_accessories_ms",
    "event_latency_ms",
)

_DELTA_ROWS = (
    ("p50", "p50"),
    ("p95", "p95"),
)

_ROW_WIDTH = 34


def _pct_change(base: float, head: float) -> str:
    """Format a percentage change from base to head."""
    if base == 0:
        return "n/a" if head == 0 else "+inf"
    change = (head - base) / base * 100
    return f"{change:+.1f}%"


def _print_metric_row(
    name: str, stat: str, base: float, head: float, higher_is_better: bool
) -> float:
    """Print one base/head/delta row.

    Returns the regression "badness" in percent: positive when the head
    is worse than the base, regardless of the metric's direction.
    """
    print(
        f"  {name:<{_ROW_WIDTH}} {stat:>4} {base:>10.2f} {head:>10.2f} "
        f"{_pct_change(base, head):>8}"
    )
    if base == 0:
        return 0.0
    change = (head - base) / base * 100
    return -change if higher_is_better else change


def compare(base_path: Path, head_path: Path, fail_over: float) -> int:
    """Compare two result documents; return the process exit code."""
    base = json.loads(base_path.read_text(encoding="utf-8"))
    head = json.loads(head_path.read_text(encoding="utf-8"))
    base_scenarios = base.get("scenarios", {})
    head_scenarios = head.get("scenarios", {})

    for label, document in (("base", base), ("head", head)):
        commit = document.get("meta", {}).get("commit", "?")
        timestamp = document.get("meta", {}).get("timestamp", "?")
        print(f"{label}: {commit} ({timestamp})")

    if not set(base_scenarios) & set(head_scenarios):
        print(
            "error: no common scenarios to compare "
            f"({list(base_scenarios)} vs {list(head_scenarios)})",
            file=sys.stderr,
        )
        return 2

    regressions: list[str] = []
    for name in sorted(set(base_scenarios) & set(head_scenarios)):
        base_result = base_scenarios[name]
        head_result = head_scenarios[name]
        scenario = base_result["scenario"]
        print(
            f"\nScenario: {name} "
            f"({scenario['clients']} clients / {scenario['accessories']} accessories)"
        )
        print(f"  {'metric':<{_ROW_WIDTH}} stat     base      head     delta")

        for metric in _LATENCY_METRICS:
            base_stats = base_result.get("metrics", {}).get(metric)
            head_stats = head_result.get("metrics", {}).get(metric)
            if not base_stats or not head_stats:
                print(f"  {metric:<{_ROW_WIDTH}}  missing in one of the results")
                continue
            if not base_stats.get("count") or not head_stats.get("count"):
                print(f"  {metric:<{_ROW_WIDTH}}  no samples in one of the results")
                continue
            for stat_key, label in _DELTA_ROWS:
                badness = _print_metric_row(
                    metric,
                    label,
                    base_stats[stat_key],
                    head_stats[stat_key],
                    higher_is_better=False,
                )
                if label == "p50" and badness > fail_over:
                    regressions.append(f"{name}/{metric} (p50) {badness:+.1f}% worse")

        for scalar, is_memory in (
            ("writes_per_second", False),
            ("get_accessories_response_bytes", False),
            ("accessory_build_ms", False),
            ("memory.peak_rss_bytes", True),
        ):
            if is_memory:
                base_value = base_result.get("memory", {}).get("peak_rss_bytes", 0)
                head_value = head_result.get("memory", {}).get("peak_rss_bytes", 0)
            else:
                base_value = base_result.get("scalars", {}).get(scalar, 0)
                head_value = head_result.get("scalars", {}).get(scalar, 0)
            badness = _print_metric_row(
                scalar,
                "value",
                base_value,
                head_value,
                higher_is_better=scalar == "writes_per_second",
            )
            if scalar == "writes_per_second" and badness > fail_over:
                regressions.append(f"{name}/writes_per_second {badness:+.1f}% lower")

    if fail_over >= 0:
        if regressions:
            print("\nREGRESSIONS over threshold:")
            for regression in regressions:
                print(f"  {regression}")
            return 1
        print(f"\nNo regression over {fail_over:.0f}%.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path, help="baseline result file")
    parser.add_argument("head", type=Path, help="candidate result file")
    parser.add_argument(
        "--fail-over",
        type=float,
        default=-1,
        metavar="PERCENT",
        help="exit non-zero when any latency p50 or the throughput regresses "
        "by more than PERCENT percent",
    )
    args = parser.parse_args(argv)
    return compare(args.base, args.head, args.fail_over)


if __name__ == "__main__":
    sys.exit(main())
