"""LLM call cost report CLI.

Usage:
    uv run python scripts/cost_report.py          # ASCII table
    uv run python scripts/cost_report.py --json    # JSON output
"""

from __future__ import annotations

import argparse
import asyncio
import json

from course_supporter.models.reports import CostReport
from course_supporter.storage.database import async_session
from course_supporter.storage.repositories import LLMCallRepository


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="LLM call cost report")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON instead of ASCII table",
    )
    return parser.parse_args(argv)


async def fetch_report() -> CostReport:
    """Fetch cost report from database."""
    async with async_session() as session:
        repo = LLMCallRepository(session)
        return await repo.get_full_report()


def print_table(report: CostReport) -> str:
    """Format report as ASCII table and return the string."""
    lines: list[str] = []

    lines.append("=== Cost Summary ===")
    s = report.summary
    lines.append(f"  Total calls:      {s.total_calls}")
    lines.append(f"  Successful:       {s.successful_calls}")
    lines.append(f"  Failed:           {s.failed_calls}")
    lines.append(f"  Total cost (USD): ${s.total_cost_usd:.4f}")
    lines.append(f"  Tokens in:        {s.total_tokens_in}")
    lines.append(f"  Tokens out:       {s.total_tokens_out}")
    lines.append(f"  Avg latency (ms): {s.avg_latency_ms:.1f}")
    lines.append("")

    for label, groups in [
        ("By Action", report.by_action),
        ("By Provider", report.by_provider),
        ("By Model", report.by_model),
    ]:
        lines.append(f"=== {label} ===")
        if not groups:
            lines.append("  (no data)")
        else:
            header = (
                f"  {'Group':<30} {'Calls':>6} {'Cost':>10} "
                f"{'Tokens In':>10} {'Tokens Out':>11} {'Avg ms':>8}"
            )
            lines.append(header)
            lines.append("  " + "-" * (len(header) - 2))
            for g in groups:
                lines.append(
                    f"  {g.group:<30} {g.calls:>6} "
                    f"${g.cost_usd:>9.4f} {g.tokens_in:>10} "
                    f"{g.tokens_out:>11} {g.avg_latency_ms:>8.1f}"
                )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    """Run the cost report CLI."""
    args = parse_args()
    report = asyncio.run(fetch_report())

    if args.json_output:
        print(json.dumps(report.model_dump(), indent=2, ensure_ascii=False))
    else:
        print(print_table(report))


if __name__ == "__main__":
    main()
