"""Test that Dockerfile and Dockerfile.worker pin uv by digest."""

from pathlib import Path


def test_dockerfiles_pin_uv_by_digest() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    dockerfile_paths = [
        repo_root / "Dockerfile",
        repo_root / "Dockerfile.worker",
    ]

    uv_copy_lines: list[str] = []

    for path in dockerfile_paths:
        assert path.exists(), f"Expected {path} to exist"
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("COPY --from=ghcr.io/astral-sh/uv")
        ]
        assert len(lines) == 1, (
            f"Expected exactly 1 uv line in {path.name}, found {len(lines)}"
        )
        line = lines[0]
        assert "@sha256:" in line, (
            f"Expected digest pin '@sha256:' in {path.name}, got: {line}"
        )
        assert ":latest" not in line, (
            f"Expected ':latest' to not be present in {path.name}, got: {line}"
        )
        uv_copy_lines.append(line)

    assert uv_copy_lines[0] == uv_copy_lines[1], (
        f"Mismatch between Dockerfile and Dockerfile.worker uv pins:\n"
        f"  Dockerfile:        {uv_copy_lines[0]}\n"
        f"  Dockerfile.worker: {uv_copy_lines[1]}"
    )
