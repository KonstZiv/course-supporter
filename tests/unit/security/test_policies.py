"""Tests for context security policies (KD14).

Vision conformance for the §KD14 policy table:

* :class:`TestAuthoredPolicy` -- AUTHORED constant matches table.
* :class:`TestHomeworkPolicy` -- HOMEWORK constant matches table.
* :class:`TestPolicyResolver` -- ``policy_for`` resolution + guard.
* :class:`TestVideoSizeResolver` -- ``get_max_size_for_extension``.
* :class:`TestPolicyConsistency` -- regression guard against drift
  between policy whitelists and the file_type MIME map.
* :class:`TestConveyorTable` -- every accepted format names a checking
  pipeline, and only accepted formats do.
* :class:`TestDoorMatchesPolicy` -- route door == Stage 1 policy after
  the prefix transform.
* :class:`TestTextExtensionsDerived` -- everything admitted as text is
  screened as text.
"""

import pytest

# ``homework.submission_core`` imports ``api.upload_validation``, and
# ``api/__init__`` eagerly imports the FastAPI app -- so importing the door
# module first hits a circular import. The cycle is pre-existing (identical on
# eae1c7d, not introduced by the gates work); importing the api package up
# front resolves the order without hiding it.
import course_supporter.api  # noqa: F401
from course_supporter.homework.submission_core import ALLOWED_HOMEWORK_EXTENSIONS
from course_supporter.security.file_type import _EXTENSION_TO_MIME_FAMILIES
from course_supporter.security.policies import (
    _ARCHIVES,
    _DOCUMENTS,
    _PROSE,
    AUTHORED_POLICY,
    CODE_EXTENSIONS,
    HOMEWORK_CONVEYORS,
    HOMEWORK_POLICY,
    ContextPolicy,
    get_max_size_for_extension,
    policy_for,
)
from course_supporter.security.stage1 import _TEXT_EXTENSIONS

# ── Authored policy ────────────────────────────────────────────────


class TestAuthoredPolicy:
    def test_name(self) -> None:
        assert AUTHORED_POLICY.name == "authored"

    def test_extensions_include_video(self) -> None:
        assert {"mp4", "mov", "avi", "mkv", "webm"} <= (
            AUTHORED_POLICY.allowed_extensions
        )

    def test_extensions_include_audio(self) -> None:
        assert {"mp3", "wav", "m4a", "ogg", "flac"} <= (
            AUTHORED_POLICY.allowed_extensions
        )

    def test_extensions_include_documents(self) -> None:
        assert {
            "pdf",
            "pptx",
            "ppt",
            "md",
            "markdown",
            "docx",
            "txt",
            "html",
            "htm",
        } <= AUTHORED_POLICY.allowed_extensions

    def test_extensions_include_code(self) -> None:
        # task-code-materials R2: the broad code list rides the whitelist.
        assert AUTHORED_POLICY.allowed_extensions >= CODE_EXTENSIONS

    def test_zip_only_archive_extension(self) -> None:
        # task-code-materials R1: zip accepted for code project archives;
        # tar-family stays excluded from authored (light-gate is zip-only).
        assert "zip" in AUTHORED_POLICY.allowed_extensions
        assert {"gz", "tgz"} & AUTHORED_POLICY.allowed_extensions == set()

    def test_size_limits(self) -> None:
        assert AUTHORED_POLICY.max_file_size_bytes == 100 * 1024 * 1024
        # Two distinct video caps (ARC.md §9): upload (browser -> route
        # memory) is tighter than download (worker fetch by URL).
        assert AUTHORED_POLICY.max_video_upload_bytes == 1024 * 1024 * 1024
        assert AUTHORED_POLICY.max_video_download_bytes == 5 * 1024 * 1024 * 1024
        assert AUTHORED_POLICY.max_presentation_size_bytes == 50 * 1024 * 1024

    def test_archive_caps(self) -> None:
        # Mirror _PROJECT_NORMALIZE_LIMITS (normalizer/models.py): same
        # author-curated-project envelope as KD18 base archives.
        assert AUTHORED_POLICY.max_archive_unzipped_bytes == 200 * 1024 * 1024
        assert AUTHORED_POLICY.max_archive_nesting_depth == 1

    def test_safety_and_charset_flags(self) -> None:
        assert AUTHORED_POLICY.enable_llm_safety_check is True
        assert AUTHORED_POLICY.enable_charset_strict is False


