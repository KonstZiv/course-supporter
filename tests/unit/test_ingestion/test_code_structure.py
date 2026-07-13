"""Code-contour reason vocabulary — construction guards (№19).

Mirrors the footgun-guards of №17/№18: the value here is not that any
single case is surprising but that the CONSTRUCTION cannot silently
regress. ``reason_for_verdict`` must stay total over the verdicts that
reach the classify-mode else-branch, or a future ``EntryVerdict`` member
leaks its raw value into the ``code_summary`` prompt again — which is
exactly the bug (№19).
"""

import pytest

from course_supporter.ingestion.code_structure import (
    CodeStructureReason,
    denylist_token,
    describe_for_llm,
    reason_for_verdict,
    render_structure_block,
    structure_reason,
)
from course_supporter.security.archive import EntryVerdict

# The two verdicts the caller handles before the else-branch; every other
# EntryVerdict CAN reach reason_for_verdict and MUST be mapped.
_CALLER_HANDLED = {EntryVerdict.INCLUDED, EntryVerdict.DENYLIST_SKIP}


class TestReasonForVerdictTotality:
    def test_total_over_every_reachable_verdict(self) -> None:
        # Footgun-guard: add an EntryVerdict member that can reach the
        # else-branch without extending _VERDICT_TO_REASON and this fails
        # (it raises) — the raw .value can never leak again (№19).
        for verdict in EntryVerdict:
            if verdict in _CALLER_HANDLED:
                continue
            assert isinstance(reason_for_verdict(verdict), CodeStructureReason)

    def test_forbidden_type_is_translated_not_leaked(self) -> None:
        # The core №19 truth-fix: the strict contour's "forbidden_type"
        # becomes the code contour's honest "non_code_type" — nothing is
        # forbidden here, the file is simply not code.
        assert (
            reason_for_verdict(EntryVerdict.FORBIDDEN_TYPE)
            is CodeStructureReason.NON_CODE_TYPE
        )
        assert reason_for_verdict(EntryVerdict.FORBIDDEN_TYPE) != "forbidden_type"

    def test_magic_mismatch_stays_verbatim(self) -> None:
        # Truthful in both contours (a real security signal) — untouched.
        assert (
            reason_for_verdict(EntryVerdict.MAGIC_MISMATCH)
            is CodeStructureReason.MAGIC_MISMATCH
        )

    @pytest.mark.parametrize("verdict", sorted(_CALLER_HANDLED))
    def test_caller_handled_verdicts_raise(self, verdict: EntryVerdict) -> None:
        # INCLUDED / DENYLIST_SKIP never belong in an excluded reason —
        # reaching this map with them is an impossible state, raised loudly.
        with pytest.raises(ValueError, match="no code-structure reason"):
            reason_for_verdict(verdict)


class TestDenylistToken:
    def test_directory_prefix_is_dir(self) -> None:
        assert denylist_token("app/.angular/") is CodeStructureReason.DENYLIST_DIR

    def test_leaf_prefix_is_file(self) -> None:
        # №19 acceptance #4: a leaf .DS_Store is not a directory.
        assert denylist_token(".DS_Store") is CodeStructureReason.DENYLIST_FILE


class TestStructureReason:
    def test_bare_token_when_no_detail(self) -> None:
        assert structure_reason(CodeStructureReason.NON_CODE_TYPE) == "non_code_type"

    def test_token_and_detail(self) -> None:
        assert (
            structure_reason(CodeStructureReason.LOCKFILE, "package-lock.json")
            == "lockfile: package-lock.json"
        )


# The full structure of a macOS-packed lesson, in DB (per-file) shape.
_STRUCTURE_ENTRIES = [
    {"path": "src/app.py", "size": 100, "cls": "included", "reason": None},
    {
        "path": "package-lock.json",
        "size": 24,
        "cls": "description_only",
        "reason": "lockfile: package-lock.json",
    },
    {
        "path": "a/.gitignore",
        "size": 10,
        "cls": "excluded",
        "reason": "non_code_type",
        "entries": 1,
    },
    {
        "path": "b/.gitignore",
        "size": 10,
        "cls": "excluded",
        "reason": "non_code_type",
        "entries": 1,
    },
    {
        "path": "favicon.ico",
        "size": 50,
        "cls": "excluded",
        "reason": "non_code_type",
        "entries": 1,
    },
    {
        "path": "__MACOSX/",
        "size": 200,
        "cls": "excluded",
        "reason": "denylist_dir",
        "entries": 5,
    },
    {
        "path": ".vscode/",
        "size": 80,
        "cls": "excluded",
        "reason": "denylist_dir",
        "entries": 3,
    },
    {
        "path": "app/.angular/",
        "size": 40,
        "cls": "excluded",
        "reason": "denylist_dir",
        "entries": 2,
    },
    {
        "path": ".DS_Store",
        "size": 6,
        "cls": "excluded",
        "reason": "denylist_file",
        "entries": 1,
    },
    {
        "path": "evil.py",
        "size": 9,
        "cls": "excluded",
        "reason": "magic_mismatch",
        "entries": 1,
    },
]


class TestRenderStructureBlock:
    def test_included_and_description_only_stay_per_file(self) -> None:
        block = render_structure_block(_STRUCTURE_ENTRIES)
        # included per-file; description_only by name + consequence (D1:
        # the model must see package-lock.json — it is a dependency).
        assert "src/app.py (100 B)" in block
        assert "package-lock.json (24 B) — залежності проєкту" in block

    def test_excluded_aggregated_with_counts(self) -> None:
        block = render_structure_block(_STRUCTURE_ENTRIES)
        # env junk: 3 dirs (5+3+2 files) + 1 leaf = 11 files, one line.
        assert (
            "Службові теки та файли середовища/редактора — "
            "пропущено (тек: 3, файлів: 11)" in block
        )
        # non_code: 3 files, distinct basenames listed.
        assert "Не є кодом — пропущено (файлів: 3; .gitignore, favicon.ico)" in block
        assert "Вміст не відповідає розширенню — пропущено (файлів: 1)" in block

    def test_block_leaks_no_internal_token(self) -> None:
        # Acceptance #3: the model never sees a machine token.
        block = render_structure_block(_STRUCTURE_ENTRIES)
        for reason in CodeStructureReason:
            assert reason.value not in block, reason

    def test_block_leaks_no_service_path(self) -> None:
        # Acceptance #3: no service-file PATH reaches the prompt — the
        # denylist junk is counts only (basenames of non-code files like
        # .gitignore are intentional per D1; service junk paths are not).
        block = render_structure_block(_STRUCTURE_ENTRIES)
        for service_path in ("__MACOSX", ".DS_Store", ".vscode", ".angular"):
            assert service_path not in block, service_path

    def test_phrase_has_no_token_substring(self) -> None:
        # The token->phrase map must itself be token-free, else the block
        # could leak one indirectly.
        for reason in CodeStructureReason:
            phrase = describe_for_llm(reason)
            for other in CodeStructureReason:
                assert other.value not in phrase

    def test_description_only_token_on_excluded_row_raises(self) -> None:
        # Loud guard (sibling of reason_for_verdict): the four typicality
        # tokens are description_only-only. If a future change ever routed
        # one to an excluded row it would silently vanish from the prompt —
        # instead it raises. Locks the anti-silent-drop invariant.
        rogue = [
            {
                "path": "vendor/lib.js",
                "size": 10,
                "cls": "excluded",
                "reason": "lockfile",
                "entries": 1,
            }
        ]
        with pytest.raises(ValueError, match="non-excluded reasons"):
            render_structure_block(rogue)
