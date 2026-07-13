"""Tests for the normalizer classifier + component denylist (KD18 P1).

Pure path/extension classification -- no archive I/O. Proves component
(not substring / prefix) denylist matching, per-segment glob, leaf vs
directory collapse, nested-denylist highest-occurrence, and the
ext -> EntryClass map.
"""

from course_supporter.normalizer.classify import (
    ExtensionClassifier,
    collapse_denylist,
    denylist_prefix,
)
from course_supporter.normalizer.models import EntryClass, ExcludedReason
from course_supporter.normalizer.ports import EntryClassifier


class TestExtensionClassifier:
    def test_conforms_to_port(self) -> None:
        clf: EntryClassifier = ExtensionClassifier()
        assert clf.classify("x.py", b"") is EntryClass.TEXT

    def test_text_extensions(self) -> None:
        clf = ExtensionClassifier()
        for path in ("main.py", "app.js", "data.json", "notes.md", "q.sql"):
            assert clf.classify(path, b"") is EntryClass.TEXT

    def test_document_extensions(self) -> None:
        clf = ExtensionClassifier()
        assert clf.classify("spec.docx", b"") is EntryClass.DOCUMENT
        assert clf.classify("paper.pdf", b"") is EntryClass.DOCUMENT

    def test_binary_for_unknown_and_dotfiles(self) -> None:
        clf = ExtensionClassifier()
        for path in ("lib.so", "mod.pyc", "img.png", "a.out", ".gitignore", "Makefile"):
            assert clf.classify(path, b"") is EntryClass.BINARY

    def test_extension_from_last_component_only(self) -> None:
        # A dot in a parent directory must not leak into the extension.
        clf = ExtensionClassifier()
        assert clf.classify("my.pkg/Makefile", b"") is EntryClass.BINARY
        assert clf.classify("v1.2/main.py", b"") is EntryClass.TEXT


class TestDenylistComponentMatch:
    def test_denied_dir_component(self) -> None:
        assert denylist_prefix("a/.venv/lib/x.py") == "a/.venv/"
        assert denylist_prefix(".venv/x.py") == ".venv/"

    def test_not_denied_lookalikes(self) -> None:
        # Component equality: substrings / prefixes must NOT match.
        for path in (
            ".venvrc",
            "my.venv.backup/x.py",
            "mybin/run",
            "bin.txt",
            "src/binder.py",
        ):
            assert denylist_prefix(path) is None

    def test_bin_component_matches_but_not_lookalikes(self) -> None:
        assert denylist_prefix("proj/bin/tool") == "proj/bin/"
        assert denylist_prefix("proj/mybin/tool") is None

    def test_glob_component(self) -> None:
        assert denylist_prefix("pkg.egg-info/PKG-INFO") == "pkg.egg-info/"
        # Requires a literal dot before egg-info; a bare token must not match.
        assert denylist_prefix("notegg-info/x") is None

    def test_ds_store_leaf_no_trailing_slash(self) -> None:
        assert denylist_prefix("src/.DS_Store") == "src/.DS_Store"
        assert denylist_prefix(".DS_Store") == ".DS_Store"

    def test_highest_occurrence_wins(self) -> None:
        # .venv (outer) wins over node_modules (inner); one prefix.
        assert denylist_prefix("a/.venv/lib/node_modules/x.js") == "a/.venv/"

    def test_no14_expansion_dirs(self) -> None:
        # №14 (operator-ratified 2026-07-12): OS junk + framework caches.
        assert denylist_prefix("__MACOSX/app/._readme.pdf") == "__MACOSX/"
        assert denylist_prefix("lesson/.angular/cache/x.js") == "lesson/.angular/"
        assert denylist_prefix("app/.next/static/c.js") == "app/.next/"
        assert denylist_prefix("app/.nuxt/dist/c.js") == "app/.nuxt/"
        assert denylist_prefix("proj/.cxx/abi/x.o") == "proj/.cxx/"
        assert denylist_prefix("a/.externalNativeBuild/x") == (
            "a/.externalNativeBuild/"
        )
        assert denylist_prefix("captures/screen.png") == "captures/"
        assert denylist_prefix("vol/.Trashes/501/f.txt") == "vol/.Trashes/"

    def test_no14_expansion_file_globs(self) -> None:
        # AppleDouble companions match as a leaf AND as any segment.
        assert denylist_prefix("app/._main.js") == "app/._main.js"
        assert denylist_prefix("~$report.docx") == "~$report.docx"
        assert denylist_prefix("home/.Trash-1000/f.txt") == "home/.Trash-1000/"
        assert denylist_prefix("pics/Thumbs.db") == "pics/Thumbs.db"
        assert denylist_prefix("Desktop.ini") == "Desktop.ini"
        assert denylist_prefix("android/local.properties") == (
            "android/local.properties"
        )

    def test_no14_expansion_lookalikes_not_matched(self) -> None:
        # Component equality / per-segment glob: near-misses stay clean.
        for path in (
            "my.angular/file.js",  # not the `.angular` segment
            ".angularx/f.js",
            "src/handler_.py",  # `._*` anchors at the segment start
            "notes/thumbs.db.md",
            "capturesx/f.png",
        ):
            assert denylist_prefix(path) is None, path


class TestCollapseDenylist:
    def test_two_files_same_prefix_one_row(self) -> None:
        rows = collapse_denylist([("a/.venv/x.py", 10), ("a/.venv/lib/y.py", 20)])
        assert len(rows) == 1
        row = rows[0]
        assert row.path == "a/.venv/"
        assert row.reason is ExcludedReason.DENYLIST_DIR
        assert row.entries == 2
        assert row.size == 30

    def test_ds_store_leaf_entries_one(self) -> None:
        rows = collapse_denylist([("src/.DS_Store", 6)])
        assert len(rows) == 1
        assert rows[0].path == "src/.DS_Store"
        assert rows[0].entries == 1
        assert rows[0].size == 6

    def test_nested_denylist_not_duplicated(self) -> None:
        rows = collapse_denylist(
            [
                ("a/.venv/x.py", 1),
                ("a/.venv/lib/node_modules/y.js", 2),
            ]
        )
        assert len(rows) == 1
        assert rows[0].path == "a/.venv/"
        assert rows[0].entries == 2
        assert rows[0].size == 3

    def test_distinct_prefixes_distinct_rows_sorted(self) -> None:
        rows = collapse_denylist(
            [
                ("b/node_modules/x", 1),
                ("a/.venv/y", 2),
            ]
        )
        assert [r.path for r in rows] == ["a/.venv/", "b/node_modules/"]

    def test_non_denied_entries_ignored(self) -> None:
        rows = collapse_denylist([("src/main.py", 100), ("README.md", 5)])
        assert rows == ()