# ── Homework policy ────────────────────────────────────────────────


class TestHomeworkPolicy:
    def test_name(self) -> None:
        assert HOMEWORK_POLICY.name == "homework"

    def test_extensions_are_the_derived_union(self) -> None:
        # Was a literal eight-item list; now the union it is built from. The
        # assertion moved from the VALUES to the CONSTRUCTION on purpose --
        # re-listing the members here would recreate, inside the test, the
        # hand-maintained twin the derived form exists to remove (DD-19-B).
        assert HOMEWORK_POLICY.allowed_extensions == (
            _PROSE | CODE_EXTENSIONS | _ARCHIVES | _DOCUMENTS
        )

    def test_groups_are_disjoint(self) -> None:
        # Overlap would make a format's conveyor ambiguous: HOMEWORK_CONVEYORS
        # is built by merging the group dicts, so a member of two groups would
        # silently take whichever comes last.
        groups = (_PROSE, CODE_EXTENSIONS, _ARCHIVES, _DOCUMENTS)
        for i, first in enumerate(groups):
            for second in groups[i + 1 :]:
                assert first & second == set(), f"{first & second!r} in two groups"

    def test_no_video_extensions(self) -> None:
        # Homework context does not accept video uploads.
        assert {"mp4", "mov", "avi", "mkv", "webm"} & (
            HOMEWORK_POLICY.allowed_extensions
        ) == set()

    def test_size_limits(self) -> None:
        assert HOMEWORK_POLICY.max_file_size_bytes == 1 * 1024 * 1024
        assert HOMEWORK_POLICY.max_video_upload_bytes is None
        assert HOMEWORK_POLICY.max_video_download_bytes is None
        assert HOMEWORK_POLICY.max_presentation_size_bytes is None

    def test_archive_caps(self) -> None:
        assert HOMEWORK_POLICY.max_archive_unzipped_bytes == 10 * 1024 * 1024
        assert HOMEWORK_POLICY.max_archive_nesting_depth == 3

    def test_safety_and_charset_flags(self) -> None:
        assert HOMEWORK_POLICY.enable_llm_safety_check is True
        assert HOMEWORK_POLICY.enable_charset_strict is True


# ── Policy resolver ────────────────────────────────────────────────


class TestPolicyResolver:
    def test_resolves_authored(self) -> None:
        assert policy_for("authored") is AUTHORED_POLICY

    def test_resolves_homework(self) -> None:
        assert policy_for("homework") is HOMEWORK_POLICY

    def test_unknown_context_raises(self) -> None:
        # Defense-in-depth runtime guard against callers bypassing
        # the Literal type hint.
        with pytest.raises(ValueError, match="unknown context"):
            policy_for("admin")  # type: ignore[arg-type]


# ── Video size resolver ────────────────────────────────────────────


