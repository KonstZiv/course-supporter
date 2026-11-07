"""Run hints of a project snapshot: tests, dependency files, entry points."""

from course_supporter.homework.run_hints import RunHints, detect_run_hints


def test_an_empty_snapshot_has_no_hints() -> None:
    hints = detect_run_hints([])
    assert hints == RunHints(test_files=(), dependency_files=(), entry_candidates=())
    assert hints.has_tests is False


def test_test_files_by_name_and_by_directory() -> None:
    hints = detect_run_hints(
        [
            "test_stats.py",
            "src/stats_test.py",
            "tests/conftest.py",
            "tests/unit/helpers.py",
            "test/data/sample.txt",
            "src/stats.py",
        ]
    )
    assert hints.test_files == (
        "src/stats_test.py",
        "test_stats.py",
        "tests/conftest.py",
        "tests/unit/helpers.py",
    )
    assert hints.has_tests is True


def test_dependency_files() -> None:
    hints = detect_run_hints(
        [
            "requirements.txt",
            "requirements-dev.txt",
            "pyproject.toml",
            "backend/package.json",
            "Pipfile",
            "requirements.md",
            "src/setup_utils.py",
        ]
    )
    assert hints.dependency_files == (
        "Pipfile",
        "backend/package.json",
        "pyproject.toml",
        "requirements-dev.txt",
        "requirements.txt",
    )


def test_entry_candidates_skip_tests() -> None:
    hints = detect_run_hints(["main.py", "app/__main__.py", "tests/main.py"])
    assert hints.entry_candidates == ("app/__main__.py", "main.py")


def test_paths_are_normalized_and_deduplicated() -> None:
    hints = detect_run_hints(
        ["./tests/test_a.py", "tests\\test_a.py", "tests/test_a.py"]
    )
    assert hints.test_files == ("tests/test_a.py",)


def test_names_are_matched_without_case() -> None:
    hints = detect_run_hints(["Tests/Test_A.py", "PYPROJECT.TOML", "Main.py"])
    assert hints.test_files == ("Tests/Test_A.py",)
    assert hints.dependency_files == ("PYPROJECT.TOML",)
    assert hints.entry_candidates == ("Main.py",)
