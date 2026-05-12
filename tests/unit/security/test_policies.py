"""Tests for context security policies (KD14).

Vision conformance for the §KD14 policy table:

* :class:`TestAuthoredPolicy` -- AUTHORED constant matches table.
* :class:`TestHomeworkPolicy` -- HOMEWORK constant matches table.
* :class:`TestPolicyResolver` -- ``policy_for`` resolution + guard.
* :class:`TestVideoSizeResolver` -- ``get_max_size_for_extension``.
* :class:`TestPolicyConsistency` -- regression guard against drift
  between policy whitelists and the file_type MIME map.
"""

import pytest

from course_supporter.security.file_type import _EXTENSION_TO_MIME_FAMILIES
from course_supporter.security.policies import (
    AUTHORED_POLICY,
    HOMEWORK_POLICY,
    ContextPolicy,
    get_max_size_for_extension,
    policy_for,
)

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
        assert {"pdf", "pptx", "md", "docx", "txt", "html"} <= (
            AUTHORED_POLICY.allowed_extensions
        )

    def test_no_archive_extensions(self) -> None:
        # Authored context does not accept archive uploads.
        assert {"zip", "gz", "tgz"} & AUTHORED_POLICY.allowed_extensions == set()

    def test_size_limits(self) -> None:
        assert AUTHORED_POLICY.max_file_size_bytes == 100 * 1024 * 1024
        assert AUTHORED_POLICY.max_video_size_bytes == 5 * 1024 * 1024 * 1024

    def test_archive_disabled(self) -> None:
        assert AUTHORED_POLICY.max_archive_unzipped_bytes is None
        assert AUTHORED_POLICY.max_archive_nesting_depth is None

    def test_safety_and_charset_flags(self) -> None:
        assert AUTHORED_POLICY.enable_llm_safety_check is True
        assert AUTHORED_POLICY.enable_charset_strict is False


# ── Homework policy ────────────────────────────────────────────────


class TestHomeworkPolicy:
    def test_name(self) -> None:
        assert HOMEWORK_POLICY.name == "homework"

    def test_extensions_match_table(self) -> None:
        assert HOMEWORK_POLICY.allowed_extensions == frozenset(
            {"pdf", "md", "txt", "py", "ipynb", "zip", "gz", "tgz"}
        )

    def test_no_video_extensions(self) -> None:
        # Homework context does not accept video uploads.
        assert {"mp4", "mov", "avi", "mkv", "webm"} & (
            HOMEWORK_POLICY.allowed_extensions
        ) == set()

    def test_size_limits(self) -> None:
        assert HOMEWORK_POLICY.max_file_size_bytes == 1 * 1024 * 1024
        assert HOMEWORK_POLICY.max_video_size_bytes is None

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
        assert (
            get_max_size_for_extension("mp4", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_video_size_bytes
        )

    def test_audio_extension_returns_default(self) -> None:
        # Audio is NOT in _VIDEO_EXTENSIONS — default cap applies.
        assert (
            get_max_size_for_extension("mp3", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_file_size_bytes
        )

    def test_document_extension_returns_default(self) -> None:
        assert (
            get_max_size_for_extension("pdf", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_file_size_bytes
        )

    def test_video_extension_in_homework_returns_default(self) -> None:
        # HOMEWORK has no max_video_size_bytes (None) — even if the
        # extension is in _VIDEO_EXTENSIONS, the default applies.
        assert (
            get_max_size_for_extension("mp4", HOMEWORK_POLICY)
            == HOMEWORK_POLICY.max_file_size_bytes
        )

    def test_uppercase_extension_normalized(self) -> None:
        assert (
            get_max_size_for_extension("MP4", AUTHORED_POLICY)
            == AUTHORED_POLICY.max_video_size_bytes
        )


# ── Critical consistency regression guard ──────────────────────────


class TestPolicyConsistency:
    """Lock: every extension in either policy MUST exist in the
    file_type module's MIME map.

    Drift between the two would cause runtime ``FORBIDDEN_TYPE``
    rejections of legitimate uploads (the policy whitelists an
    extension, but ``verify_extension_matches_content`` rejects it
    because the MIME map has no entry). This test catches the drift
    at CI time instead of in production.
    """

    @pytest.mark.parametrize(
        "policy", [AUTHORED_POLICY, HOMEWORK_POLICY], ids=["authored", "homework"]
    )
    def test_all_policy_extensions_exist_in_file_type_map(
        self, policy: ContextPolicy
    ) -> None:
        missing = policy.allowed_extensions - set(_EXTENSION_TO_MIME_FAMILIES)
        assert missing == set(), (
            f"{policy.name} policy lists extensions {missing!r} that have no "
            f"entry in _EXTENSION_TO_MIME_FAMILIES"
        )
