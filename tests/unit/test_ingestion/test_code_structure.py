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
    reason_for_verdict,
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
