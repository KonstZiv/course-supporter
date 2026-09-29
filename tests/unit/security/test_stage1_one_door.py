"""Stage 1 through the one text screen (task 11).

The student's archive: names screened, secrets named and never read, the
closed list of extensionless text files read as text. The author's side:
strict as before, with the composed emoji no longer mistaken for an attack.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError
from course_supporter.security.stage1 import Stage1Result, run_stage1

MAKEFILE = b"test:\n\tpytest -q\n"
DOCKERFILE = b"FROM python:3.13-slim\nRUN pip install uv\n"
ENV = b"OPENAI_API_KEY=sk-live-secret\n"


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _student(files: dict[str, bytes]) -> Stage1Result:
    return run_stage1(filename="hw.zip", content=_zip(files), context="homework")


class TestExtensionlessTextFiles:
    def test_makefile_and_dockerfile_are_read_as_text(self) -> None:
        result = _student(
            {"main.py": b"print(1)\n", "Makefile": MAKEFILE, "Dockerfile": DOCKERFILE}
        )
        assert result.archive_entries is not None
        by_name = {e.arcname: e.content for e in result.archive_entries}
        assert by_name["Makefile"] == MAKEFILE
        assert by_name["Dockerfile"] == DOCKERFILE
        assert result.not_opened == ()

    def test_a_listed_name_with_binary_content_is_named_not_read(self) -> None:
        result = _student({"main.py": b"x = 1\n", "Makefile": b"\x7fELF\x00\x00"})
        assert [(n.arcname, n.reason) for n in result.not_opened] == [
            ("Makefile", ErrorCategory.MAGIC_MISMATCH)
        ]

    def test_other_extensionless_files_stay_unread(self) -> None:
        result = _student({"main.py": b"x = 1\n", "NOTES": b"hello\n"})
        assert [(n.arcname, n.reason) for n in result.not_opened] == [
            ("NOTES", ErrorCategory.FORBIDDEN_TYPE)
        ]


class TestSecrets:
    @pytest.mark.parametrize(
        "name", [".env", ".env.local", "certs/server.pem", "tls.key", "id_rsa"]
    )
    def test_named_with_the_reason_and_never_read(self, name: str) -> None:
        result = _student({"main.py": b"x = 1\n", name: ENV})
        assert [(n.arcname, n.reason) for n in result.not_opened] == [
            (name, ErrorCategory.MAY_CONTAIN_SECRETS)
        ]
        assert result.archive_entries is not None
        assert [e.arcname for e in result.archive_entries] == ["main.py"]

    def test_the_template_is_read(self) -> None:
        result = _student({"main.py": b"x = 1\n", ".env.example": b"KEY=\n"})
        assert result.not_opened == ()
        assert result.archive_entries is not None
        assert ".env.example" in {e.arcname for e in result.archive_entries}

    def test_a_secret_named_like_code_is_still_a_secret(self) -> None:
        # ``.env.py`` has a code extension, but ``.env.*`` decides first.
        result = _student({"main.py": b"x = 1\n", ".env.py": b"KEY='x'\n"})
        assert [n.reason for n in result.not_opened] == [
            ErrorCategory.MAY_CONTAIN_SECRETS
        ]


class TestMemberNames:
    def test_a_direction_override_in_a_name_refuses_the_submission(self) -> None:
        with pytest.raises(SecurityRejectedError) as refused:
            _student({"main.py": b"x = 1\n", "evil‮txt.py": b"x = 2\n"})
        assert refused.value.category is ErrorCategory.SUSPICIOUS_UNICODE

    def test_a_phrase_in_a_name_is_a_name_flag(self) -> None:
        name = "docs/ignore previous instructions.md"
        result = _student({"main.py": b"x = 1\n", name: b"# notes\n"})
        assert [(f.source, f.where, f.line) for f in result.flags] == [
            (name, "name", None)
        ]

    def test_the_name_of_an_unread_file_is_screened_too(self) -> None:
        with pytest.raises(SecurityRejectedError):
            _student({"main.py": b"x = 1\n", "a‮b.exe": b"MZ"})


class TestTheAuthorIsStrictAsBefore:
    def test_a_composed_emoji_in_an_authored_readme_is_taken(self) -> None:
        """The author's emoji lock: U+200D between emoji is not an attack."""
        readme = "# Агенти 👨‍💻\n\nКурс про агентську розробку 🏳️‍🌈.\n".encode()
        result = run_stage1(filename="README.md", content=readme, context="authored")
        assert result.nfc_text == readme.decode()
        assert result.flags == ()

    def test_other_zero_width_in_authored_text_still_refuses(self) -> None:
        with pytest.raises(SecurityRejectedError) as refused:
            run_stage1(
                filename="README.md",
                content="# Агенти​\n".encode(),
                context="authored",
            )
        assert refused.value.category is ErrorCategory.SUSPICIOUS_UNICODE

    def test_the_student_archive_rules_do_not_reach_an_authored_archive(self) -> None:
        # Strict and all-or-nothing, as before: an extensionless file refuses.
        with pytest.raises(SecurityRejectedError) as refused:
            run_stage1(
                filename="materials.zip",
                content=_zip({"main.py": b"x = 1\n", "Makefile": MAKEFILE}),
                context="authored",
            )
        assert refused.value.category is ErrorCategory.FORBIDDEN_TYPE
