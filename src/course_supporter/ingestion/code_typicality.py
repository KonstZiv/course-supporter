"""Typicality filter for code materials (task-code-materials F3/F7, R2).

Decides, per included file, whether it is CUSTOM authored code (gets a
verbatim segment) or lives as description-only (typical/library files
and files over the sanitary cap — recorded in
``DocumentSummary.structure`` with a reason, fully OUTSIDE the
reference text per F2; the material is never rejected, R5).

NEW ISOLATED MODULE by ratified design (F3): ``normalizer/classify.py``
is untouched — its ``aggregate_hash`` blast radius on existing KD18
project tasks is unacceptable. The directory layer is REUSED through
classify.py's public :func:`denylist_prefix` (single source of truth
for ``.venv`` / ``node_modules`` / build dirs, per-segment + glob
semantics); this module adds:

* an extra vendored-directory layer conventional for code lessons but
  absent from the KD18 denylist (``vendor`` / ``third_party`` / …);
* the FILE layer: lockfiles, minified/bundled artifacts, source maps,
  generated-file markers (R2: convention-based only — dependency
  manifests of ecosystems are never read);
* the ``kept_single_max_bytes`` = 4 MiB sanitary cap (F7, ratified):
  a garbage detector / second typicality guard, NOT a budget ceiling.

Asymmetry (ratified R2): doubtful → include. Only convention-certain
patterns demote a file to description-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal

from course_supporter.ingestion.code_structure import (
    CodeStructureReason,
    denylist_token,
    structure_reason,
)
from course_supporter.normalizer.classify import denylist_prefix

# F7 (ratified): generous per-file sanitary cap. Empirical grounding
# (pre-flight, 580 real custom files): max hand-authored 88 KB, target
# workload max 30 KB, p99 38 KB — 4 MiB is ~46x headroom. A file over
# the cap is a garbage/typicality signal, not legitimate lesson code.
KEPT_SINGLE_MAX_BYTES: Final[int] = 4 * 1024 * 1024

# Vendored-code directories conventional in lesson archives but not in
# the KD18 dir-denylist (which targets build/IDE/VCS junk).
_VENDORED_DIRS: Final[frozenset[str]] = frozenset(
    {"vendor", "vendors", "third_party", "thirdparty", "bower_components"}
)

# Lockfiles — machine-written dependency snapshots (R2 file layer).
_LOCKFILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "packages.lock.json",
        "pubspec.lock",
    }
)

# Minified / bundled / map artifacts — generated, never authored.
_GENERATED_SUFFIXES: Final[tuple[str, ...]] = (
    ".min.js",
    ".min.css",
    ".js.map",
    ".css.map",
    ".bundle.js",
)

# №21 (decision 8): project build-config files — description-only, so their keys
# (compilerOptions, devDependencies, outDir, …) stop reaching the concept lists.
# A first-approximation dictionary (decision 1: the author corrects a false hit
# on the confirm screen); core from the defect slice is angular/tsconfig/package.
_BUILD_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {"angular.json", "package.json", ".babelrc"}
)
# Config files matched by their ``<stem>.config.<ext>`` shape
# (webpack.config.js, vite.config.ts, rollup.config.mjs, …).
_BUILD_CONFIG_STEMS: Final[frozenset[str]] = frozenset(
    {
        "webpack.config",
        "vite.config",
        "rollup.config",
        "babel.config",
        "jest.config",
        "postcss.config",
        "tailwind.config",
    }
)


def _is_build_config(basename: str) -> bool:
    """Whether ``basename`` is a project build-config file (decision 8)."""
    if basename in _BUILD_CONFIG_NAMES:
        return True
    if basename.startswith("tsconfig") and basename.endswith(".json"):
        return True
    return basename.rsplit(".", 1)[0] in _BUILD_CONFIG_STEMS


Disposition = Literal["custom", "typical", "oversize"]


@dataclass(frozen=True, slots=True)
class TypicalityVerdict:
    """Per-file verdict: segment-worthy custom code or description-only."""

    disposition: Disposition
    reason: str | None

    @property
    def is_custom(self) -> bool:
        return self.disposition == "custom"


def assess(path: str, size_bytes: int) -> TypicalityVerdict:
    """Classify one included file: custom vs typical vs oversize.

    Order matters: the directory layers fire first (a lockfile inside
    ``node_modules`` reports the directory reason — the collapse point
    the author recognises), then the file layer, then the size cap.
    Anything that no convention-certain pattern demotes stays custom
    (ratified asymmetry: doubtful → include).
    """
    # Reason grammar "<token>: <detail>" — every token is drawn from the
    # code contour's single reason vocabulary (:mod:`code_structure`, №19),
    # never a raw string. The denylist branch here is effectively dead in
    # the archive contour (denylist entries are skipped at extraction), so
    # it only fires for a solo denylist upload; it still splits dir/leaf
    # for truthfulness (a leaf .DS_Store is not a directory).
    prefix = denylist_prefix(path)
    if prefix is not None:
        return TypicalityVerdict(
            "typical", structure_reason(denylist_token(prefix), prefix)
        )

    parts = PurePosixPath(path).parts
    for segment in parts[:-1]:
        if segment.lower() in _VENDORED_DIRS:
            return TypicalityVerdict(
                "typical",
                structure_reason(CodeStructureReason.VENDORED_DIR, segment),
            )

    basename = parts[-1].lower() if parts else path.lower()
    if basename in _LOCKFILE_NAMES:
        return TypicalityVerdict(
            "typical", structure_reason(CodeStructureReason.LOCKFILE, basename)
        )
    for suffix in _GENERATED_SUFFIXES:
        if basename.endswith(suffix):
            return TypicalityVerdict(
                "typical",
                structure_reason(CodeStructureReason.GENERATED_ARTIFACT, f"*{suffix}"),
            )

    # №21 (decision 8): build-config → description-only (a new BUILD_CONFIG
    # reason). Before the size cap: a config file is config regardless of size.
    if _is_build_config(basename):
        return TypicalityVerdict(
            "typical",
            structure_reason(CodeStructureReason.BUILD_CONFIG, basename),
        )

    if size_bytes > KEPT_SINGLE_MAX_BYTES:
        return TypicalityVerdict(
            "oversize",
            structure_reason(
                CodeStructureReason.OVERSIZE,
                f"file size {size_bytes} B exceeds the "
                f"{KEPT_SINGLE_MAX_BYTES} B per-file cap",
            ),
        )

    return TypicalityVerdict("custom", None)
