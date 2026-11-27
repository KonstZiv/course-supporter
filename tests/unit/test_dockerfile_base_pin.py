"""Test that Dockerfile and Dockerfile.worker pin the Python base image by digest."""

from pathlib import Path


def test_dockerfiles_pin_python_base_by_digest() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pins: list[str] = []

    for name in ("Dockerfile", "Dockerfile.worker"):
        text = (repo_root / name).read_text(encoding="utf-8")
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("FROM python:")
        ]
        assert len(lines) == 2, (
            f"Expected 2 python FROM lines in {name}, found {len(lines)}"
        )
        for line in lines:
            assert "@sha256:" in line, (
                f"Expected a digest pin '@sha256:' in {name}, got: {line}"
            )
            pins.append(line.split()[1])

    assert len(set(pins)) == 1, (
        f"All four python base pins must name the same image, got: {pins}"
    )
