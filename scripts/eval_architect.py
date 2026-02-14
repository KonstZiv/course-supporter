"""ArchitectAgent quality assessment.

Usage:
    uv run python scripts/eval_architect.py               # real LLM
    uv run python scripts/eval_architect.py --mock         # mock mode (CI)
    uv run python scripts/eval_architect.py --save-mock    # run real + save response
    uv run python scripts/eval_architect.py --output r.json  # save report to file
    uv run python scripts/eval_architect.py --threshold 0.7  # custom pass threshold
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from course_supporter.evals.comparator import StructureComparator
from course_supporter.models.course import CourseStructure

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "eval"
MOCK_PATH = FIXTURE_DIR / "mock_llm_response.json"
REFERENCE_PATH = FIXTURE_DIR / "reference_structure.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Evaluate ArchitectAgent output")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use pre-saved mock LLM response instead of calling real LLM",
    )
    parser.add_argument(
        "--save-mock",
        action="store_true",
        help="Run real LLM and save response as mock fixture",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Save JSON report to file",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Minimum overall score for success (default: 0.5)",
    )
    return parser.parse_args(argv)


def load_reference() -> CourseStructure:
    """Load the gold-standard reference structure."""
    raw = REFERENCE_PATH.read_text()
    return CourseStructure.model_validate_json(raw)


def load_mock() -> CourseStructure:
    """Load pre-saved mock LLM response."""
    raw = MOCK_PATH.read_text()
    return CourseStructure.model_validate_json(raw)


async def run_real_pipeline() -> CourseStructure:
    """Run the real pipeline: TextProcessor -> MergeStep -> ArchitectAgent."""
    from course_supporter.agents.architect import ArchitectAgent
    from course_supporter.config import settings
    from course_supporter.ingestion.merge import MergeStep
    from course_supporter.ingestion.text import TextProcessor
    from course_supporter.llm import create_model_router
    from course_supporter.models.source import SourceMaterial, SourceType

    router = create_model_router(settings, session_factory=None)
    processor = TextProcessor()
    merge = MergeStep()

    fixture_files = [
        ("transcript.txt", SourceType.VIDEO),
        ("slides.txt", SourceType.PRESENTATION),
        ("tutorial.md", SourceType.TEXT),
    ]

    documents = []
    for filename, source_type in fixture_files:
        filepath = FIXTURE_DIR / filename
        material = SourceMaterial(
            source_type=source_type,
            source_url=str(filepath),
            title=filepath.stem,
        )
        doc = await processor.process(material, router=None)
        documents.append(doc)

    context = merge.merge(documents)
    agent = ArchitectAgent(router=router)
    return await agent.run(context)


def main() -> None:
    """Run the eval script."""
    args = parse_args()

    reference = load_reference()

    if args.mock:
        print("Mode: mock (using pre-saved LLM response)")
        generated = load_mock()
    else:
        print("Mode: real LLM")
        generated = asyncio.run(run_real_pipeline())

    if args.save_mock:
        MOCK_PATH.write_text(
            json.dumps(
                generated.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        print(f"Mock response saved to {MOCK_PATH}")

    comparator = StructureComparator()
    report = comparator.compare(generated, reference)

    print()
    print(report.to_table())
    print()

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
        )
        print(f"Report saved to {output_path}")

    sys.exit(0 if report.overall_score >= args.threshold else 1)


if __name__ == "__main__":
    main()