class TestVideoSizeResolver:
    def test_video_extension_returns_video_cap(self) -> None:
        # The resolver serves the UPLOAD cap (browser -> route), never the
        # larger download cap read directly by the yt-dlp path (ARC.md §9).
        assert (
            get_max_size_for_extension("mp4", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_video_upload_bytes
        )

    def test_audio_extension_returns_default(self) -> None:
        # Audio is NOT in _VIDEO_EXTENSIONS — default cap applies.
        assert (
            get_max_size_for_extension("mp3", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_file_size_bytes
        )

    def test_document_extension_returns_default(self) -> None:
        # ``md`` is neither video nor presentation -> default cap applies.
        # (``pdf`` moved to the presentation override in Phase 2.3 #6.)
        assert (
            get_max_size_for_extension("md", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_file_size_bytes
        )

    def test_video_extension_in_homework_returns_default(self) -> None:
        # HOMEWORK has no max_video_upload_bytes (None) — even if the
        # extension is in _VIDEO_EXTENSIONS, the default applies.
        assert (
            get_max_size_for_extension("mp4", HOMEWORK_POLICY)
            == HOMEWORK_POLICY.max_file_size_bytes
        )

    def test_uppercase_extension_normalized(self) -> None:
        assert (
            get_max_size_for_extension("MP4", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_video_upload_bytes
        )


# ── Presentation size resolver (Phase 2.3 #6, KD-2.3-M) ────────────


class TestPresentationSizeResolver:
    @pytest.mark.parametrize("ext", ["pdf", "pptx", "ppt"])
    def test_presentation_extension_returns_presentation_cap(self, ext: str) -> None:
        assert (
            get_max_size_for_extension(ext, AUTHORED_POLICY)
            == AUTHORED_POLICY.max_presentation_size_bytes
        )

    def test_authored_policy_presentation_size_cap_50mb(self) -> None:
        assert AUTHORED_POLICY.max_presentation_size_bytes == 50 * 1024 * 1024

    def test_homework_presentation_extension_takes_the_document_cap(self) -> None:
        # HOMEWORK has no presentation override (None), so pdf falls THROUGH
        # it -- and lands on the document cap, not the text default: the
        # conveyor table routes pdf to ``document``. The authored 50 MB
        # presentation cap is untouched either way.
        assert HOMEWORK_POLICY.max_presentation_size_bytes is None
        assert (
            get_max_size_for_extension("pdf", HOMEWORK_POLICY)
            == HOMEWORK_POLICY.max_document_size_bytes
            == 10 * 1024 * 1024
        )

    def test_homework_document_cap_does_not_leak_onto_text(self) -> None:
        for ext in ("md", "txt", "py", "zip"):
            assert (
                get_max_size_for_extension(ext, HOMEWORK_POLICY)
                == HOMEWORK_POLICY.max_file_size_bytes
            ), ext

    def test_ppt_in_authored_whitelist(self) -> None:
        assert "ppt" in AUTHORED_POLICY.allowed_extensions

    def test_uppercase_presentation_extension_normalized(self) -> None:
        assert (
            get_max_size_for_extension("PDF", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_presentation_size_bytes
        )


# ── Critical consistency regression guard ──────────────────────────


class TestPolicyConsistency:
    """Lock: every policy extension MUST be validatable by
    ``verify_extension_matches_content`` -- either as a CODE extension
    (textual invariant) or via a ``_EXTENSION_TO_MIME_FAMILIES`` entry
    (document/media exact-family match).

    Drift would cause runtime ``FORBIDDEN_TYPE`` rejections of
    legitimate uploads (the policy whitelists an extension, but the
    verifier has no branch for it). This test catches the drift at CI
    time instead of in production (№17: a code extension that is neither
    in ``CODE_EXTENSIONS`` nor the MIME map would silently fail).
    """

    @pytest.mark.parametrize(
        "policy", [AUTHORED_POLICY, HOMEWORK_POLICY], ids=["authored", "homework"]
    )
    def test_all_policy_extensions_are_verifiable(self, policy: ContextPolicy) -> None:
        # A code extension is handled by the textual invariant, not the
        # family map; a document/media extension needs a map entry.
        uncovered = (
            policy.allowed_extensions
            - set(_EXTENSION_TO_MIME_FAMILIES)
            - CODE_EXTENSIONS
        )
        assert uncovered == set(), (
            f"{policy.name} policy lists extensions {uncovered!r} that are "
            f"neither CODE_EXTENSIONS (textual invariant) nor in "
            f"_EXTENSION_TO_MIME_FAMILIES (exact-family)"
        )

    def test_code_extensions_absent_from_family_map(self) -> None:
        # №17 footgun guard: a hand-maintained family entry for a code
        # extension is what silently discarded TypeScript. Code is
        # governed solely by the textual invariant — keep the map clean.
        leaked = CODE_EXTENSIONS & set(_EXTENSION_TO_MIME_FAMILIES)
        assert leaked == set(), (
            f"code extensions {leaked!r} must not carry a family-map entry; "
            f"they are validated by the textual invariant"
        )

    def test_code_extensions_are_text_in_normalizer(self) -> None:
        # №18 footgun guard (mirror of the family-map guard above):
        # everything the AUTHOR can upload as code, the student project
        # normalizer MUST read as text — else the Mentor reviews a
        # submission without seeing the changed code (that was the defect:
        # .tsx/.jsx/... classified BINARY -> no text extraction). The
        # invariant is tautological under the derived-union construction
        # in classify.py, and that is exactly why it is valuable: it
        # guards the CONSTRUCTION, not the values. If someone "simplifies"
        # the union back to a hand-maintained list, this fails at CI.
        from course_supporter.normalizer.classify import _TEXT_EXTS

        missing = CODE_EXTENSIONS - _TEXT_EXTS
        assert missing == set(), (
            f"code extensions {missing!r} are not read as text by the "
            f"normalizer; the Mentor would review submissions without "
            f"their code (see _TEXT_EXTS = _NORMALIZER_TEXT_EXTS | "
            f"CODE_EXTENSIONS in normalizer/classify.py)"
        )


# ── Derived-surface locks (gates, §1.2) ────────────────────────────


class TestConveyorTable:
    """Every accepted format names its checking pipeline, and only those.

    The product question ("may a student send this?") and the security
    question ("how is it verified?") live in two structures on purpose, so
    the lock is that neither may answer without the other. It guards the
    CONSTRUCTION: silencing it by adding a name to the table is impossible
    without also naming a conveyor, which is the whole point.
    """

    def test_every_accepted_format_has_a_conveyor(self) -> None:
        unrouted = HOMEWORK_POLICY.allowed_extensions - set(HOMEWORK_CONVEYORS)
        assert unrouted == set(), (
            f"accepted extensions {unrouted!r} have no conveyor; a format the "
            f"student may send with no named way to check it is exactly what "
            f"the table exists to forbid"
        )

    def test_no_conveyor_for_an_unaccepted_format(self) -> None:
        orphans = set(HOMEWORK_CONVEYORS) - HOMEWORK_POLICY.allowed_extensions
        assert orphans == set(), (
            f"conveyors {orphans!r} route extensions no policy accepts — dead "
            f"routing drifts into a false sense of coverage"
        )

    def test_lock_reddens_on_a_format_without_a_conveyor(self) -> None:
        # The lock is only worth having if it actually fires. Prove the
        # predicate on a policy widened past the table, rather than trusting
        # that a tautology under the derived construction would catch drift.
        widened = HOMEWORK_POLICY.allowed_extensions | {"rst"}
        assert widened - set(HOMEWORK_CONVEYORS) == {"rst"}

    def test_conveyor_values_are_the_three_known_pipelines(self) -> None:
        assert set(HOMEWORK_CONVEYORS.values()) == {"text", "archive", "document"}


class TestDoorMatchesPolicy:
    """The route door and Stage 1 accept the same set, in two shapes.

    They must not be compared textually: the door carries a leading dot
    (``file_extension``) and the policy does not (``extension_of``). A
    textual comparison would be green and meaningless, so the lock
    normalises first — and separately pins that the shapes really do differ,
    so the transform cannot quietly become a no-op.
    """

    def test_door_equals_policy_after_normalisation(self) -> None:
        normalised = {ext.removeprefix(".") for ext in ALLOWED_HOMEWORK_EXTENSIONS}
        assert normalised == HOMEWORK_POLICY.allowed_extensions, (
            "the submission door and the Stage 1 policy disagree; a student "
            "would meet either a 422 for a format the worker accepts, or a "
            "silent worker rejection after a 202"
        )

    def test_door_carries_the_dot_prefix(self) -> None:
        assert all(ext.startswith(".") for ext in ALLOWED_HOMEWORK_EXTENSIONS)
        assert not any(
            ext.startswith(".") for ext in HOMEWORK_POLICY.allowed_extensions
        )


class TestTextExtensionsDerived:
    """Everything a student may send as text is screened as text.

    Sibling of ``test_code_extensions_are_text_in_normalizer``: same empty
    set difference, same reason. Here the failure mode is worse than a poor
    review — an unscreened extension skips the unicode hard-reject and the
    prompt-injection pre-screen entirely.
    """

    def test_every_code_extension_is_screened(self) -> None:
        unscreened = CODE_EXTENSIONS - _TEXT_EXTENSIONS
        assert unscreened == set(), (
            f"code extensions {unscreened!r} are admitted but not run through "
            f"the text content checks; they would reach the model past the "
            f"unicode reject and the injection pre-screen (see _TEXT_EXTENSIONS "
            f"= _PROSE | CODE_EXTENSIONS in security/stage1.py)"
        )

    def test_prose_is_screened(self) -> None:
        assert _PROSE <= _TEXT_EXTENSIONS

    def test_text_conveyor_and_screened_set_agree(self) -> None:
        routed_as_text = {e for e, c in HOMEWORK_CONVEYORS.items() if c == "text"}
        assert routed_as_text == _TEXT_EXTENSIONS
