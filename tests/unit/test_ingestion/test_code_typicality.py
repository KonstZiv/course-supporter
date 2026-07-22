"""Unit tests for the code typicality filter (task-code-materials F3/F7).

The module is NEW and isolated by ratified design: classify.py stays
untouched (its dir-layer is consumed through the public
``denylist_prefix``); the belt-and-braces coupling lock below keeps the
KD18 blast-radius guarantee visible.
"""

from __future__ import annotations

import pytest

from course_supporter.ingestion.code_typicality import (
    KEPT_SINGLE_MAX_BYTES,
    TypicalityVerdict,
    assess,
)


class TestDirLayers:
    def test_kd18_denylist_dir_is_typical(self) -> None:
        verdict = assess("app/node_modules/lodash/index.js", 1000)
        assert verdict.disposition == "typical"
        assert verdict.reason is not None
        assert "node_modules" in verdict.reason

    def test_venv_dir_is_typical(self) -> None:
        assert assess(".venv/lib/site.py", 10).disposition == "typical"

    def test_vendored_dir_layer_added_by_this_module(self) -> None:
        # ``vendor`` is NOT in the KD18 dir-denylist — the file layer
        # of this module adds it (R2 vendored conventions).
        verdict = assess("src/vendor/lib.js", 10)
        assert verdict.disposition == "typical"
        assert verdict.reason is not None
        assert "vendored" in verdict.reason

    def test_vendor_as_leaf_filename_is_not_a_dir_match(self) -> None:
        # Only DIRECTORY components match the vendored layer.
        assert assess("src/vendor.js", 10).is_custom


class TestFileLayer:
    def test_lockfiles_are_typical(self) -> None:
        for name in ("package-lock.json", "poetry.lock", "go.sum", "uv.lock"):
            verdict = assess(f"app/{name}", 500)
            assert verdict.disposition == "typical", name

    def test_minified_and_maps_are_typical(self) -> None:
        for name in ("app.min.js", "style.min.css", "bundle.js.map"):
            assert assess(f"static/{name}", 500).disposition == "typical", name

    def test_case_insensitive_basename(self) -> None:
        assert assess("Gemfile.lock", 10).disposition == "typical"


class TestSizeCap:
    def test_over_cap_is_oversize(self) -> None:
        verdict = assess("src/huge_dump.py", KEPT_SINGLE_MAX_BYTES + 1)
        assert verdict.disposition == "oversize"
        assert verdict.reason is not None
        assert str(KEPT_SINGLE_MAX_BYTES) in verdict.reason

    def test_at_cap_is_custom(self) -> None:
        assert assess("src/big.py", KEPT_SINGLE_MAX_BYTES).is_custom


class TestAsymmetry:
    def test_ordinary_source_is_custom(self) -> None:
        # Ratified R2 asymmetry: doubtful → include.
        for path in ("src/app.py", "lib/service.rb", "a/b/c/main.go", "x.html"):
            assert assess(path, 1000) == TypicalityVerdict("custom", None), path


class TestClassifyCouplingLock:
    def test_dir_layer_is_consumed_from_classify(self) -> None:
        """Belt-and-braces (F3): the dir-layer is classify.py's, unmodified.

        The typicality module must never grow its own copy of the KD18
        dir-denylist — a fork would silently drift from the manifest
        classification existing project tasks hash against.
        """
        from course_supporter.ingestion import code_typicality
        from course_supporter.normalizer.classify import denylist_prefix

        assert code_typicality.denylist_prefix is denylist_prefix


class TestBuildConfig:
    """№21 (decision 8): build-config files → description-only (BUILD_CONFIG)."""

    @pytest.mark.parametrize(
        "name",
        [
            "angular.json",
            "package.json",
            "tsconfig.json",
            "tsconfig.app.json",
            "tsconfig.spec.json",
            "webpack.config.js",
            "vite.config.ts",
            "rollup.config.mjs",
            ".babelrc",
            "babel.config.js",
            "jest.config.ts",
            "postcss.config.cjs",
            "tailwind.config.js",
        ],
    )
    def test_build_config_is_description_only(self, name: str) -> None:
        verdict = assess(f"project/{name}", 500)
        assert verdict.disposition == "typical"
        assert not verdict.is_custom
        assert verdict.reason is not None
        assert verdict.reason.startswith("build_config")

    def test_ordinary_code_stays_custom(self) -> None:
        # A user file whose name merely CONTAINS "config" is not a build config;
        # only the exact names / ``<stem>.config.<ext>`` shapes match.
        assert assess("src/config.ts", 500).is_custom
        assert assess("src/app.component.ts", 500).is_custom
